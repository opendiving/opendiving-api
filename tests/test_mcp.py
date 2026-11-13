"""Real HTTP OAuth, permission and ownership checks against migrated PostgreSQL."""

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from uuid6 import uuid7

from src.app.api import router
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.core.utils.request_context import RequestContext
from src.app.mcp import dispatch, oauth, resources
from src.app.mcp.catalog import OPERATIONS
from src.app.mcp.permissions import SCOPES
from src.app.mcp.transport import install_mcp
from src.app.mcp.urls import issuer_url, resource_url
from src.app.models.mcp import McpAuthorization, McpGrant, McpToken
from src.app.models.user import User
from src.app.services.auth_service import issue_tokens
from tests.conftest import db_available

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not db_available(), reason="PostgreSQL required")]


@pytest_asyncio.fixture
async def mcp_http(
    async_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[httpx.AsyncClient]:
    monkeypatch.setattr(settings, "MCP_ALLOWED_HOSTS", "testserver," + settings.MCP_ALLOWED_HOSTS)
    app = create_application(router=router, settings=settings, apply_migrations_on_start=False)
    server = install_mcp(app)
    monkeypatch.setattr(oauth, "local_session", async_sessions)
    monkeypatch.setattr(dispatch, "local_session", async_sessions)
    monkeypatch.setattr(resources, "local_session", async_sessions)

    async def database() -> AsyncGenerator[AsyncSession]:
        async with async_sessions() as db:
            yield db

    app.dependency_overrides[async_get_db] = database
    from src.app.core.setup import close_redis_cache_pool, create_redis_cache_pool

    await create_redis_cache_pool()
    ready, stop = asyncio.Event(), asyncio.Event()

    async def manager() -> None:
        async with server.session_manager.run():
            ready.set()
            await stop.wait()

    task = asyncio.create_task(manager())
    await ready.wait()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        stop.set()
        await task
        await close_redis_cache_pool()


async def browser(sessions: async_sessionmaker[AsyncSession], diver: User) -> dict[str, str]:
    async with sessions() as db:
        tokens = await issue_tokens(
            Response(),
            diver.uuid,
            db=db,
            context=RequestContext(ip="127.0.0.1", user_agent="MCP integration test"),
            user_id=diver.id,
        )
    return {"Authorization": "Bearer " + tokens.access_token}


async def authorize(
    client: httpx.AsyncClient, browser_headers: dict[str, str], scopes: list[str]
) -> tuple[dict[str, str], dict[str, Any]]:
    registration = await client.post(
        "/api/v1/mcp/oauth/register",
        json={
            "client_name": "Test application",
            "redirect_uris": ["http://127.0.0.1:7777/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": " ".join(scopes),
        },
    )
    assert registration.status_code == 201, registration.text
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    request = {
        "client_id": registration.json()["client_id"],
        "redirect_uri": "http://127.0.0.1:7777/callback",
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource_url(),
        "scope": " ".join(scopes),
        "state": "opaque-state",
    }
    redirect = await client.get("/api/v1/mcp/oauth/authorize", params=request)
    assert redirect.status_code == 302, redirect.text
    pending = parse_qs(urlsplit(redirect.headers["location"]).query)["request"][0]
    consent = await client.get("/api/v1/user/mcp/consent", params={"request": pending}, headers=browser_headers)
    assert consent.status_code == 200, consent.text
    decision = await client.post(
        "/api/v1/user/mcp/consent", json={"request": pending, "scopes": scopes}, headers=browser_headers
    )
    assert decision.status_code == 200, decision.text
    callback = parse_qs(urlsplit(decision.json()["redirect_uri"]).query)
    assert callback["state"] == ["opaque-state"]
    return {
        "grant_type": "authorization_code",
        "client_id": request["client_id"],
        "code": callback["code"][0],
        "redirect_uri": request["redirect_uri"],
        "code_verifier": verifier,
        "resource": resource_url(),
    }, consent.json()


async def connected(client: httpx.AsyncClient, headers: dict[str, str], scopes: list[str]) -> dict[str, Any]:
    form, _ = await authorize(client, headers, scopes)
    response = await client.post("/api/v1/mcp/oauth/token", data=form)
    assert response.status_code == 200, response.text
    tokens: dict[str, Any] = response.json()
    return tokens


async def rpc(
    client: httpx.AsyncClient, token: str, method: str, params: dict[str, Any] | None = None
) -> httpx.Response:
    return await client.post(
        "/api/v1/mcp",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "MCP-Method": method,
            **({"MCP-Name": str(params["name"])} if params and "name" in params else {}),
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {
                **(params or {}),
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )


async def test_discovery_and_unauthenticated_transport(mcp_http: httpx.AsyncClient) -> None:
    metadata = await mcp_http.get("/.well-known/oauth-protected-resource/api/v1/mcp")
    assert metadata.json()["resource"] == resource_url()
    issuer = await mcp_http.get("/.well-known/oauth-authorization-server/api/v1/mcp/oauth")
    assert issuer.json()["issuer"] == issuer_url()
    assert issuer.json()["code_challenge_methods_supported"] == ["S256"]
    response = await rpc(mcp_http, "not-a-token", "tools/list")
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


async def test_code_pkce_resource_and_single_use(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    form, _ = await authorize(mcp_http, await browser(async_sessions, diver), ["dives:read"])
    for changed in (
        {"resource": "https://evil.example/mcp"},
        {"code_verifier": "b" * 64},
        {"redirect_uri": "http://127.0.0.1:7777/other"},
    ):
        response = await mcp_http.post("/api/v1/mcp/oauth/token", data={**form, **changed})
        assert response.status_code == 400, response.text
    first, second = await asyncio.gather(*(mcp_http.post("/api/v1/mcp/oauth/token", data=form) for _ in range(2)))
    assert sorted([first.status_code, second.status_code]) == [200, 400]
    async with async_sessions() as db:
        assert await db.get(McpAuthorization, hashlib.sha256(form["code"].encode()).hexdigest()) is None
        assert await db.scalar(select(McpToken).where(McpToken.secret_hash == form["code"])) is None


async def test_refresh_rotation_replay_and_revoke(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    headers = await browser(async_sessions, diver)
    tokens = await connected(mcp_http, headers, ["dives:read"])
    rows = (await mcp_http.get("/api/v1/user/mcp/connections", headers=headers)).json()
    form = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": rows[0]["client_id"],
        "resource": resource_url(),
    }
    rotated = await mcp_http.post("/api/v1/mcp/oauth/token", data=form)
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]
    assert (await mcp_http.post("/api/v1/mcp/oauth/token", data=form)).status_code == 400
    assert (await rpc(mcp_http, rotated.json()["access_token"], "tools/list")).status_code == 401


async def test_connection_activity_tracks_authenticated_requests(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    headers = await browser(async_sessions, diver)
    tokens = await connected(mcp_http, headers, ["dives:read"])
    path = "/api/v1/user/mcp/connections"
    connection = (await mcp_http.get(path, headers=headers)).json()[0]
    assert connection["last_used_at"] is None
    started = datetime.now(UTC)
    assert (await rpc(mcp_http, tokens["access_token"], "tools/list")).status_code == 200
    used = (await mcp_http.get(path, headers=headers)).json()[0]
    assert started <= datetime.fromisoformat(used["last_used_at"]) <= datetime.now(UTC)
    assert (await rpc(mcp_http, tokens["access_token"], "tools/list")).status_code == 200
    assert (await mcp_http.get(path, headers=headers)).json()[0]["last_used_at"] == used["last_used_at"]

    previous = started - timedelta(minutes=5)
    async with async_sessions() as db:
        await db.execute(update(McpGrant).where(McpGrant.uuid == connection["uuid"]).values(last_used_at=previous))
        await db.commit()
    await connected(mcp_http, headers, ["dives:read"])
    assert (await rpc(mcp_http, tokens["access_token"], "tools/list")).status_code == 200
    rows = (await mcp_http.get(path, headers=headers)).json()
    assert rows[0]["last_used_at"] is None
    assert datetime.fromisoformat(rows[1]["last_used_at"]) > previous


async def test_expired_token_does_not_record_connection_activity(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    headers = await browser(async_sessions, diver)
    tokens = await connected(mcp_http, headers, ["dives:read"])
    async with async_sessions() as db:
        await db.execute(
            update(McpToken)
            .where(McpToken.secret_hash == oauth.hash_token(tokens["access_token"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
    assert (await rpc(mcp_http, tokens["access_token"], "tools/list")).status_code == 401
    rows = (await mcp_http.get("/api/v1/user/mcp/connections", headers=headers)).json()
    assert rows[0]["last_used_at"] is None


async def test_content_permissions_and_ownership(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User, other_diver: User
) -> None:
    headers = await browser(async_sessions, diver)
    tokens = await connected(mcp_http, headers, ["people:read", "people:write", "people:delete"])
    token = tokens["access_token"]
    created = await rpc(
        mcp_http,
        token,
        "tools/call",
        {"name": "write_person", "arguments": {"body": {"name": "MCP Buddy"}, "idempotency_key": str(uuid7())}},
    )
    assert created.status_code == 200, created.text
    payload = created.json()["result"]
    assert not payload.get("isError"), payload
    person = json.loads(payload["content"][0]["text"])["data"]
    stranger = await connected(mcp_http, await browser(async_sessions, other_diver), ["people:read"])
    foreign = await rpc(
        mcp_http,
        stranger["access_token"],
        "tools/call",
        {"name": "read_person", "arguments": {"path": {"uuid": person["uuid"]}}},
    )
    assert json.loads(foreign.json()["result"]["content"][0]["text"])["status"] == 404
    refused = await rpc(
        mcp_http,
        stranger["access_token"],
        "tools/call",
        {"name": "erase_person", "arguments": {"path": {"uuid": person["uuid"]}}},
    )
    assert refused.json()["result"]["isError"] is True
    rows = (await mcp_http.get("/api/v1/user/mcp/connections", headers=headers)).json()
    connection_path = "/api/v1/user/mcp/connection/" + rows[0]["uuid"]
    foreign_revoke = await mcp_http.delete(connection_path, headers=await browser(async_sessions, other_diver))
    assert foreign_revoke.status_code == 404
    assert (await mcp_http.delete(connection_path, headers=headers)).status_code == 200
    assert (await rpc(mcp_http, token, "tools/list")).status_code == 401


async def test_catalog_has_no_credentials_or_admin() -> None:
    assert len({op.name for op in OPERATIONS}) == len(OPERATIONS)
    for op in OPERATIONS:
        assert set(op.scopes) <= SCOPES
        assert not any(
            part in op.path
            for part in ("/auth/", "/admin/", "/api-key", "/passkey", "/session", "/email-change", "/download-ticket")
        )


async def test_official_sdk_modern_and_legacy(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    tokens = await connected(mcp_http, await browser(async_sessions, diver), ["dives:read"])
    # Both clients use real ASGI HTTP framing and the SDK's wire protocol serializers.
    asgi = mcp_http._transport
    assert isinstance(asgi, httpx.ASGITransport)
    for modern in (True, False):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=asgi.app), headers={"Authorization": "Bearer " + tokens["access_token"]}
        ) as http:
            async with streamable_http_client("http://testserver/api/v1/mcp", http_client=http) as streams:
                async with ClientSession(*streams) as session:
                    if modern:
                        discovered = await session.discover()
                        assert "2026-07-28" in discovered.supported_versions
                    else:
                        initialized = await session.initialize()
                        assert initialized.server_info.name == "OpenDiving"
                    tools = await session.list_tools()
                    assert "read_dives" in {tool.name for tool in tools.tools}
                    assert "write_dive" not in {tool.name for tool in tools.tools}
                    response = await session.call_tool("read_dives", {"query": {"page": 1, "items_per_page": 1}})
                    assert not response.is_error, response


async def test_file_handles_are_private_to_connection(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    headers = await browser(async_sessions, diver)
    scopes = ["files:write", "files:read"]
    first = await connected(mcp_http, headers, scopes)
    second = await connected(mcp_http, headers, scopes)
    bearer = {"Authorization": "Bearer " + first["access_token"]}
    upload = await mcp_http.post(
        "/api/v1/mcp/files", files={"file": ("example.txt", b"hello diving", "text/plain")}, headers=bearer
    )
    assert upload.status_code == 200, upload.text
    path = "/api/v1/mcp/files/" + upload.json()["handle"]
    download = await mcp_http.get(path, headers=bearer)
    assert download.content == b"hello diving"
    cached = await mcp_http.get(path, headers={**bearer, "If-None-Match": download.headers["etag"]})
    assert cached.status_code == 304
    assert cached.content == b""
    refused = await mcp_http.get(path, headers={"Authorization": "Bearer " + second["access_token"]})
    assert refused.status_code == 404
    assert (await mcp_http.delete(path, headers=bearer)).status_code == 200
    assert (await mcp_http.get(path, headers=bearer)).status_code == 404


async def test_profile_permission_does_not_allow_preference_changes(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    tokens = await connected(mcp_http, await browser(async_sessions, diver), ["profile:write"])
    response = await rpc(
        mcp_http,
        tokens["access_token"],
        "tools/call",
        {"name": "patch_user", "arguments": {"body": {"units": "imperial"}}},
    )
    assert json.loads(response.json()["result"]["content"][0]["text"])["status"] == 422
    assert (
        await mcp_http.get(
            "/api/v1/user/mcp/connections", headers={"Authorization": "Bearer " + tokens["access_token"]}
        )
    ).status_code == 401
    assert (await mcp_http.get("/api/v1/people", headers={"Authorization": "Bearer internal-mcp"})).status_code == 401


async def test_all_catalog_schemas_are_resolvable(
    mcp_http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], diver: User
) -> None:
    tokens = await connected(mcp_http, await browser(async_sessions, diver), sorted(SCOPES))
    response = await rpc(mcp_http, tokens["access_token"], "tools/list")
    assert response.status_code == 200, response.text
    tools = response.json()["result"]["tools"]
    assert len(tools) == len(OPERATIONS)
    apply = next(tool for tool in tools if tool["name"] == "apply_logbook_import")
    assert "token" in apply["inputSchema"]["properties"]["body"]["required"]
    assert "file" not in apply["inputSchema"]["properties"]["body"]["properties"]
    from jsonschema import Draft202012Validator

    for tool in tools:
        Draft202012Validator.check_schema(tool["inputSchema"])


async def test_browser_cors_and_transport_origin_boundaries(mcp_http: httpx.AsyncClient) -> None:
    discovery = await mcp_http.options(
        "/.well-known/oauth-protected-resource/api/v1/mcp",
        headers={
            "Origin": "https://client.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "MCP-Protocol-Version",
        },
    )
    assert discovery.status_code == 200
    assert discovery.headers["access-control-allow-origin"] == "*"
    blocked = await mcp_http.options(
        "/api/v1/mcp",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, MCP-Method",
        },
    )
    assert blocked.status_code == 400
    assert "access-control-allow-origin" not in blocked.headers


async def test_registration_allows_requesting_permissions_without_granting_them(mcp_http: httpx.AsyncClient) -> None:
    registration = await mcp_http.post(
        "/api/v1/mcp/oauth/register",
        json={
            "redirect_uris": ["https://client.example/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert registration.status_code == 201
    assert set(registration.json()["scope"].split()) == SCOPES
    challenge = base64.urlsafe_b64encode(hashlib.sha256(b"a" * 64).digest()).decode().rstrip("=")
    requested = await mcp_http.get(
        "/api/v1/mcp/oauth/authorize",
        params={
            "client_id": registration.json()["client_id"],
            "redirect_uri": "https://client.example/callback",
            "response_type": "code",
            "scope": "dives:read dives:write",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource_url(),
        },
    )
    assert requested.status_code == 302
    assert "/mcp/authorize?request=" in requested.headers["location"]
    assert (await rpc(mcp_http, registration.json()["client_id"], "tools/list")).status_code == 401


async def test_oauth_request_bodies_are_bounded(mcp_http: httpx.AsyncClient) -> None:
    response = await mcp_http.post(
        "/api/v1/mcp/oauth/register", content=b"x" * (64 * 1024 + 1), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
