"""File resources describe a private download without embedding unbounded binary data."""

import json
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token

from ..core.db.database import local_session
from .dispatch import ContentServer
from .files import owned_file


def register_resources(server: ContentServer) -> None:
    @server.resource("opendiving://file/{handle}", name="temporary_file", mime_type="application/json")
    async def temporary_file(handle: str) -> str:
        access = get_access_token()
        if access is None or "files:read" not in access.scopes:
            raise ValueError("files:read permission required.")
        async with local_session() as db:
            row = await owned_file(db, UUID(handle), access)
            return json.dumps(
                {
                    "handle": str(row.uuid),
                    "filename": row.filename,
                    "byte_size": row.byte_size,
                    "expires_at": row.expires_at.isoformat(),
                    "download_url": "/api/v1/mcp/files/" + str(row.uuid),
                    "authentication": "Use this connection's MCP bearer token.",
                }
            )
