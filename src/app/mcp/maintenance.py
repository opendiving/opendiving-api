"""Bound storage for expired authorization artifacts and content-free audit events."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from ..core.db.database import local_session
from ..models.mcp import McpAudit, McpAuthorization, McpClient, McpFile, McpGrant, McpToken
from ..services.blob_store import delete_after_commit
from .consent import CODE_LIFETIME
from .files import stale_files


async def purge_expired_mcp_data(ctx: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    async with local_session() as db:
        # Refresh reuse must remain detectable through its original lifetime.
        await db.execute(delete(McpToken).where(McpToken.expires_at < now))
        await db.execute(delete(McpAuthorization).where(McpAuthorization.expires_at < now))
        await db.execute(delete(McpAudit).where(McpAudit.created_at < now - timedelta(days=90)))
        rows = (
            await db.scalars(select(McpFile).where(stale_files(now)).with_for_update(skip_locked=True).limit(1000))
        ).all()
        for row in rows:
            delete_after_commit(db, row.storage_key)
            await db.delete(row)
        # Consent creates the connection; one whose code was never exchanged has no tokens, was
        # never used (so holds no files or activity) and would otherwise count toward the cap forever.
        await db.execute(
            delete(McpGrant).where(
                McpGrant.created_at < now - CODE_LIFETIME,
                McpGrant.last_used_at.is_(None),
                ~select(McpToken.secret_hash).where(McpToken.grant_id == McpGrant.id).exists(),
                ~select(McpAuthorization.secret_hash).where(McpAuthorization.grant_id == McpGrant.id).exists(),
            )
        )
        await db.execute(
            delete(McpClient).where(
                McpClient.created_at < now - timedelta(days=1),
                ~select(McpGrant.id).where(McpGrant.client_id == McpClient.client_id).exists(),
                ~select(McpAuthorization.secret_hash).where(McpAuthorization.client_id == McpClient.client_id).exists(),
            )
        )
        await db.commit()
