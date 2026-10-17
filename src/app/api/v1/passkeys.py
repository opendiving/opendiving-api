"""Registering and managing the passkeys on an account - every route here is `/user/*`.

The other half of the feature, `POST /auth/passkey/options`/`verify`, is in `api.v1.auth`
beside the flow it belongs to: those are anonymous and mint a session, these need one.
The same split the email flow already has, where signing in lives in `auth.py` and
changing your address lives in `users.py` off the same `authentication_request` table.

Registration only ever happens inside an authenticated session, which is the whole reason
there is no auto-linking question anywhere in this feature: a credential is born attached
to the account that made it, and a sign-in only ever resolves credentials registration
created. There is deliberately no "sign up with a passkey" either - no `User` row exists
until `POST /auth/complete`, so there is nothing to attach one to until onboarding is
done.
"""

import uuid as uuid_pkg
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from sqlalchemy import CursorResult, delete
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.utils.rate_limit import enforce_rate_limit
from ...core.utils.request_context import RequestContext
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_webauthn_credentials import crud_webauthn_credentials
from ...models.webauthn_credential import WebauthnCredential
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.webauthn_credential import (
    PasskeyRegistrationOptions,
    PasskeyRegistrationVerifyRequest,
    WebauthnCredentialRead,
    WebauthnCredentialReadInternal,
    WebauthnCredentialUpdate,
)
from ...services.email_service import send_best_effort, send_passkey_added_email, send_passkey_removed_email
from ...services.passkey_service import finish_registration, start_registration

router = APIRouter(tags=["passkeys"])

_NOT_FOUND = "Passkey not found"

# How many rows `GET /user/passkeys` will return. Deliberately *above*
# `PASSKEY_MAX_CREDENTIALS_PER_USER` rather than equal to it: an operator who lowers that
# setting after people have registered would otherwise make the excess rows invisible in
# the UI, and a passkey nobody can see is a passkey nobody can revoke.
_LIST_LIMIT = 100


# -------------------- registration ceremony (signed in) --------------------
@router.post("/user/passkey/options", response_model=PasskeyRegistrationOptions)
async def passkey_registration_options(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> PasskeyRegistrationOptions:
    """Step 1 of adding a passkey: mint a challenge and the creation options the browser
    passes to `navigator.credentials.create()`.

    Registration only ever happens inside an authenticated session, which is what makes
    auto-linking a non-question: the credential is born attached to the account that made
    it, and nothing else can claim it later.

    One pending registration per account, so two tabs racing both fail - the second tab's
    options overwrite the challenge the first tab's verify then presents. It self-heals on
    a retry.
    """
    await enforce_rate_limit(
        f"passkey-register:user:{current_user['id']}",
        settings.PASSKEY_REGISTER_RATE_LIMIT_PER_USER,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    options = await start_registration(db=db, user=current_user)
    return PasskeyRegistrationOptions(options=options)


@router.post("/user/passkey/verify", response_model=WebauthnCredentialRead, status_code=201)
async def passkey_registration_verify(
    request: Request,
    body: PasskeyRegistrationVerifyRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> WebauthnCredentialRead:
    """Step 2: verify the attestation, store the credential, and tell the account's inbox.

    409 when the account is already at `PASSKEY_MAX_CREDENTIALS_PER_USER`, or when this
    exact credential is already registered here - which a browser honouring
    `excludeCredentials` will not produce, and one that ignores it should not be able to
    duplicate.

    `request` is here only so the audit trail can record where the passkey was added from;
    this module took no `Request` at all before that.
    """
    created = await finish_registration(
        db=db,
        user=current_user,
        credential=body.credential,
        name=body.name,
        context=RequestContext.from_request(request),
    )

    # After the commit, and never fatal: the person has just completed a biometric prompt, so a
    # relay that times out must not come back as "that failed".
    await send_best_effort(
        send_passkey_added_email(email=current_user["email"], passkey_name=created.name),
        what="the passkey-added notice",
        user_id=current_user["id"],
    )

    return WebauthnCredentialRead.model_validate(created, from_attributes=True)


# -------------------- management --------------------
@router.get("/user/passkeys", response_model=list[WebauthnCredentialRead])
async def read_passkeys(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[WebauthnCredentialRead]:
    """Every passkey on the caller's account, oldest first.

    Unpaginated and not Redis-cached, unlike every other owned resource. Registration holds
    it to `PASSKEY_MAX_CREDENTIALS_PER_USER` rows, and this reads up to `_LIST_LIMIT` so a
    lowered cap cannot strand one - either way it is a handful of rows that nothing anywhere
    embeds, so there is no invalidation obligation to get wrong. The sixth of the documented
    opt-outs on `OwnedResourceCache`.
    """
    rows = await crud_webauthn_credentials.get_multi(
        db=db,
        user_id=current_user["id"],
        schema_to_select=WebauthnCredentialRead,
        return_as_model=True,
        sort_columns="created_at",
        sort_orders="asc",
        limit=_LIST_LIMIT,
    )
    return list(rows["data"])


@router.patch("/user/passkey/{uuid}")
async def patch_passkey(
    uuid: uuid_pkg.UUID,
    values: WebauthnCredentialUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Rename a passkey. The label is the only thing about a credential a user owns -
    everything else on the row is the authenticator's to report or fixed at registration.

    404 unless the caller owns it, exactly as for a passkey that doesn't exist.
    """
    await fetch_owned_or_raise(
        db=db,
        crud=crud_webauthn_credentials,
        uuid=uuid,
        current_user=current_user,
        schema=WebauthnCredentialReadInternal,
        not_found_message=_NOT_FOUND,
    )

    if values.model_dump(exclude_unset=True):
        await crud_webauthn_credentials.update(db=db, object=values, uuid=uuid)

    return {"message": "Passkey updated"}


@router.delete("/user/passkey/{uuid}")
async def erase_passkey(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Revoke a passkey.

    A real `DELETE FROM`, not a flag: the row is what an assertion looks up, so a
    soft-deleted one is a live sign-in path one forgotten filter away from working. An
    account can remove its last passkey - the magic link is always there, and refusing
    would be inventing a lockout to prevent one.

    Revoking a credential at its owner's request is an auth event; renaming one
    (`PATCH /user/passkey/{uuid}`) deliberately is not, even though it commits to the same
    table - a label change leaves the credential's power exactly where it was.
    """
    stored = await fetch_owned_or_raise(
        db=db,
        crud=crud_webauthn_credentials,
        uuid=uuid,
        current_user=current_user,
        schema=WebauthnCredentialReadInternal,
        not_found_message=_NOT_FOUND,
    )

    # Core, and uncommitted until the event below commits it with the removal: one
    # transaction, so a removal is never left without its audit row. A second request for
    # the same passkey - a double click - blocks on this row until the first commits, then
    # deletes nothing and is the 404 any later one would be; FastCRUD's `delete` would
    # meet that as `NoResultFound` from its own `count()` and answer 500.
    removed = cast(
        CursorResult,
        await db.execute(
            delete(WebauthnCredential).where(
                WebauthnCredential.uuid == uuid, WebauthnCredential.user_id == current_user["id"]
            )
        ),
    ).rowcount
    if not removed:
        raise NotFoundException(_NOT_FOUND)

    await record_auth_event(
        db,
        event_type=AuthEventType.PASSKEY_REMOVED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
        provider="passkey",
    )

    # The delivery failure this can log is not an auth event of its own: the auth event on
    # this path is the removal above.
    await send_best_effort(
        send_passkey_removed_email(email=current_user["email"], passkey_name=stored.name),
        what="the passkey-removed notice",
        user_id=current_user["id"],
    )

    return {"message": "Passkey removed"}
