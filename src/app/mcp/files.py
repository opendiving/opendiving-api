"""Bounded, expiring file handles owned by both the user and the connected app."""

import hashlib
import io
import tempfile
from datetime import UTC, datetime, timedelta
from typing import IO, Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from mcp.server.auth.provider import AccessToken
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from ..core.db.database import async_get_db
from ..core.security import bearer_scheme
from ..core.utils.binary_responses import BINARY_CSP, not_modified
from ..core.utils.rate_limit import enforce_rate_limit
from ..core.utils.spooled_response import SpooledResponse
from ..core.utils.uploads import content_disposition_attachment, read_upload_within_limit, safe_filename
from ..models.mcp import McpFile, McpGrant
from ..models.user import User
from ..services import blob_store
from .oauth import provider

router = APIRouter(prefix="/api/v1/mcp/files", tags=["mcp-files"])
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_STAGED_BYTES = 50 * 1024 * 1024


async def file_access(token: Annotated[str, Depends(bearer_scheme)]) -> AccessToken:
    access = await provider.load_access_token(token)
    if access is None:
        raise HTTPException(401, "An MCP access token is required.")
    return access


async def owned_file(db: AsyncSession, handle: UUID, access: AccessToken, *, include_expired: bool = False) -> McpFile:
    row = await db.scalar(
        select(McpFile).where(
            McpFile.uuid == handle,
            McpFile.grant_id == (access.claims or {})["grant_id"],
            McpFile.user_id == int(access.subject or "0"),
            *([] if include_expired else [McpFile.expires_at > datetime.now(UTC)]),
        )
    )
    if row is None:
        raise HTTPException(404, "File handle expired or not found.")
    return row


def stale_files(now: datetime) -> ColumnElement[bool]:
    revoked = select(McpGrant.id).where(McpGrant.revoked_at.is_not(None))
    return or_(McpFile.expires_at <= now, McpFile.grant_id.in_(revoked))


def file_digest(source: IO[bytes]) -> tuple[int, str]:
    size = source.seek(0, 2)
    source.seek(0)
    digest = hashlib.sha256()
    while chunk := source.read(64 * 1024):
        digest.update(chunk)
    source.seek(0)
    return size, digest.hexdigest()


async def store_file(
    db: AsyncSession, access: AccessToken, data: bytes | IO[bytes], filename: str, content_type: str
) -> McpFile:
    if isinstance(data, bytes):
        with io.BytesIO(data) as source:
            return await store_file(db, access, source, filename, content_type)
    size, digest = await run_in_threadpool(file_digest, data)
    grant_id = (access.claims or {})["grant_id"]
    # Upload before taking the account lock, which other writes to the account wait on. A row
    # that then fails to commit leaves an orphan for `sweep_orphaned_files`.
    key = blob_store.new_key("mcp-files", sha256=digest)
    await blob_store.put_file(key, data)
    # The account lock serializes quota checks across all its connected apps.
    await db.scalar(select(User).where(User.id == int(access.subject or "0")).with_for_update())
    retired = (
        await db.scalars(
            delete(McpFile)
            .where(McpFile.user_id == int(access.subject or "0"), stale_files(datetime.now(UTC)))
            .returning(McpFile.storage_key)
        )
    ).all()
    blob_store.delete_after_commit(db, list(retired))
    used = await db.scalar(
        select(func.coalesce(func.sum(McpFile.byte_size), 0)).where(McpFile.user_id == int(access.subject or "0"))
    )
    if (used or 0) + size > MAX_STAGED_BYTES:
        await db.rollback()
        await blob_store.delete(key)
        raise HTTPException(413, "Temporary file quota exceeded. Delete unused handles.")
    row = McpFile(
        user_id=int(access.subject or "0"),
        grant_id=grant_id,
        storage_key=key,
        filename=safe_filename(filename)[:255],
        content_type=content_type[:128],
        byte_size=size,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(row)
    await db.commit()
    return row


@router.post("")
async def stage_file(
    request: Request,
    access: Annotated[AccessToken, Depends(file_access)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    file: Annotated[UploadFile, File(description="A file to stage for an MCP upload tool, at most 10 MiB")],
) -> dict[str, Any]:
    """Stage one file for an hour and return its opaque handle. Requires files:write.

    Files are private to this connected app. Each account may hold 50 MiB of temporary files.
    """
    if "files:write" not in access.scopes:
        raise HTTPException(403, "files:write permission required.")
    await enforce_rate_limit(f"mcp-stage:{access.subject}", 60, 3600)
    data = await read_upload_within_limit(file, MAX_FILE_BYTES)
    row = await store_file(db, access, data, file.filename or "upload", file.content_type or "application/octet-stream")
    return {"handle": row.uuid, "filename": row.filename, "byte_size": row.byte_size, "expires_at": row.expires_at}


@router.get("/{handle}")
async def download_file(
    request: Request,
    handle: UUID,
    access: Annotated[AccessToken, Depends(file_access)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> Response:
    """Download an owned temporary file using the same MCP token that created it.

    Returns 404 for another app's handles and expired handles. Requires files:read.
    A matching If-None-Match returns 304 after checking ownership.
    """
    if "files:read" not in access.scopes:
        raise HTTPException(403, "files:read permission required.")
    row = await owned_file(db, handle, access)
    # The key ends in the SHA-256 of the bytes as uploaded (`blob_store.new_key`).
    etag = '"' + row.storage_key.rsplit("_", 1)[-1] + '"'
    if request.headers.get("if-none-match") == etag:
        return not_modified(etag, cache_control="private, no-store")
    body = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    try:
        await blob_store.get_file(row.storage_key, body)
    except BaseException:
        body.close()
        raise
    size = body.seek(0, 2)
    body.seek(0)
    return SpooledResponse(
        body,
        media_type=row.content_type,
        headers={
            "Content-Length": str(size),
            "Content-Disposition": content_disposition_attachment(row.filename),
            "Cache-Control": "private, no-store",
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": BINARY_CSP,
        },
    )


@router.delete("/{handle}")
async def delete_file(
    handle: UUID,
    access: Annotated[AccessToken, Depends(file_access)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Discard an owned temporary file immediately. Other apps' handles return 404."""
    row = await owned_file(db, handle, access, include_expired=True)
    key = row.storage_key
    await db.delete(row)
    blob_store.delete_after_commit(db, key)
    await db.commit()
    return {"message": "File discarded"}
