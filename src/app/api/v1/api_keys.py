"""A diver's API keys, for tools of their own - every route here is `/user/*`.

The routes that create, list and revoke them refuse an API key themselves
(`refuse_api_keys`): a key is made and revoked from a signed-in browser or app, so one that
leaks cannot mint its own successor or revoke its owner's other keys. Using a key is not here
at all; it is a branch of `get_current_user`, which is how every other route accepts one.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import CursorResult, delete
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user, refuse_api_keys
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.security import API_KEY_SHOWN_LENGTH, generate_api_key
from ...core.utils.rate_limit import enforce_rate_limit
from ...core.utils.request_context import RequestContext
from ...crud.crud_api_keys import count_api_keys, crud_api_keys
from ...crud.crud_auth_audit_events import record_auth_event
from ...models.api_key import ApiKey
from ...schemas.api_key import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyCreateInternal,
    ApiKeyRead,
    ApiKeyReadInternal,
    ApiKeyRevokedResponse,
)
from ...schemas.auth_audit_event import AuthEventType
from ...services.email_service import send_api_key_created_email, send_api_key_revoked_email, send_best_effort

router = APIRouter(tags=["api-keys"])

_NOT_FOUND = "API key not found"

# Above `API_KEY_MAX_PER_USER` for the reason `passkeys.py` gives for its own: an operator who
# lowers the cap would otherwise hide the excess rows, and a key nobody can see is a key
# nobody can revoke.
_LIST_LIMIT = 100


def _at_the_cap() -> HTTPException:
    """409 for the account's state, as `passkey_service._at_the_cap` is - and a factory for
    the reason that one gives."""
    return HTTPException(
        status_code=409,
        detail=f"You already have {settings.API_KEY_MAX_PER_USER} API keys. Revoke one to create another.",
    )


@router.post("/user/api-keys", response_model=ApiKeyCreated, status_code=201, dependencies=[Depends(refuse_api_keys)])
async def write_api_key(
    request: Request,
    body: ApiKeyCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> ApiKeyCreated:
    """Create an API key, and return its secret - in this response and nowhere else, since
    only its hash is kept.

    From a signed-in session, never an API key. The server computes `expires_at` from
    `lifetime`. 409 when the account already holds the instance's cap on keys, expired ones
    included; the `detail` names the figure. Rate limited per account. The account's email
    address is told of the new key, by its name and prefix.

    Not idempotent, and takes no `Idempotency-Key`: a replay could not return the secret
    again.
    """
    await enforce_rate_limit(
        f"api-key-create:user:{current_user['id']}",
        settings.API_KEY_CREATE_RATE_LIMIT_PER_USER,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )
    if await count_api_keys(db, user_id=current_user["id"]) >= settings.API_KEY_MAX_PER_USER:
        raise _at_the_cap()

    secret, token_hash = generate_api_key()
    created = cast(
        ApiKeyReadInternal,
        await crud_api_keys.create(
            db=db,
            object=ApiKeyCreateInternal(
                user_id=current_user["id"],
                name=body.name,
                prefix=secret[:API_KEY_SHOWN_LENGTH],
                token_hash=token_hash,
                scope=body.scope,
                expires_at=body.lifetime.expires_at(datetime.now(UTC)),
            ),
            schema_to_select=ApiKeyReadInternal,
            return_as_model=True,
            commit=False,
        ),
    )
    # The fact of the key, never the key: neither the secret nor its hash nor its prefix.
    await record_auth_event(
        db,
        event_type=AuthEventType.API_KEY_CREATED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
        provider="api_key",
        commit=False,
    )
    await db.commit()

    await send_best_effort(
        send_api_key_created_email(
            email=current_user["email"],
            key_name=created.name,
            prefix=created.prefix,
            scope=created.scope,
            expires_at=created.expires_at,
        ),
        what="the API-key-created notice",
        user_id=current_user["id"],
    )

    return ApiKeyCreated(**ApiKeyRead.model_validate(created, from_attributes=True).model_dump(), secret=secret)


@router.get("/user/api-keys", response_model=list[ApiKeyRead], dependencies=[Depends(refuse_api_keys)])
async def read_api_keys(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[ApiKeyRead]:
    """Every API key on the caller's account, oldest first, expired ones included.

    From a signed-in session, never an API key. Unpaginated, and never the secret: the
    `prefix` is what identifies a key.
    """
    # Not Redis-cached, for `GET /user/passkeys`' reason: a handful of rows that nothing
    # embeds.
    rows = await crud_api_keys.get_multi(
        db=db,
        user_id=current_user["id"],
        schema_to_select=ApiKeyRead,
        return_as_model=True,
        sort_columns="created_at",
        sort_orders="asc",
        limit=_LIST_LIMIT,
    )
    return list(rows["data"])


@router.delete("/user/api-key/{uuid}", response_model=ApiKeyRevokedResponse, dependencies=[Depends(refuse_api_keys)])
async def erase_api_key(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> ApiKeyRevokedResponse:
    """Revoke an API key: it is refused from its very next request.

    From a signed-in session, never an API key. 404 unless the caller owns it, exactly as
    for a key that does not exist. The account's email address is told of the revocation,
    by the key's name.
    """
    # Refused from the next request because every request looks the row up. A real
    # `DELETE FROM`, as a passkey's removal is, with its audit row in the same transaction.
    stored = await fetch_owned_or_raise(
        db=db,
        crud=crud_api_keys,
        uuid=uuid,
        current_user=current_user,
        schema=ApiKeyReadInternal,
        not_found_message=_NOT_FOUND,
    )

    # Core, for `erase_passkey`'s reason: a double click's second delete finds nothing and is
    # the 404 any later one would be, where FastCRUD's `delete` would answer 500.
    removed = cast(
        CursorResult,
        await db.execute(delete(ApiKey).where(ApiKey.uuid == uuid, ApiKey.user_id == current_user["id"])),
    ).rowcount
    if not removed:
        raise NotFoundException(_NOT_FOUND)

    await record_auth_event(
        db,
        event_type=AuthEventType.API_KEY_REVOKED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
        provider="api_key",
    )

    await send_best_effort(
        send_api_key_revoked_email(email=current_user["email"], key_name=stored.name),
        what="the API-key-revoked notice",
        user_id=current_user["id"],
    )

    return ApiKeyRevokedResponse()
