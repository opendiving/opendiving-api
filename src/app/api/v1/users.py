import logging
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Cookie, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_session_uuid, get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    BadRequestException,
    DuplicateValueException,
    NotFoundException,
    UnauthorizedException,
)
from ...core.security import blacklist_token, blacklist_tokens, generate_secure_token, hash_token, oauth2_scheme
from ...core.utils.cache import cache, delete_keys_by_pattern
from ...core.utils.client_ip import client_ip
from ...core.utils.pagination import clamp_pagination
from ...core.utils.rate_limit import enforce_rate_limit
from ...core.utils.request_context import RequestContext
from ...core.utils.uploads import content_disposition_attachment
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_authentication_requests import claim_authentication_request, crud_authentication_requests
from ...crud.crud_user_dive_stats import crud_user_dive_stats
from ...crud.crud_user_sessions import revoke_session
from ...crud.crud_users import crud_users
from ...models.user import User
from ...schemas.auth import LinkCheckResponse
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.authentication_request import AuthenticationRequestCreate, AuthenticationRequestUpdate
from ...schemas.dive import DiveActivityPoint, DiveGasUsePoint
from ...schemas.email_change import (
    EmailChangeRequest,
    EmailChangeRequestResponse,
    EmailChangeVerifyRequest,
    EmailChangeVerifyResponse,
)
from ...schemas.species import SpeciesLifeListEntry
from ...schemas.user import (
    ANCHOR_REQUIRED_MESSAGES,
    EMERGENCY_CONTACT_FIELDS,
    INSURANCE_FIELDS,
    AccountDeletionResponse,
    AvatarRead,
    UserRead,
    UserUpdate,
    lacks_its_anchor,
)
from ...schemas.user_dive_stats import UserDiveStatsRead, UserDiveStatsReadInternal
from ...services.dive_activity import dive_activity
from ...services.dive_gas import gas_use_history
from ...services.email_service import (
    send_account_deletion_email,
    send_email_change_confirmation_email,
    send_email_changed_notification,
)
from ...services.species_life_list import species_life_list
from ...services.user_avatars import (
    AVATAR_CONTENT_TYPE,
    AVATAR_FILENAME,
    MAX_AVATAR_UPLOAD_SIZE,
    UnsupportedAvatarImageError,
    delete_user_avatar,
    get_stored_avatar,
    read_avatar_bytes,
    store_user_avatar,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["user"])

# Note: there is no `POST /user` here - account creation only ever happens via
# `POST /auth/complete` (see `api.v1.auth`), after an identity (email or Google) has
# already been verified. There is no separate signup flow.

_EMAIL_CHANGE_REQUEST_RESPONSE = EmailChangeRequestResponse()


def _replay_result_or_reject(*, current_email: str, new_email: str) -> EmailChangeVerifyResponse:
    """Decide what a *second* verification of an email-change token gets back.

    Tolerated only while the change the token represents is the account's current email:
    re-applying that grants nothing, so a mail scanner detonating the link or a user
    clicking twice reports success rather than an error. A mismatch means the account has
    moved on since (a later change request), which makes this a genuine reuse.

    Shared by the two ways a caller can arrive second - reading an already-`used_at` row,
    and losing the conditional claim to a request still in flight - so the rule can't
    drift between them.
    """
    if current_email == new_email:
        return EmailChangeVerifyResponse(email=new_email)
    raise UnauthorizedException("This confirmation link has already been used.")


# Note: there is no `GET /users` here (yet) either - a public-facing listing of
# all users has the same "other users shouldn't see email" problem as a single
# lookup by uuid, and is being designed together with the eventual public-profile
# endpoint rather than left in its previous shape (which returned full `UserRead`,
# including `email`, for every user) in the meantime.


@router.get("/user", response_model=UserRead)
async def read_current_user(request: Request, current_user: Annotated[dict, Depends(get_current_user)]) -> dict:
    """Return the authenticated user's own profile.

    Served straight from the token-resolved user, so it costs no extra query. There is no
    endpoint for reading *another* user - see the note below.
    """
    return current_user


# Note: there is no `GET /user/{uuid}` here (yet) - looking up *other* users will be
# added later as a separate, public-profile-shaped endpoint (limited fields, no
# email) rather than reusing this module's current-user-only routes. Until then,
# there's no way to fetch another user's data through this API at all.


@router.patch("/user")
async def patch_user(
    request: Request,
    values: UserUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update the authenticated user's own profile.

    Email is deliberately not updatable here - changing it requires the verification
    round-trip in `POST /user/email-change/request`. Taking a username someone else
    already holds is a 422.

    A username change is rate limited per-user for the same reason `POST /auth/complete`
    is per-IP: the availability check below answers a distinguishable "Username not
    available", so unthrottled it is a wordlist oracle over who exists. The rest of the
    profile isn't limited.

    An emergency contact needs a name and an insurance needs a provider: a patch that would
    leave either holding its other fields without one is a 422 naming the missing field.
    Only a patch touching that object's fields is checked, so clearing a whole contact
    passes, and so does a units toggle on an account whose contact has no name yet.
    """
    # Note: `email` is deliberately not part of `UserUpdate` - see
    # `POST /user/email-change/request` for how email changes work instead.
    patch = values.model_dump(exclude_unset=True)
    after = {**current_user, **patch}
    unanchored = [
        fields[0]
        for fields in (EMERGENCY_CONTACT_FIELDS, INSURANCE_FIELDS)
        if patch.keys() & set(fields) and lacks_its_anchor(after, fields)
    ]
    if unanchored:
        raise RequestValidationError(
            [
                {
                    "type": "missing",
                    "loc": ("body", anchor),
                    "msg": ANCHOR_REQUIRED_MESSAGES[anchor],
                    "input": after.get(anchor),
                }
                for anchor in unanchored
            ]
        )
    if values.username is not None and values.username != current_user["username"]:
        await enforce_rate_limit(
            f"username-change:user:{current_user['id']}",
            settings.USERNAME_CHANGE_RATE_LIMIT_PER_USER,
            settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
        )
        if await crud_users.exists(db=db, username=values.username):
            raise DuplicateValueException("Username not available")

    await crud_users.update(db=db, object=values, uuid=current_user["uuid"])
    return {"message": "User updated"}


# -------------- avatar --------------
#
# Three routes rather than a field on `PATCH /user`, for the reason every stored payload
# here gets its own: the bytes live in the blob store - a volume or a bucket, on
# `FILE_STORAGE_BACKEND` - and the row carries a key, so the write has an ordering rule
# (file first, row second) and the delete has the mirror of it.
# A JSON PATCH that could null the key would leave the file behind.
#
# Self-scoped like the rest of `/user` - no uuid anywhere, so there is no ownership check
# to get backwards. Serving *another* diver's avatar is a separate, viewer-facing route
# for whenever buddies or sharing arrive; nothing renders one today.


@router.put("/user/avatar", response_model=AvatarRead)
async def write_user_avatar(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    file: Annotated[
        UploadFile,
        File(description=f"Profile picture (JPEG, PNG, WEBP or GIF, max {MAX_AVATAR_UPLOAD_SIZE // (1024 * 1024)} MB)"),
    ],
) -> AvatarRead:
    """Set or replace the caller's profile picture.

    `PUT` because this is a whole-slot idempotent replace - there is one avatar per
    account and uploading again overwrites it.

    **What comes back out is not what went in.** The image is decoded, oriented from its
    EXIF, cropped square, bounded to 512 px and re-encoded as WebP, which is what strips
    the metadata a phone photo carries - GPS included. Anything that will not decode as one
    of the four accepted formats is a 415; anything over the size limit is a 413.

    The response carries the stored image's digest, which is its version: append it to
    `GET /user/avatar` as `?v=` so a replacement lands on a URL the browser has not cached.
    """
    try:
        digest = await store_user_avatar(db=db, user_id=current_user["id"], upload=file)
    except UnsupportedAvatarImageError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    return AvatarRead(sha256=digest)


@router.get("/user/avatar")
async def read_user_avatar(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[
        str | None,
        Query(description="Opaque cache-busting version token; ignored by the server"),
    ] = None,
) -> Response:
    """Serve the caller's own avatar - one WebP, always.

    Deliberately *not* `@cache`d, for the same reason as the card download: Redis here
    holds serialized API responses, and parking binaries in it evicts what the cache is
    for. The `ETag`/`If-None-Match` pair does that job in the browser instead.

    `v` is read by nothing here, and is declared so the contract is visible rather than
    looking like a stray parameter. The response is cacheable for five minutes and the
    avatar can be *replaced* at this same URL, so the client passes the digest from
    `UserRead.avatar_sha256` and each version gets its own cache entry.

    The key and the digest come from a narrow read here rather than from `current_user`,
    which is a snapshot taken when the request began. Serving from the snapshot would let a
    replacement commit and unlink the old blob mid-request, turning a routine race into a
    `BlobMissingError` - which is a loud 500 by design, because a row naming bytes that are
    gone is data loss and answering 404 is how nobody ever investigates it.
    """
    stored = await get_stored_avatar(db=db, user_id=current_user["id"])
    if stored is None:
        raise NotFoundException("No avatar set")

    etag = f'"{stored.sha256}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=300"})

    return Response(
        content=await read_avatar_bytes(stored),
        media_type=AVATAR_CONTENT_TYPE,
        headers={
            # `attachment`, not `inline`: the clients fetch this through their API client
            # and render from a blob URL, so nothing ever navigates here directly.
            "Content-Disposition": content_disposition_attachment(AVATAR_FILENAME, default="avatar"),
            "X-Content-Type-Options": "nosniff",
            # `frame-ancestors` is spelled out because it does not fall back to
            # `default-src`: a response with its own policy opts out of
            # `SecurityHeadersMiddleware`'s default and would otherwise be framable
            # however strict the rest of this is.
            "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
            # `private` because this is one diver's picture and no shared cache should keep
            # a copy; the `ETag` makes re-validation after 5 minutes cheap.
            "Cache-Control": "private, max-age=300",
            "ETag": etag,
        },
    )


@router.delete("/user/avatar")
async def erase_user_avatar(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Remove the caller's profile picture, leaving the account itself alone.

    404 when there was nothing to remove, the shape the card-file delete established. No
    confirmation step anywhere near this: re-uploading undoes it.
    """
    if not await delete_user_avatar(db=db, user_id=current_user["id"]):
        raise NotFoundException("No avatar set")

    return {"message": "Avatar removed"}


@router.post("/user/email-change/request", response_model=EmailChangeRequestResponse)
async def request_email_change(
    request: Request,
    body: EmailChangeRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> EmailChangeRequestResponse:
    """Step 1 of changing an account's email: generates a confirmation link and
    emails it to the *new* address - the change only takes effect once that link is
    opened (`POST /user/email-change/verify`), proving the caller actually controls
    it.

    Always operates on the caller's own account (from the access token), not a path
    parameter - there is no other account to target.

    Always returns the same generic message, whether or not `new_email` already
    belongs to another account.
    """
    new_email = body.new_email.lower()
    if new_email == current_user["email"].lower():
        raise BadRequestException("That's already your email address.")

    await enforce_rate_limit(
        f"email-change:user:{current_user['id']}",
        settings.EMAIL_CHANGE_REQUEST_RATE_LIMIT_PER_USER,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"email-change:ip:{client_ip(request)}",
        settings.EMAIL_CHANGE_REQUEST_RATE_LIMIT_PER_USER * 5,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    # Only one *live* change request per user at a time, regardless of which target
    # address a previous one was for.
    pending_count = await crud_authentication_requests.count(
        db, user_id=current_user["id"], purpose="email_change", invalidated_at=None
    )
    if pending_count > 0:
        await crud_authentication_requests.update(
            db=db,
            object=AuthenticationRequestUpdate(invalidated_at=datetime.now(UTC)),
            allow_multiple=True,
            user_id=current_user["id"],
            purpose="email_change",
            invalidated_at=None,
        )

    raw_token = generate_secure_token()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES)
    await crud_authentication_requests.create(
        db=db,
        object=AuthenticationRequestCreate(
            email=new_email,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
            purpose="email_change",
            user_id=current_user["id"],
        ),
    )

    # The creation is the event; invalidating whatever was live before it (above) is
    # housekeeping of this same request and gets none of its own. The row carries the
    # account, so it sits on the 90-day tier - and no `email`, because `user_id` already
    # names the subject and the *target* address is not what this event is about.
    await record_auth_event(
        db,
        event_type=AuthEventType.EMAIL_CHANGE_REQUESTED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
    )

    confirm_url = f"{settings.FRONTEND_URL}/settings/confirm-email?token={raw_token}"
    await send_email_change_confirmation_email(new_email=new_email, confirm_url=confirm_url)

    return _EMAIL_CHANGE_REQUEST_RESPONSE


@router.get("/user/email-change/verify/check", response_model=LinkCheckResponse)
async def check_email_change_link(
    request: Request, response: Response, token: str, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> LinkCheckResponse:
    """Side-effect-free precheck used by the confirmation page before it shows the
    "Confirm email change" button - lets it show an error immediately for a link
    that's already been used, invalidated, or expired (e.g. revisited via the
    browser's back button after already confirming) rather than a misleadingly
    clickable button, and lets it display the target email up front. Never marks
    anything used or changes any state.

    Opts out of the default `public` caching for the same reason as
    `auth.check_email_link`: anonymous side-effect-free GET, but the token is in the
    query string and the body is an email address.
    """
    response.headers["Cache-Control"] = "private, no-store"

    await enforce_rate_limit(
        f"email-change-verify-check:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(db=db, token_hash=hash_token(token), purpose="email_change")
    if auth_request is None or auth_request["invalidated_at"] is not None or auth_request["used_at"] is not None:
        return LinkCheckResponse(valid=False)

    expires_at = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        return LinkCheckResponse(valid=False)

    return LinkCheckResponse(valid=True, email=auth_request["email"])


@router.post("/user/email-change/verify", response_model=EmailChangeVerifyResponse)
async def verify_email_change(
    request: Request,
    body: EmailChangeVerifyRequest,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> EmailChangeVerifyResponse:
    """Step 2: validates the confirmation link and, if it checks out, applies the
    email change. Deliberately doesn't require the caller to be signed in - the link
    may well be opened on a different device/browser than the one the change was
    requested from - the token itself, tied to a specific `user_id`, is what
    authorizes this.

    Re-opening/re-verifying the *same* link again (e.g. a mail client's
    link-preview/security-scanning feature "detonating" it before a human clicks, or
    the user clicking twice) is deliberately not an error, as long as the change it
    represents was actually applied - it's a no-op that just reports that back,
    rather than re-touching the DB or re-notifying anyone. Only an *expired* link,
    one superseded by a newer request (see `AuthenticationRequest.invalidated_at`),
    or a used token whose target *doesn't* match the account's current email (a
    genuine, rejected reuse) is an error. `check_email_change_link` is what actually
    keeps a human from re-triggering this in the first place after the first use -
    this leniency is just a safety net for races (e.g. a double click).

    Two submissions genuinely racing meet that same rule from the other side: the
    `used_at` read above can't decide it on its own, so only whoever wins the conditional
    claim (`claim_authentication_request`) applies the change, and the loser re-reads the
    account's email and takes the identical tolerated-replay-or-reject decision.
    """
    await enforce_rate_limit(
        f"email-change-verify:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(
        db=db, token_hash=hash_token(body.token), purpose="email_change"
    )
    if auth_request is None:
        raise UnauthorizedException("This confirmation link is invalid.")

    if auth_request["invalidated_at"] is not None:
        raise UnauthorizedException("This confirmation link is no longer valid - a newer request was made.")

    new_email = auth_request["email"]
    user_id = auth_request["user_id"]

    db_user = await crud_users.get(db=db, id=user_id, is_deleted=False)
    if db_user is None:
        raise NotFoundException("User not found")

    current_email = db_user["email"] if isinstance(db_user, dict) else db_user.email

    if auth_request["used_at"] is not None:
        return _replay_result_or_reject(current_email=current_email, new_email=new_email)

    expires_at = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise UnauthorizedException("This confirmation link has expired.")

    if await crud_users.exists(db=db, email=new_email):
        raise DuplicateValueException("Email is already registered to another account")

    try:
        # Claimed first, and `commit=False` throughout so the claim and the email land in
        # one transaction. Claiming first means losing the race costs nothing to undo;
        # committing separately (FastCRUD's `update` does by default) would leave a window
        # where the account's email has changed but its confirmation token is still live.
        claimed = await claim_authentication_request(db, request_id=auth_request["id"], commit=False)
        if claimed:
            await crud_users.update(db=db, object={"email": new_email}, id=user_id, commit=False)
            # In the same transaction as the claim and the address change: an event saying
            # the change was applied must not survive a rollback that undid it. Only on the
            # winning branch - a tolerated replay below re-reports a change this request did
            # not make, and recording it again would count one change twice.
            #
            # The account id comes off the claimed row rather than from a session: this
            # endpoint is anonymous by design, because the link may be opened on a
            # different device than the one that asked for the change.
            await record_auth_event(
                db,
                event_type=AuthEventType.EMAIL_CHANGE_COMPLETED,
                context=RequestContext.from_request(request),
                user_id=user_id,
                commit=False,
            )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise DuplicateValueException("Email is already registered to another account") from None

    if not claimed:
        # Another verification of this same token got there first. Unlike sign-in that
        # isn't automatically a rejection here - re-reading the account's email applies
        # the same rule as the `used_at` fast path above, so a double click that lost the
        # race still reports the change it asked for rather than erroring.
        db_user = await crud_users.get(db=db, id=user_id, is_deleted=False)
        if db_user is None:
            raise NotFoundException("User not found")
        current_email = db_user["email"] if isinstance(db_user, dict) else db_user.email
        return _replay_result_or_reject(current_email=current_email, new_email=new_email)

    await send_email_changed_notification(old_email=current_email, new_email=new_email)

    return EmailChangeVerifyResponse(email=new_email)


@router.get("/user/dive-stats", response_model=UserDiveStatsRead)
async def read_dive_stats(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> UserDiveStatsRead:
    """Return the caller's aggregate dive statistics.

    A user with no dives logged yet gets zeroed-out stats rather than a 404: every user
    conceptually has stats, the row just hasn't been created.
    """
    stats = await crud_user_dive_stats.get(
        db=db, user_id=current_user["id"], schema_to_select=UserDiveStatsReadInternal, return_as_model=True
    )
    if stats is None:
        # No dives logged yet - return zeroed-out stats rather than 404, since
        # every user conceptually has stats, they just haven't been created yet.
        # total_dives/max_depth/total_time/species_seen have Pydantic defaults, but mypy's
        # pydantic plugin doesn't recognize defaults declared via `Annotated[..., Field(default=...)]`.
        return UserDiveStatsRead(user_uuid=current_user["uuid"], created_at=datetime.now(UTC))  # type: ignore[call-arg]

    stats = cast(UserDiveStatsReadInternal, stats)
    return UserDiveStatsRead(
        **{k: v for k, v in stats.model_dump().items() if k != "user_id"}, user_uuid=current_user["uuid"]
    )


# Keyed under the `user_{id}_dives:` prefix on purpose, even though this hangs off
# `/user/...` rather than `/dives`: `invalidate_dive_caches()` already sweeps
# `user_{id}_dives:*` after every dive create, update and delete, so this series drops
# with them and needed no invalidation change at all. A key of its own (say
# `user_{id}_gas_use:`) would have been a third pattern to remember to add there, and the
# bug from forgetting is a graph that silently keeps showing yesterday's dives.
#
# Same authorization caveat as `_cached_read_dives` in `dives.py`: `@cache` serves a hit
# without re-running the route's body, so this must only ever be called with the *calling*
# user's own id, which is all the route below passes.
@cache(key_prefix="user_{user_id}_dives:gas_use_history", resource_id_name="user_id", expiration=60)
async def _cached_gas_use_history(request: Request, user_id: int, db: AsyncSession) -> list[DiveGasUsePoint]:
    """Fetches (and caches) a user's whole gas-use series. Authorization happens in the
    route before this is reached - `@cache` serves a hit without re-checking it.
    """
    return await gas_use_history(db=db, user_id=user_id)


@router.get("/user/gas-use-history", response_model=list[DiveGasUsePoint])
async def read_gas_use_history(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[DiveGasUsePoint]:
    """Every dive of the caller's that records enough to derive its gas use, oldest first.

    The whole series rather than a page of it: this exists to be plotted as a trend, and
    a trend needs the whole career. Dives that can't produce a figure are simply absent -
    it's a chart series, not a checklist, and `GET /dive/{uuid}` is where a diver finds
    out why one of theirs is missing.

    Always the caller's own account, like `/user/dive-stats` - no uuid parameter.
    """
    return await _cached_gas_use_history(request, user_id=current_user["id"], db=db)


# Keyed under `user_{id}_dives:` for the same reason as `_cached_gas_use_history` above:
# `invalidate_dive_caches()` already sweeps that prefix after every dive write, so this
# series drops with them and needs no invalidation change of its own. The same
# authorization caveat applies - `@cache` serves a hit without re-running the route body,
# so this must only ever be called with the calling user's own id.
@cache(key_prefix="user_{user_id}_dives:dive_activity", resource_id_name="user_id", expiration=60)
async def _cached_dive_activity(request: Request, user_id: int, db: AsyncSession) -> list[DiveActivityPoint]:
    """Fetches (and caches) a user's dives-per-day series. Authorization happens in the
    route below, before this is reached.
    """
    return await dive_activity(db=db, user_id=user_id)


@router.get("/user/dive-activity", response_model=list[DiveActivityPoint])
async def read_dive_activity(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[DiveActivityPoint]:
    """How many dives the caller logged on each calendar day, oldest first.

    Days without diving are absent, not zeroed - the client draws a fixed grid of days,
    months or years and fills the gaps itself. Counts are bucketed by each dive's *own*
    local day, so a dive keeps the day it was logged on wherever it's being read from.

    Days rather than months because the client windows this one series three ways, and
    sums the finer buckets into the coarser ones itself (see `DiveActivityPoint`).

    The whole series rather than a page of it, like `/user/gas-use-history`: it exists to
    be plotted, one small object per day with diving in it. Always the caller's own
    account - no uuid parameter.
    """
    return await _cached_dive_activity(request, user_id=current_user["id"], db=db)


# Keyed under `user_{id}_dives:` for the same reason as the two series above, and with a
# second obligation they do not have: **every query parameter has to be in the key**.
# `@cache` builds its key from this template's placeholders and the resource id and nothing
# else, so a prefix missing `page` and `search` would collapse every page *and* every search of
# one diver onto a single entry - page 2 serving page 1, and a search serving the unfiltered
# list, for sixty seconds at a time. Nothing downstream catches that: `TestListCacheKeys`
# asserts `page_{page}` only for `OwnedResourceCache` instances, and this is deliberately a
# hand-written aggregate. The shape is `_cached_read_dives`', not `_cached_gas_use_history`',
# which is an unparameterised whole-series fetch with no page to vary by.
#
# The prefix is what makes this drop with the rest: `invalidate_dive_caches()` sweeps
# `user_{id}_dives:*` after every dive create, update and delete, and deliberately not a wider
# pattern. A namespace of its own would have been a third pattern to remember to add there, and
# the bug from forgetting it is specific here - `species_seen` is uncached and drops instantly
# on a dive delete, so the dashboard tile and this list would disagree, breaking the very
# equality that makes the two checkable against each other.
#
# Same authorization caveat as everywhere in this file: `@cache` serves a hit without re-running
# the body, so this must only ever be called with the calling user's own id.
# Named rather than written inline so a test can assert on it directly. That is worth the one
# extra line here and nowhere else in this file: this is the key whose omissions produce a
# wrong answer rather than a slow one, and every other way of checking it either reads this
# file's source text or passes whenever it happens to run outside the 60 s TTL.
SPECIES_LIFE_LIST_CACHE_KEY_PREFIX = (
    "user_{user_id}_dives:species:page_{page}:items_per_page:{items_per_page}:search:{search}"
)


@cache(key_prefix=SPECIES_LIFE_LIST_CACHE_KEY_PREFIX, resource_id_name="user_id", expiration=60)
async def _cached_species_life_list(
    request: Request, user_id: int, db: AsyncSession, page: int, items_per_page: int, search: str | None
) -> dict:
    """Fetches (and caches) a page of the caller's life list. Authorization happens in the
    route before this is reached.
    """
    data = await species_life_list(
        db=db, user_id=user_id, offset=compute_offset(page, items_per_page), limit=items_per_page, search=search
    )
    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get("/user/species", response_model=PaginatedListResponse[SpeciesLifeListEntry])
async def read_species_life_list(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Match by any name the species goes by, as the catalog search does"),
    ] = None,
) -> dict:
    """Every species the caller has ever logged, most recently seen first.

    One row per taxon rather than per sighting: `dive_count`, `first_seen` and `last_seen` are
    that diver's whole history with it, and `photo_sha256` is non-null when there is a photo to
    render (build `/species/{uuid}/photo?v=<digest>` from it - that route needs no token).

    Counts **live** dives only, so soft-deleting a dive drops its species from this list when it
    was the only sighting - which is also why `total_count` equals the `species_seen` on
    `GET /user/dive-stats` for the same account.

    Always the caller's own account, like the rest of `/user/...` - no uuid parameter, so there
    is no ownership check to get backwards. Out-of-range pagination is clamped, not rejected.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)
    # Normalized here rather than in the service, so the shape the cache key is built from is
    # the shape this route accepted - the reasoning `read_species_search` spells out for its
    # own search parameter. An all-whitespace term becomes `None` rather than a `%%` pattern
    # that matches the unfiltered list under a different key.
    normalized = " ".join(search.split()) if search is not None else None
    return await _cached_species_life_list(
        request,
        user_id=current_user["id"],
        db=db,
        page=page,
        items_per_page=items_per_page,
        search=normalized or None,
    )


@router.delete("/user", response_model=AccountDeletionResponse)
async def erase_user(
    request: Request,
    response: Response,
    current_user: Annotated[dict, Depends(get_current_user)],
    session_uuid: Annotated[uuid_pkg.UUID | None, Depends(current_session_uuid)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    access_token: str = Depends(oauth2_scheme),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
) -> AccountDeletionResponse:
    """Delete the authenticated user's own account, reversibly for `ACCOUNT_DELETION_GRACE_DAYS`.

    The account goes **dark immediately** and stays deleted until the grace period runs
    out, at which point `purge_deleted_accounts` issues a real `DELETE FROM "user"` and
    every row and stored file the account owns goes with it. There is no state in between
    where the app keeps working: `get_current_user` filters `is_deleted=False`, so every
    read 401s from this instant, and `/auth/refresh` re-resolves the row so the other
    devices' cookies stop rotating too. Someone deleting their account because they want
    to be *gone from it* gets that, not a fortnight's countdown banner.

    Both presented tokens are blacklisted, the caller's **own** session row is stamped
    `revoked_at`, and the refresh cookie is cleared - so the caller's own session dies now
    rather than at its natural expiry.

    **No other session row is touched, and that is load-bearing rather than an oversight.**
    The other devices are already inert: `get_current_user` filters `is_deleted=False` and
    `/auth/refresh` re-resolves the account, so every one of them 401s from this instant.
    Leaving them *unrevoked* is what lets `POST /auth/restore` bring the account back with
    those sessions intact - "a restore that silently signed every other device out would be
    a worse answer", which is exactly the property the refresh check was written to
    preserve. Somebody who wants them gone has `DELETE /user/sessions` for it.

    The response carries `purge_after` and is composed **before** the confirmation email is
    attempted, so a dead relay costs the copy rather than the date.
    """
    await enforce_rate_limit(
        f"account-deletion:user:{current_user['id']}",
        settings.ACCOUNT_DELETION_RATE_LIMIT_PER_USER,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    # Conditional on the row actually transitioning, not on what `get_current_user`
    # resolved. Two concurrent calls from one live session both pass that dependency
    # before either commits, and an unconditional write would rewrite `deleted_at` - which
    # is the deletion clock - and send a second email. `RETURNING` is what makes "did this
    # call do it?" one statement rather than a check-then-act with the same race in it.
    now = datetime.now(UTC)
    deleted_at = cast(
        datetime | None,
        (
            await db.execute(
                update(User)
                .where(User.id == current_user["id"], User.is_deleted.is_(False))
                .values(is_deleted=True, deleted_at=now)
                .returning(User.deleted_at)
            )
        ).scalar_one_or_none(),
    )
    newly_deleted = deleted_at is not None
    if deleted_at is None:
        # Already pending: report the deadline the *first* request set, so a double submit
        # and a retry both answer with the same date.
        deleted_at = cast(
            datetime | None,
            (await db.execute(select(User.deleted_at).where(User.id == current_user["id"]))).scalar_one_or_none(),
        )
    await db.commit()

    if refresh_token:
        await blacklist_tokens(access_token=access_token, refresh_token=refresh_token, db=db)
        response.delete_cookie(key="refresh_token")
    else:
        await blacklist_token(token=access_token, db=db)

    if session_uuid is not None:
        await revoke_session(db, session_uuid=session_uuid)

    # Commits the revoke above with it. `newly_deleted` is not a condition on the event:
    # the caller asked to delete their account either way, and a repeated request is a
    # thing the trail should show rather than hide.
    await record_auth_event(
        db,
        event_type=AuthEventType.ACCOUNT_DELETION_REQUESTED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
    )

    # Null only if the row is not there to read - it was purged between `get_current_user`
    # resolving it and this statement - or if something flagged it without setting the
    # clock, the never-purge state `purge_deleted_accounts` warns about. Neither is worth
    # 500ing at somebody who is trying to leave, so answer with a date derived from now.
    purge_after = (deleted_at or now) + timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS)

    # Hygiene rather than correctness: every read 401s from this instant regardless, and
    # user ids are sequential and never reused, so nothing can be served out of these. The
    # uuid-keyed `trip_cache:{uuid}` / `dive_site_cache:{uuid}` entries are not swept -
    # they are not user-scoped, and are unreachable for the same reason.
    await delete_keys_by_pattern(f"user_{current_user['id']}_*")

    if newly_deleted:
        # Last, and never fatal. The deletion has already committed; a relay failure that
        # propagated would leave the user locked out *and* never told the purge date,
        # which is strictly worse than no email. `send_gear_service_digests` makes the
        # same trade the other way round, for a job that can safely repeat itself.
        try:
            await send_account_deletion_email(current_user["email"], purge_after)
        except Exception:
            logger.exception("Could not send the account-deletion confirmation for user %s", current_user["id"])

    return AccountDeletionResponse(message="User deleted", purge_after=purge_after)
