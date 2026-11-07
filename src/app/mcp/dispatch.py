"""Run the reviewed content operations through the existing application boundary.

The internal ASGI request preserves FastAPI validation, ownership-before-cache,
idempotency dependencies and exception handlers. No delegated token is forwarded to
REST, and neither an arbitrary URL nor arbitrary headers can be supplied by a client.
"""

import json
import tempfile
from email.message import Message
from typing import IO, Any
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, InputRequiredResult, TextContent, Tool, ToolAnnotations
from starlette.types import ASGIApp, Receive, Scope, Send

from ..authorization.operations import OPERATIONS, Operation
from ..core.db.database import local_session
from ..models.mcp import McpAudit
from ..services import blob_store
from .actor import SCOPE_KEY, DelegatedActor
from .downloads import DownloadCapture, DownloadTooLarge
from .files import MAX_STAGED_BYTES, owned_file, store_file
from .oauth import provider
from .profiles import sampled_profile
from .schemas import argument_schema

MAX_RESULT_BYTES = 512 * 1024


class ActorApplication:
    def __init__(self, app: FastAPI, actor: DelegatedActor) -> None:
        self.app, self.actor = app, actor

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app({**scope, SCOPE_KEY: self.actor}, receive, send)


def result(content: Any, *, error: bool = False) -> CallToolResult:
    text = json.dumps(content, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        text = json.dumps({"detail": "Result exceeds the output limit. Narrow the query or export the logbook."})
        error = True
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=content if isinstance(content, dict) and not error else None,
        is_error=error,
    )


class ContentServer(MCPServer[None]):
    def __init__(self, app: FastAPI, **kwargs: Any) -> None:
        super().__init__("OpenDiving", subscriptions=False, **kwargs)
        self.app = app
        self.operations = {op.name: op for op in OPERATIONS}
        self.schemas: dict[str, dict[str, Any]] = {}

    def schema(self, op: Operation) -> dict[str, Any]:
        if op.name not in self.schemas:
            self.schemas[op.name] = argument_schema(op, self.app.openapi())
        return self.schemas[op.name]

    async def list_tools(self) -> list[Tool]:
        access = get_access_token()
        if access is None:
            return []
        return [
            Tool(
                name=op.name,
                description=self.app.openapi()["paths"][op.path][op.method.lower()].get("description", op.name),
                input_schema=self.schema(op),
                annotations=ToolAnnotations(
                    read_only_hint=op.method == "GET",
                    destructive_hint=op.method == "DELETE" or "dives:delete" in op.scopes,
                    idempotent_hint=op.method in {"GET", "PUT"},
                    open_world_hint=any(
                        part in op.path for part in ("/species", "/geocode", "/dive-conditions", "/import/")
                    ),
                ),
            )
            for op in OPERATIONS
            if set(op.scopes) <= set(access.scopes)
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[None, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        access = get_access_token()
        op = self.operations.get(name)
        if access is None or op is None or not set(op.scopes) <= set(access.scopes):
            return result({"detail": "Tool unavailable or permission not granted."}, error=True)
        # Revalidate here as well as at the transport, so revocation takes effect on the next call.
        fresh = await provider.load_access_token(access.token)
        account = await provider.account(fresh) if fresh else None
        if fresh is None or account is None:
            return result({"detail": "Connection expired or revoked."}, error=True)
        download = None
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as spool:
            try:
                response = await self.execute(op, arguments, account, download=spool)
                if response.is_success and op.result_kind == "file":
                    disposition = Message()
                    disposition["Content-Disposition"] = response.headers.get("content-disposition", "")
                    async with local_session() as db:
                        row = await store_file(
                            db,
                            fresh,
                            spool,
                            disposition.get_filename() or "opendiving-download",
                            response.headers.get("content-type", "application/octet-stream"),
                        )
                        download = {
                            "handle": str(row.uuid),
                            "filename": row.filename,
                            "content_type": row.content_type,
                            "download_url": "/api/v1/mcp/files/" + str(row.uuid),
                            "byte_size": row.byte_size,
                            "expires_at": row.expires_at,
                            "instructions": (
                                "Download with this connection's MCP access token in Authorization: Bearer."
                            ),
                        }
            except HTTPException as exc:
                response = httpx.Response(exc.status_code, json={"detail": exc.detail})
        async with local_session() as db:
            db.add(McpAudit(grant_id=(fresh.claims or {})["grant_id"], tool=name, status=response.status_code))
            await db.commit()
        if download is not None:
            return result(download)
        try:
            data = response.json()
        except ValueError:
            return result({"detail": "Binary result requires a download resource."}, error=True)
        if op.name == "read_dive_profile" and response.is_success:
            data = sampled_profile(data, arguments.get("max_profile_points", 500))
        if op.account_view is not None and op.method == "GET" and response.is_success:
            data = {key: value for key, value in data.items() if key in op.account_view.read_fields}
        return result({"status": response.status_code, "data": data}, error=response.is_error)

    async def execute(
        self, op: Operation, arguments: dict[str, Any], account: dict[str, Any], *, download: IO[bytes]
    ) -> httpx.Response:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError

        try:
            Draft202012Validator(self.schema(op), format_checker=Draft202012Validator.FORMAT_CHECKER).validate(
                arguments
            )
        except ValidationError as exc:
            return httpx.Response(422, json={"detail": f"Invalid arguments at {'.'.join(str(p) for p in exc.path)}"})
        path = op.path
        for key, value in arguments.get("path", {}).items():
            path = path.replace("{" + key + "}", quote(str(value), safe=""))
        headers = {"Authorization": "Bearer internal-mcp"}
        if "idempotency_key" in arguments:
            headers["Idempotency-Key"] = arguments["idempotency_key"]
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        access = get_access_token()
        if arguments.get("files"):
            if access is None or "files:write" not in access.scopes:
                return httpx.Response(403, json={"detail": "files:write permission required."})
            async with local_session() as db:
                for handle in arguments["files"]:
                    row = await owned_file(db, UUID(handle), access)
                    files.append(("file", (row.filename, await blob_store.get(row.storage_key), row.content_type)))
        application: ASGIApp = ActorApplication(
            self.app, DelegatedActor(account, frozenset(access.scopes) if access else frozenset())
        )
        if op.result_kind == "file":
            application = DownloadCapture(application, download, MAX_STAGED_BYTES)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://mcp-internal"
        ) as client:
            if files:
                form = {
                    key: json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
                    for key, value in arguments.get("body", {}).items()
                    if value is not None
                }
                request = client.build_request(
                    op.method, path, params=arguments.get("query"), data=form, files=files, headers=headers
                )
            else:
                request = client.build_request(
                    op.method, path, params=arguments.get("query"), json=arguments.get("body"), headers=headers
                )
            try:
                return await client.send(request)
            except DownloadTooLarge:
                return httpx.Response(413, json={"detail": "Download exceeds the 50 MiB temporary file limit."})
