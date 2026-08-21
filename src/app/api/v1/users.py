import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
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
from ...core.utils.rate_limit import enforce_rate_limit
from ...crud.crud_authentication_requests import claim_authentication_request, crud_authentication_requests
from ...crud.crud_user_dive_stats import crud_user_dive_stats
from ...crud.crud_users import crud_users
from ...models.user import User
from ...schemas.auth import LinkCheckResponse
from ...schemas.authentication_request import AuthenticationRequestCreate, AuthenticationRequestUpdate
from ...schemas.dive import DiveActivityPoint, DiveGasUsePoint
from ...schemas.email_change import (
    EmailChangeRequest,
    EmailChangeRequestResponse,
    EmailChangeVerifyRequest,
    EmailChangeVerifyResponse,
)
from ...schemas.user import AccountDeletionResponse, UserRead, UserUpdate
from ...schemas.user_dive_stats import UserDiveStatsRead, UserDiveStatsReadInternal
from ...services.dive_activity import dive_activity
from ...services.dive_gas import gas_use_history
from ...services.email_service import (
    send_account_deletion_email,
    send_email_change_confirmation_email,
    send_email_changed_notification,
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
    """
    # Note: `email` is deliberately not part of `UserUpdate` - see
    # `POST /user/email-change/request` for how email changes work instead.
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


@router.delete("/user", response_model=AccountDeletionResponse)
async def erase_user(
    request: Request,
    response: Response,
    current_user: Annotated[dict, Depends(get_current_user)],
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

    Both presented tokens are blacklisted and the refresh cookie cleared, so the caller's
    own session dies now rather than at its natural expiry.

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
