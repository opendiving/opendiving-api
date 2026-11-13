"""Exercise MCP content adapters with real OAuth, routes, PostgreSQL and file storage."""

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.app.api.v1 import export
from src.app.mcp import dispatch, files
from src.app.models.mcp import McpFile
from src.app.models.user import User
from src.app.services import blob_store
from tests.conftest import db_available
from tests.test_mcp import browser, connected, rpc
from tests.test_mcp import mcp_http as mcp_http

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not db_available(), reason="PostgreSQL required")]


def tool_data(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    data = json.loads(response.json()["result"]["content"][0]["text"])
    assert isinstance(data, dict)
    return data


async def test_expired_files_release_quota_without_waiting_for_worker(
    mcp_http: httpx.AsyncClient,
    async_sessions: async_sessionmaker[AsyncSession],
    diver: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(files, "MAX_STAGED_BYTES", 3)
    tokens = await connected(mcp_http, await browser(async_sessions, diver), ["files:read", "files:write"])
    headers = {"Authorization": "Bearer " + tokens["access_token"]}
    first = await mcp_http.post("/api/v1/mcp/files", headers=headers, files={"file": ("first.txt", b"abc")})
    assert first.status_code == 200, first.text
    async with async_sessions() as db:
        key = await db.scalar(select(McpFile.storage_key).where(McpFile.user_id == diver.id))
        await db.execute(
            update(McpFile)
            .where(McpFile.user_id == diver.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
    next_file = await mcp_http.post("/api/v1/mcp/files", headers=headers, files={"file": ("next.txt", b"x")})
    assert next_file.status_code == 200, next_file.text
    assert key is not None
    assert not await blob_store.has(key)
    assert (await mcp_http.get("/api/v1/mcp/files/" + first.json()["handle"], headers=headers)).status_code == 404


async def test_expired_handles_can_be_discarded_by_their_owner(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    tokens = await connected(mcp_http, await browser(async_sessions, diver), ["files:write"])
    headers = {"Authorization": "Bearer " + tokens["access_token"]}
    uploaded = await mcp_http.post("/api/v1/mcp/files", headers=headers, files={"file": ("file.txt", b"abc")})
    assert uploaded.status_code == 200, uploaded.text
    async with async_sessions() as db:
        await db.execute(
            update(McpFile)
            .where(McpFile.user_id == diver.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
    assert (await mcp_http.delete("/api/v1/mcp/files/" + uploaded.json()["handle"], headers=headers)).status_code == 200


async def test_revoked_connection_files_do_not_block_new_connections(
    mcp_http: httpx.AsyncClient,
    async_sessions: async_sessionmaker[AsyncSession],
    diver: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(files, "MAX_STAGED_BYTES", 3)
    session = await browser(async_sessions, diver)
    tokens = await connected(mcp_http, session, ["files:write"])
    uploaded = await mcp_http.post(
        "/api/v1/mcp/files",
        headers={"Authorization": "Bearer " + tokens["access_token"]},
        files={"file": ("file.txt", b"abc")},
    )
    assert uploaded.status_code == 200, uploaded.text
    connection = (await mcp_http.get("/api/v1/user/mcp/connections", headers=session)).json()[0]
    revoked = await mcp_http.delete("/api/v1/user/mcp/connection/" + connection["uuid"], headers=session)
    assert revoked.status_code == 200
    new_tokens = await connected(mcp_http, session, ["files:write"])
    next_file = await mcp_http.post(
        "/api/v1/mcp/files",
        headers={"Authorization": "Bearer " + new_tokens["access_token"]},
        files={"file": ("next.txt", b"x")},
    )
    assert next_file.status_code == 200, next_file.text


async def test_oversized_export_stops_after_middleware_prefetch_and_closes_its_spool(
    mcp_http: httpx.AsyncClient,
    async_sessions: async_sessionmaker[AsyncSession],
    diver: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountedBuffer(io.BytesIO):
        consumed = 0

        def read(self, size: int | None = -1) -> bytes:
            chunk = super().read(size)
            self.consumed += len(chunk)
            return chunk

    monkeypatch.setattr(dispatch, "MAX_STAGED_BYTES", 3)
    buffer = CountedBuffer(b"x" * (128 * 1024))
    monkeypatch.setattr(export, "load_export_bundle", AsyncMock(return_value=None))
    monkeypatch.setattr(export, "write_archive", AsyncMock(return_value=buffer))
    tokens = await connected(mcp_http, await browser(async_sessions, diver), ["export:read", "files:read"])
    response = await rpc(mcp_http, tokens["access_token"], "tools/call", {"name": "export_archive", "arguments": {}})
    assert response.json()["result"]["isError"]
    assert tool_data(response)["status"] == 413
    # BaseHTTPMiddleware may prefetch one chunk before forwarding response headers.
    assert buffer.consumed <= 64 * 1024
    assert buffer.closed


async def test_export_download_keeps_its_filename_media_type_and_bytes(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    session = await browser(async_sessions, diver)
    tokens = await connected(mcp_http, session, ["export:read", "files:read"])
    response = await rpc(mcp_http, tokens["access_token"], "tools/call", {"name": "export_csv", "arguments": {}})
    assert not response.json()["result"].get("isError"), response.text
    metadata = tool_data(response)
    assert metadata["filename"].endswith(".csv")
    download = await mcp_http.get(
        metadata["download_url"], headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    original = await mcp_http.get("/api/v1/export/csv", headers=session)
    assert download.content == original.content
    assert download.headers["content-type"] == original.headers["content-type"]
    assert download.headers["content-disposition"] != 'attachment; filename="opendiving-export"'
    assert metadata["filename"] in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"


async def test_retry_keys_are_only_accepted_by_operations_that_support_them(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    tokens = await connected(mcp_http, await browser(async_sessions, diver), ["presets:write", "people:write"])
    token = tokens["access_token"]
    person = {"name": "write_person", "arguments": {"body": {"name": "MCP Buddy"}, "idempotency_key": "person-once"}}
    first = tool_data(await rpc(mcp_http, token, "tools/call", person))
    repeated = tool_data(await rpc(mcp_http, token, "tools/call", person))
    assert first["data"]["uuid"] == repeated["data"]["uuid"]
    defaults: dict[str, Any] = {"name": "restore_default_dive_form_presets", "arguments": {}}
    assert tool_data(await rpc(mcp_http, token, "tools/call", defaults))["status"] == 200
    refused = tool_data(
        await rpc(mcp_http, token, "tools/call", {**defaults, "arguments": {"idempotency_key": "ignored"}})
    )
    assert refused["status"] == 422


async def test_single_file_tools_refuse_multiple_handles_before_mutating(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    session = await browser(async_sessions, diver)
    tokens = await connected(mcp_http, session, ["profile:write", "files:write"])
    headers = {"Authorization": "Bearer " + tokens["access_token"]}
    png = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(png, format="PNG")
    handles = []
    for name, data in [("invalid.png", b"not an image"), ("valid.png", png.getvalue())]:
        staged = await mcp_http.post("/api/v1/mcp/files", headers=headers, files={"file": (name, data, "image/png")})
        assert staged.status_code == 200, staged.text
        handles.append(staged.json()["handle"])
    response = await rpc(
        mcp_http, tokens["access_token"], "tools/call", {"name": "write_user_avatar", "arguments": {"files": handles}}
    )
    assert tool_data(response)["status"] == 422
    assert (await mcp_http.get("/api/v1/user/avatar", headers=session)).status_code == 404
    single = await rpc(
        mcp_http,
        tokens["access_token"],
        "tools/call",
        {"name": "write_user_avatar", "arguments": {"files": [handles[1]]}},
    )
    assert tool_data(single)["status"] == 200


async def test_profile_and_preference_views_are_disjoint_for_reads_and_writes(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    session = await browser(async_sessions, diver)
    profile = await connected(mcp_http, session, ["profile:read", "profile:write"])
    preferences = await connected(mcp_http, session, ["preferences:read", "preferences:write"])
    profile_read = tool_data(
        await rpc(mcp_http, profile["access_token"], "tools/call", {"name": "read_current_user", "arguments": {}})
    )["data"]
    preferences_read = tool_data(
        await rpc(mcp_http, preferences["access_token"], "tools/call", {"name": "read_preferences", "arguments": {}})
    )["data"]
    assert profile_read["email"] == diver.email
    assert preferences_read["units"] == "metric"
    assert set(profile_read).isdisjoint(preferences_read)
    for tokens, name, body in [
        (profile, "patch_user", {"name": "Updated Diver"}),
        (preferences, "patch_preferences", {"units": "imperial"}),
    ]:
        response = await rpc(
            mcp_http, tokens["access_token"], "tools/call", {"name": name, "arguments": {"body": body}}
        )
        assert tool_data(response)["status"] == 200
    denied = await rpc(
        mcp_http,
        preferences["access_token"],
        "tools/call",
        {"name": "patch_preferences", "arguments": {"body": {"name": "Not allowed"}}},
    )
    assert tool_data(denied)["status"] == 422
