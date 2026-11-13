"""Compose the MCP resource server and its separate OAuth authorization service."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server.auth.routes import build_metadata, create_auth_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..authorization.scopes import SCOPES
from ..core.config import settings, split_csv
from ..core.exceptions.http_exceptions import RateLimitException
from ..core.utils.client_ip import client_ip_bucket
from ..core.utils.rate_limit import enforce_rate_limit
from .consent import router
from .cors import McpCorsMiddleware
from .dispatch import ContentServer
from .files import router as files_router
from .oauth import provider
from .resources import register_resources
from .urls import issuer_url, resource_url, validate_configuration

REGISTRATION = ClientRegistrationOptions(enabled=True, valid_scopes=sorted(SCOPES), default_scopes=sorted(SCOPES))
REVOCATION = RevocationOptions(enabled=True)


class OAuthBoundary:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            await enforce_rate_limit(f"mcp-oauth:{client_ip_bucket(request)}", 120, 60)
        except RateLimitException:
            await JSONResponse({"error": "temporarily_unavailable"}, status_code=429)(scope, receive, send)
            return
        if request.method == "POST":
            payload = bytearray()
            async for chunk in request.stream():
                payload.extend(chunk)
                if len(payload) > 64 * 1024:
                    await JSONResponse({"error": "invalid_request"}, status_code=413)(scope, receive, send)
                    return
            bounded_body = bytes(payload)

            async def replay_bounded() -> Message:
                return {"type": "http.request", "body": bounded_body, "more_body": False}

            receive = replay_bounded
            request = Request(scope, receive)
        path = scope["path"].rstrip("/")
        # SDK token handlers validate PKCE and exact redirects. Resource binding is
        # enforced here too because the SDK does not compare the token request resource.
        if path.endswith("/token") and request.method == "POST":
            body = await request.body()
            form = await request.form()
            if form.get("resource") != resource_url():
                await JSONResponse(
                    {"error": "invalid_target"},
                    status_code=400,
                    headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
                )(scope, receive, send)
                return

            async def replay() -> Message:
                return {"type": "http.request", "body": body, "more_body": False}

            receive = replay
        if path.endswith("/authorize") and request.method == "POST":
            await request.body()
        if path.endswith("/authorize"):
            params = request.query_params if request.method == "GET" else await request.form()
            if params.get("code_challenge_method") != "S256":
                await JSONResponse(
                    {"error": "invalid_request", "error_description": "PKCE S256 is required."}, status_code=400
                )(scope, receive, send)
                return
        if path.endswith("/authorize") and request.method == "POST":
            body = await request.body()

            async def replay_authorize() -> Message:
                return {"type": "http.request", "body": body, "more_body": False}

            receive = replay_authorize
        await self.app(scope, receive, send)


async def authorization_metadata(request: Request) -> JSONResponse:
    metadata = build_metadata(AnyHttpUrl(issuer_url()), None, REGISTRATION, REVOCATION)
    metadata.token_endpoint_auth_methods_supported = ["none"]
    metadata.client_id_metadata_document_supported = True
    metadata.revocation_endpoint_auth_methods_supported = ["none"]
    return JSONResponse(
        metadata.model_dump(mode="json", exclude_none=True),
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


async def protected_metadata(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "resource": resource_url(),
            "authorization_servers": [issuer_url()],
            "scopes_supported": sorted(SCOPES),
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


def install_mcp(app: FastAPI) -> ContentServer:
    app.add_middleware(McpCorsMiddleware)
    app.include_router(router)
    app.include_router(files_router)
    server = ContentServer(
        app,
        token_verifier=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer_url()),
            resource_server_url=AnyHttpUrl(resource_url()),
            validate_token_resource=True,
        ),
    )
    register_resources(server)
    hosts = [urlsplit(resource_url()).netloc, *split_csv(settings.MCP_ALLOWED_HOSTS)]
    transport = server.streamable_http_app(
        streamable_http_path="/api/v1/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=hosts, allowed_origins=[settings.frontend_origin, *split_csv(settings.MCP_ALLOWED_ORIGINS)]
        ),
    )
    auth_routes = create_auth_routes(
        provider, AnyHttpUrl(issuer_url()), client_registration_options=REGISTRATION, revocation_options=REVOCATION
    )
    auth_routes = [route for route in auth_routes if route.path != "/.well-known/oauth-authorization-server"]
    auth_routes.append(Route("/.well-known/oauth-authorization-server", authorization_metadata))
    app.mount("/api/v1/mcp/oauth", OAuthBoundary(Starlette(routes=auth_routes)))
    app.router.routes.extend(
        [
            Route("/.well-known/oauth-authorization-server/api/v1/mcp/oauth", authorization_metadata),
            Route("/.well-known/oauth-protected-resource/api/v1/mcp", protected_metadata),
        ]
    )
    # Only the MCP path: a root mount would answer every unmatched REST request itself, in place of
    # the API's JSON 404/405 and slash redirects.
    app.router.routes.append(Route("/api/v1/mcp", transport))
    return server


def mcp_lifespan(app: FastAPI, server: ContentServer) -> None:
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        validate_configuration()
        async with original(application), server.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
