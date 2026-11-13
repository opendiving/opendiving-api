"""OAuth persistence for the SDK's authorization-code and PKCE handlers.

Each consent creates a token family. Row locks serialize rotation and revocation across
workers. Reusing any spent refresh token revokes its entire family, including successors.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db.database import local_session
from ..core.security import generate_secure_token, hash_token
from ..crud.crud_users import read_account
from ..models.mcp import McpAuthorization, McpClient, McpGrant, McpToken
from ..models.user import User
from .permissions import DEFAULT_SCOPES, SCOPES, validate_scopes
from .urls import resource_url, valid_redirect

ACCESS_SECONDS = 900
REFRESH_SECONDS = 30 * 86400
# A busy client records activity at most once a minute, including read-only requests.
LAST_USED_GRANULARITY = timedelta(minutes=1)


async def issue_tokens(db: AsyncSession, grant: McpGrant, scopes: list[str]) -> OAuthToken:
    now = datetime.now(UTC)
    access, refresh = generate_secure_token(), generate_secure_token()
    for secret, kind, seconds in ((access, "access", ACCESS_SECONDS), (refresh, "refresh", REFRESH_SECONDS)):
        db.add(
            McpToken(
                secret_hash=hash_token(secret),
                grant_id=grant.id,
                kind=kind,
                scopes=" ".join(scopes),
                expires_at=now + timedelta(seconds=seconds),
            )
        )
    return OAuthToken(access_token=access, refresh_token=refresh, expires_in=ACCESS_SECONDS, scope=" ".join(scopes))


async def live_grant(db: AsyncSession, grant_id: int, *, lock: bool = False) -> McpGrant | None:
    query = select(McpGrant).where(McpGrant.id == grant_id, McpGrant.revoked_at.is_(None))
    if lock:
        query = query.with_for_update()
    grant = await db.scalar(query)
    if grant is None:
        return None
    owner = await db.scalar(select(User.uuid).where(User.id == grant.user_id, User.is_deleted.is_(False)))
    return grant if owner else None


class RegisteredClient(OAuthClientInformationFull):
    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        # RFC 8252 permits ephemeral ports on native loopback IP callbacks only.
        if redirect_uri is not None and self.application_type == "native":
            for registered in self.redirect_uris or []:
                if (
                    redirect_uri.scheme == registered.scheme == "http"
                    and redirect_uri.host == registered.host
                    and registered.host in {"127.0.0.1", "[::1]", "::1"}
                    and redirect_uri.path == registered.path
                    and redirect_uri.query == registered.query
                    and redirect_uri.fragment == registered.fragment
                    and redirect_uri.username == registered.username
                    and redirect_uri.password == registered.password
                ):
                    return redirect_uri
        return super().validate_redirect_uri(redirect_uri)


class OpenDivingOAuth(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with local_session() as db:
            client = await db.get(McpClient, client_id)
            if client and (
                not client_id.startswith("https://") or client.created_at > datetime.now(UTC) - timedelta(hours=1)
            ):
                return RegisteredClient.model_validate(client.metadata_json)
        if not client_id.startswith("https://"):
            return None
        from .client_metadata import fetch_client_metadata

        metadata = await fetch_client_metadata(client_id)
        if metadata is None:
            return None
        if metadata.scope is None:
            metadata.scope = " ".join(sorted(SCOPES))
        try:
            await self.register_client(metadata)
        except RegistrationError:
            return None
        return RegisteredClient.model_validate(metadata.model_dump())

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Only public clients: there is no recoverable client secret in this database.
        if (
            client_info.token_endpoint_auth_method != "none"
            or client_info.client_secret
            or not client_info.redirect_uris
            or len(client_info.redirect_uris) > 10
            or any(not valid_redirect(str(uri)) for uri in client_info.redirect_uris)
            or not set(client_info.grant_types) <= {"authorization_code", "refresh_token"}
            or client_info.response_types != ["code"]
            or not set((client_info.scope or "").split()) <= SCOPES
        ):
            raise RegistrationError(
                "invalid_client_metadata", "Use a public authorization-code client with HTTPS or loopback redirects."
            )
        async with local_session() as db:
            now = datetime.now(UTC)
            statement = insert(McpClient).values(
                client_id=client_info.client_id, metadata_json=client_info.model_dump(mode="json"), created_at=now
            )
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[McpClient.client_id],
                    set_={"metadata_json": statement.excluded.metadata_json, "created_at": now},
                )
            )
            await db.commit()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        scopes = params.scopes or DEFAULT_SCOPES
        if not validate_scopes(scopes):
            raise AuthorizeError("invalid_scope", "Unknown or empty permissions.")
        if params.resource != resource_url():
            raise AuthorizeError("invalid_target", "The resource must identify this MCP endpoint.")
        if len(params.code_challenge) != 43:
            raise AuthorizeError("invalid_request", "A PKCE S256 challenge is required.")
        params.scopes = scopes
        secret = generate_secure_token()
        async with local_session() as db:
            # Pending requests are short lived; avoid retaining expired browser requests.
            await db.execute(delete(McpAuthorization).where(McpAuthorization.expires_at < datetime.now(UTC)))
            db.add(
                McpAuthorization(
                    secret_hash=hash_token(secret),
                    client_id=client.client_id,
                    params=params.model_dump(mode="json"),
                    expires_at=datetime.now(UTC) + timedelta(minutes=10),
                )
            )
            await db.commit()
        from ..core.config import settings

        return settings.frontend_origin + "/mcp/authorize?request=" + secret

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        async with local_session() as db:
            row = await db.get(McpAuthorization, hash_token(authorization_code))
            if row is None or row.client_id != client.client_id or row.grant_id is None:
                return None
            grant = await live_grant(db, row.grant_id)
            if grant is None:
                return None
            params = AuthorizationParams.model_validate(row.params)
            return AuthorizationCode(
                code=authorization_code,
                client_id=row.client_id,
                expires_at=row.expires_at.timestamp(),
                scopes=grant.scopes.split(),
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=resource_url(),
                subject=str(grant.user_id),
            )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        async with local_session() as db:
            row = await db.scalar(
                select(McpAuthorization)
                .where(McpAuthorization.secret_hash == hash_token(authorization_code.code))
                .with_for_update()
            )
            if (
                row is None
                or row.client_id != client.client_id
                or row.grant_id is None
                or row.expires_at <= datetime.now(UTC)
            ):
                raise TokenError("invalid_grant", "Code is expired or already used.")
            grant = await live_grant(db, row.grant_id, lock=True)
            if grant is None:
                raise TokenError("invalid_grant")
            await db.delete(row)
            tokens = await issue_tokens(db, grant, grant.scopes.split())
            await db.commit()
            return tokens

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        async with local_session() as db:
            row = await db.get(McpToken, hash_token(refresh_token))
            if row is None or row.kind != "refresh":
                return None
            grant = await live_grant(db, row.grant_id)
            if grant is None or grant.client_id != client.client_id:
                return None
            # Keep spent tokens readable so exchange detects reuse and revokes successors.
            return RefreshToken(
                token=refresh_token,
                client_id=client.client_id,
                scopes=row.scopes.split(),
                expires_at=int(row.expires_at.timestamp()),
                resource=resource_url(),
                subject=str(grant.user_id),
            )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        async with local_session() as db:
            row = await db.get(McpToken, hash_token(refresh_token.token))
            if row is None or row.kind != "refresh":
                raise TokenError("invalid_grant")
            grant = await live_grant(db, row.grant_id, lock=True)
            # Re-read after the family lock; another worker may have rotated it.
            await db.refresh(row)
            if grant is None or grant.client_id != client.client_id:
                raise TokenError("invalid_grant")
            now = datetime.now(UTC)
            if row.spent_at is not None:
                grant.revoked_at = now
                await db.commit()
                raise TokenError("invalid_grant", "Refresh token reuse revoked this connection.")
            if row.expires_at <= now or not set(scopes) <= set(row.scopes.split()) & set(grant.scopes.split()):
                raise TokenError("invalid_grant")
            row.spent_at = now
            tokens = await issue_tokens(db, grant, scopes)
            await db.commit()
            return tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with local_session() as db:
            now = datetime.now(UTC)
            row = await db.get(McpToken, hash_token(token))
            if row is None or row.kind != "access" or row.expires_at <= now:
                return None
            grant = await live_grant(db, row.grant_id)
            if grant is None:
                return None
            cutoff = now - LAST_USED_GRANULARITY
            if grant.last_used_at is None or grant.last_used_at <= cutoff:
                await db.execute(
                    update(McpGrant)
                    .where(
                        McpGrant.id == grant.id,
                        McpGrant.revoked_at.is_(None),
                        or_(McpGrant.last_used_at.is_(None), McpGrant.last_used_at <= cutoff),
                    )
                    .values(last_used_at=now)
                )
                await db.commit()
            return AccessToken(
                token=token,
                client_id=grant.client_id,
                scopes=row.scopes.split(),
                expires_at=int(row.expires_at.timestamp()),
                resource=resource_url(),
                subject=str(grant.user_id),
                claims={"grant_id": grant.id},
            )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with local_session() as db:
            row = await db.get(McpToken, hash_token(token.token))
            if row:
                grant = await live_grant(db, row.grant_id, lock=True)
                if grant and grant.client_id == token.client_id:
                    grant.revoked_at = datetime.now(UTC)
                    await db.commit()

    async def account(self, access: AccessToken) -> dict[str, Any] | None:
        async with local_session() as db:
            owner_uuid = await db.scalar(select(User.uuid).where(User.id == int(access.subject or "0")))
            account = await read_account(db, uuid=uuid.UUID(str(owner_uuid))) if owner_uuid else None
            return {**account, "is_superuser": False} if account else None


provider = OpenDivingOAuth()
