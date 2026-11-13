"""OAuth discovery is public; content transport accepts only configured browser origins."""

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from ..core.config import settings, split_csv


class McpCorsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.public = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "MCP-Protocol-Version"],
        )
        self.transport = CORSMiddleware(
            app,
            allow_origins=[settings.frontend_origin, *split_csv(settings.MCP_ALLOWED_ORIGINS)],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["WWW-Authenticate", "MCP-Protocol-Version", "MCP-Session-Id"],
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        public = path.startswith(("/.well-known/oauth-", "/api/v1/mcp/oauth/.well-known/")) or path in {
            "/api/v1/mcp/oauth/token",
            "/api/v1/mcp/oauth/register",
            "/api/v1/mcp/oauth/revoke",
        }
        if public:
            await self.public(scope, receive, send)
        elif path == "/api/v1/mcp" or path.startswith("/api/v1/mcp/files"):
            await self.transport(scope, receive, send)
        else:
            await self.app(scope, receive, send)
