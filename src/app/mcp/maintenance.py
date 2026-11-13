"""Bound storage for expired authorization artifacts and content-free audit events."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from ..core.db.database import local_session
from ..models.mcp import McpAudit, McpAuthorization, McpClient, McpFile, McpGrant, McpToken
from ..services.blob_store import delete_after_commit


async def purge_expired_mcp_data(ctx: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    async with local_session() as db:
        # Refresh reuse must remain detectable through its original lifetime.
        await db.execute(delete(McpToken).where(McpToken.expires_at < now))
        await db.execute(delete(McpAuthorization).where(McpAuthorization.expires_at < now))
        await db.execute(delete(McpAudit).where(McpAudit.created_at < now - timedelta(days=90)))
        rows = (
            await db.scalars(
                select(McpFile).where(McpFile.expires_at < now).with_for_update(skip_locked=True).limit(1000)
            )
        ).all()
        for row in rows:
            delete_after_commit(db, row.storage_key)
            await db.delete(row)
        await db.execute(
            delete(McpClient).where(
                McpClient.created_at < now - timedelta(days=1),
                ~select(McpGrant.id).where(McpGrant.client_id == McpClient.client_id).exists(),
                ~select(McpAuthorization.secret_hash).where(McpAuthorization.client_id == McpClient.client_id).exists(),
            )
        )
        await db.commit()
