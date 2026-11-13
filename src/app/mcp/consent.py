"""Browser-only connection management. MCP credentials cannot authorize themselves."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from mcp.server.auth.provider import AuthorizationParams, construct_redirect_uri
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.dependencies import get_current_user, refuse_api_keys
from ..core.db.database import async_get_db
from ..core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from ..core.security import generate_secure_token, hash_token
from ..core.utils.request_context import RequestContext
from ..crud.crud_auth_audit_events import record_auth_event
from ..models.mcp import McpAudit, McpAuthorization, McpClient, McpGrant
from ..models.user import User
from ..schemas.auth_audit_event import AuthEventType

CODE_LIFETIME = timedelta(minutes=2)

router = APIRouter(prefix="/api/v1/user/mcp", tags=["connected-apps"], dependencies=[Depends(refuse_api_keys)])


class ConsentDecision(BaseModel):
    request: str = Field(
        min_length=32, max_length=128, description="The opaque authorization request shown by the consent page"
    )
    scopes: list[str] = Field(
        max_length=64, description="The requested permissions the user approves; empty denies access"
    )


async def pending_request(db: AsyncSession, secret: str, *, lock: bool = False) -> McpAuthorization:
    query = select(McpAuthorization).where(McpAuthorization.secret_hash == hash_token(secret))
    row = await db.scalar(query.with_for_update() if lock else query)
    if row is None or row.grant_id is not None or row.expires_at <= datetime.now(UTC):
        raise NotFoundException("Authorization request expired or already used.")
    return row


@router.get("/consent")
async def read_consent(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    request: Annotated[str, Query(min_length=32, max_length=128, description="The opaque authorization request")],
) -> dict[str, Any]:
    """Read the application name, exact callback and requested permissions before granting access.

    Requires a browser session. Expired or consumed requests return 404.
    """
    row = await pending_request(db, request)
    client = await db.get(McpClient, row.client_id)
    return {
        "client_id": row.client_id,
        "name": (client.metadata_json.get("client_name") if client else None) or "Unnamed application",
        "redirect_uri": row.params["redirect_uri"],
        "scopes": row.params["scopes"],
    }


@router.post("/consent")
async def decide_consent(
    request: Request,
    body: ConsentDecision,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Approve selected permissions or deny a connection, returning the validated client callback.

    Consent is single-use. Only permissions in the original request may be selected.
    The returned URL contains a short-lived authorization code, never an access token.
    """
    row = await pending_request(db, body.request, lock=True)
    params = AuthorizationParams.model_validate(row.params)
    if not set(body.scopes) <= set(params.scopes or []) or len(body.scopes) != len(set(body.scopes)):
        raise UnprocessableEntityException("Select only requested permissions.")
    await db.delete(row)
    if not body.scopes:
        redirect = construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)
    else:
        await db.scalar(select(User).where(User.id == current_user["id"]).with_for_update())
        count = await db.scalar(
            select(func.count())
            .select_from(McpGrant)
            .where(McpGrant.user_id == current_user["id"], McpGrant.revoked_at.is_(None))
        )
        if (count or 0) >= 100:
            raise UnprocessableEntityException("Revoke an unused connection before connecting another app.")
        grant = McpGrant(user_id=current_user["id"], client_id=row.client_id, scopes=" ".join(body.scopes))
        db.add(grant)
        await db.flush()
        await record_auth_event(
            db,
            event_type=AuthEventType.MCP_CONNECTION_CREATED,
            context=RequestContext.from_request(request),
            user_id=current_user["id"],
            provider="mcp",
            commit=False,
        )
        code = generate_secure_token()
        db.add(
            McpAuthorization(
                secret_hash=hash_token(code),
                client_id=row.client_id,
                params=row.params,
                expires_at=datetime.now(UTC) + CODE_LIFETIME,
                grant_id=grant.id,
            )
        )
        redirect = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
    await db.commit()
    return {"redirect_uri": redirect}


@router.get("/connections")
async def read_connections(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[dict[str, Any]]:
    """List up to 100 active connected applications, newest first, including their permissions.

    Last used is the latest authenticated MCP request, recorded at most once a minute.
    A null timestamp means no activity has been recorded for the connection.
    """
    rows = (
        await db.execute(
            select(McpGrant, McpClient)
            .join(McpClient)
            .where(McpGrant.user_id == current_user["id"], McpGrant.revoked_at.is_(None))
            .order_by(McpGrant.created_at.desc())
            .limit(100)
        )
    ).all()
    return [
        {
            "uuid": grant.uuid,
            "name": client.metadata_json.get("client_name") or "Unnamed application",
            "client_id": grant.client_id,
            "scopes": grant.scopes.split(),
            "created_at": grant.created_at,
            "last_used_at": grant.last_used_at,
        }
        for grant, client in rows
    ]


@router.delete("/connection/{uuid}")
async def revoke_connection(
    request: Request,
    uuid: UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Revoke an owned app connection and all its access and refresh tokens immediately.

    Missing connections and another user's connections both return 404.
    """
    grant = await db.scalar(
        select(McpGrant)
        .where(McpGrant.uuid == uuid, McpGrant.user_id == current_user["id"], McpGrant.revoked_at.is_(None))
        .with_for_update()
    )
    if grant is None:
        raise NotFoundException("Connection not found.")
    grant.revoked_at = datetime.now(UTC)
    await record_auth_event(
        db,
        event_type=AuthEventType.MCP_CONNECTION_REVOKED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
        provider="mcp",
        commit=False,
    )
    await db.commit()
    return {"message": "Connection revoked"}


@router.get("/activity")
async def read_activity(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[dict[str, Any]]:
    """Read the latest 100 tool calls for the caller's connections, without content or arguments."""
    rows = (
        await db.execute(
            select(McpAudit, McpGrant.uuid)
            .join(McpGrant)
            .where(McpGrant.user_id == current_user["id"])
            .order_by(McpAudit.id.desc())
            .limit(100)
        )
    ).all()
    return [
        {
            "uuid": audit.uuid,
            "connection_uuid": grant_uuid,
            "tool": audit.tool,
            "status": audit.status,
            "created_at": audit.created_at,
        }
        for audit, grant_uuid in rows
    ]
