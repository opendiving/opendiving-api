"""Unified authentication & registration flow, plus session refresh/teardown.

The single entry point into the app: a caller either proves ownership of an email
address (the magic link, or the six-digit code printed beside it in the same email) or
authenticates with Google, and *only then* do we ask "does an account already exist for
this identity?" (`resolve_identity`, in `services.auth_service`).
If so, they're signed in immediately. If not, a temporary onboarding session is issued
and no `User` row is created until profile completion (`POST /auth/complete`) succeeds -
there are no unverified users, and there is no separate sign up flow.

`POST /auth/refresh`/`POST /auth/logout` also live here (see `DECISIONS.md`) - they
used to sit in their own `login.py`/`logout.py` modules under a stale `"login"` tag,
left over from the old password-based flow.
"""

import hmac
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Request, Response
from jose import JWTError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, UnauthorizedException
from ...core.schemas import OnboardingTokenData
from ...core.security import (
    TokenType,
    blacklist_token,
    blacklist_tokens,
    create_onboarding_token,
    generate_secure_token,
    generate_sign_in_code,
    hash_sign_in_code,
    hash_token,
    oauth2_scheme,
    revocation_time,
    token_subject,
    verify_google_id_token,
    verify_onboarding_token,
    verify_token,
)
from ...core.utils.client_ip import client_ip
from ...core.utils.rate_limit import enforce_rate_limit
from ...crud.crud_authentication_providers import crud_authentication_providers
from ...crud.crud_authentication_requests import (
    claim_authentication_request,
    crud_authentication_requests,
    register_failed_code_attempt,
)
from ...crud.crud_users import crud_users
from ...schemas.auth import (
    AuthOutcome,
    EmailAuthRequest,
    EmailAuthRequestResponse,
    EmailCodeVerifyRequest,
    EmailVerifyRequest,
    GoogleAuthRequest,
    LinkCheckResponse,
    ProfileCompletionRequest,
)
from ...schemas.authentication_provider import AuthenticationProviderCreate
from ...schemas.authentication_request import (
    AuthenticationRequestCreate,
    AuthenticationRequestRead,
    AuthenticationRequestUpdate,
)
from ...schemas.user import UserCreateInternal, UserReadInternal
from ...services.auth_service import AuthenticatedUser, OnboardingRequired, issue_tokens, resolve_identity
from ...services.email_service import send_magic_link_email

router = APIRouter(prefix="/auth", tags=["auth"])

logger = logging.getLogger(__name__)

# The one thing `POST /auth/email/verify-code` ever says about a code it won't accept.
# Wrong digits, a `request_id` naming no row, an expired or superseded request, and a code
# whose attempts ran out are all this sentence, so that nothing about the row leaks to a
# caller who has not already proven they are the browser that asked for it.
_CODE_REJECTED = "This code is invalid or has expired."


def _has_expired(auth_request: dict[str, Any]) -> bool:
    """Whether a request's `expires_at` is in the past, tolerating a naive timestamp.

    The column is `TIMESTAMPTZ` and asyncpg hands back an aware `datetime`, but a row read
    through a driver or a test double that doesn't is a `TypeError` on the comparison
    rather than a wrong answer - so the coercion is here, once, instead of at each of the
    three sites that ask the question.
    """
    expires_at: datetime = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at < datetime.now(UTC)


async def _start_onboarding_or_sign_in(
    response: Response, outcome: AuthenticatedUser | OnboardingRequired
) -> AuthOutcome:
    """Turn a verified identity into either a signed-in session or an onboarding handoff.

    Shared by every entry point that proves who someone is (magic link, Google), because
    each of them faces the same fork: a `User` row already exists for this identity, or it
    doesn't and one has to be created by `POST /auth/complete`. In the second case no user
    is created here - the caller gets a short-lived onboarding token carrying the verified
    email and profile, which is the only thing that lets `/auth/complete` trust them.
    """
    if isinstance(outcome, AuthenticatedUser):
        tokens = await issue_tokens(response, outcome.user["uuid"])
        return AuthOutcome(status="authenticated", **tokens)

    onboarding_token = await create_onboarding_token(
        OnboardingTokenData(
            email=outcome.email,
            provider=outcome.provider,
            provider_user_id=outcome.provider_user_id,
            name=outcome.name,
            avatar=outcome.avatar,
        )
    )
    return AuthOutcome(
        status="onboarding_required",
        onboarding_token=onboarding_token,
        email=outcome.email,
        name=outcome.name,
        avatar=outcome.avatar,
    )


@router.post("/email/request", response_model=EmailAuthRequestResponse)
async def request_email_link(
    request: Request, body: EmailAuthRequest, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> EmailAuthRequestResponse:
    """Step 1 of the email flow: generates a magic-link token and a six-digit code, and
    emails both.

    Always returns the same generic message, whether or not `email` belongs to an
    existing account - this must never be used to check if someone has signed up.

    The `request_id` in the response is the row's public uuid, and it is handed back for
    one reason: it is the only way to reach `POST /auth/email/verify-code`, which has no
    email-keyed lookup at all. That keeps the code's attempt budget spendable *only* by
    the browser that asked for it - a stranger who knows an address cannot burn a diver's
    code, let alone their link. It is not an oracle either way: a row is minted for every
    address, so the id is a fresh random value whether or not an account exists.
    """
    email = body.email.lower()

    await enforce_rate_limit(
        f"auth:email-request:email:{email}",
        settings.MAGIC_LINK_REQUEST_RATE_LIMIT_PER_EMAIL,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"auth:email-request:ip:{client_ip(request)}",
        settings.MAGIC_LINK_REQUEST_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    # Only one *live* link per email: invalidate any previous, still-live request(s)
    # rather than leaving them valid alongside the new one. FastCRUD's `update(...,
    # allow_multiple=True)` raises `NoResultFound` when zero rows match (the common
    # case - most emails won't have one), so check first rather than treating
    # "nothing to invalidate" as an error (mirrors the token-blacklist purge job's
    # `count()`-before-`delete()` pattern in `core.worker.functions`).
    pending_count = await crud_authentication_requests.count(db, email=email, purpose="sign_in", invalidated_at=None)
    if pending_count > 0:
        await crud_authentication_requests.update(
            db=db,
            object=AuthenticationRequestUpdate(invalidated_at=datetime.now(UTC)),
            allow_multiple=True,
            email=email,
            purpose="sign_in",
            invalidated_at=None,
        )

    raw_token = generate_secure_token()
    code = generate_sign_in_code()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.MAGIC_LINK_TOKEN_EXPIRE_MINUTES)
    created = await crud_authentication_requests.create(
        db=db,
        object=AuthenticationRequestCreate(
            email=email,
            token_hash=hash_token(raw_token),
            code_hash=hash_sign_in_code(code),
            expires_at=expires_at,
            purpose="sign_in",
        ),
        schema_to_select=AuthenticationRequestRead,
        return_as_model=True,
    )

    magic_link_url = f"{settings.FRONTEND_URL}/auth/verify?token={raw_token}"
    await send_magic_link_email(email=email, magic_link_url=magic_link_url, code=code)

    return EmailAuthRequestResponse(request_id=created.uuid)


@router.get("/email/verify/check", response_model=LinkCheckResponse)
async def check_email_link(
    request: Request, response: Response, token: str, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> LinkCheckResponse:
    """Side-effect-free precheck used by the sign-in landing page before it shows the
    "Sign in" button - lets it show an error immediately for a link that's already
    been used, invalidated, or expired (e.g. revisited via the browser's back
    button after already signing in) rather than a misleadingly clickable button,
    and lets it display which email it's about to sign in as. Never marks anything
    used or changes any state.

    The only GET in this module that must not be publicly cached. It is anonymous and
    side-effect-free, which is exactly the shape `ClientCacheMiddleware` marks
    `public, max-age=60` - but the magic-link token sits in the query string and the
    response body is the account's email address, so a shared cache keyed on that URL
    would hand both to whoever asked next. Setting the header here stops the middleware
    from filling one in.
    """
    response.headers["Cache-Control"] = "private, no-store"

    await enforce_rate_limit(
        f"auth:email-verify-check:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(db=db, token_hash=hash_token(token), purpose="sign_in")
    if auth_request is None or auth_request["invalidated_at"] is not None or auth_request["used_at"] is not None:
        return LinkCheckResponse(valid=False)

    if _has_expired(auth_request):
        return LinkCheckResponse(valid=False)

    return LinkCheckResponse(valid=True, email=auth_request["email"])


@router.post("/email/verify", response_model=AuthOutcome)
async def verify_email_link(
    request: Request,
    body: EmailVerifyRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Step 2 of the email flow: validates the magic-link token and either signs the
    caller in (existing account) or hands back an onboarding session (new one).

    The token is strictly single-use, as the email that carries it promises: an
    already-`used_at` link is rejected, alongside one that expired or was superseded
    by a newer request (see `request_email_link` and
    `AuthenticationRequest.invalidated_at`). Replay used to be allowed here on the
    grounds that `resolve_identity` is a pure lookup - true, but beside the point,
    since the side effect that matters is `_start_onboarding_or_sign_in` minting a
    fresh refresh cookie good for `REFRESH_TOKEN_EXPIRE_DAYS`. Anyone who reads the
    mail after the recipient has clicked it got a brand-new week-long session.
    """
    await enforce_rate_limit(
        f"auth:email-verify:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(db=db, token_hash=hash_token(body.token), purpose="sign_in")
    if auth_request is None:
        raise UnauthorizedException("This sign-in link is invalid.")

    # Ordered most-specific-reason-first, matching `check_email_link`: superseded
    # beats used, used beats expired, so the caller is told the one thing that is
    # most useful to know about their link.
    if auth_request["invalidated_at"] is not None:
        raise UnauthorizedException("This sign-in link is no longer valid - a newer one was requested.")

    if auth_request["used_at"] is not None:
        raise UnauthorizedException("This sign-in link has already been used.")

    if _has_expired(auth_request):
        raise UnauthorizedException("This sign-in link has expired.")

    # The authoritative single-use gate, sitting immediately before the session gets
    # minted: the `used_at` check above is a read, and a read plus a later write is two
    # statements two concurrent submissions can both walk through. See
    # `claim_authentication_request` for why this can't be a filtered FastCRUD `update`.
    if not await claim_authentication_request(db, request_id=auth_request["id"]):
        raise UnauthorizedException("This sign-in link has already been used.")

    outcome = await resolve_identity(db=db, provider="email", email=auth_request["email"])
    return await _start_onboarding_or_sign_in(response, outcome)


@router.post("/email/verify-code", response_model=AuthOutcome)
async def verify_email_code(
    request: Request,
    body: EmailCodeVerifyRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """The other half of step 2: the six-digit code from the sign-in email, typed back
    into the tab that asked for it. Signs the caller in, or hands back an onboarding
    session, exactly as the link does.

    This exists because a link signs in *the device that opens it*, and mail is very often
    read somewhere else - the one magic-link failure the explicit-click precheck never
    addressed. Both credentials ride the same row and end at the same
    `claim_authentication_request`, so a link and a code racing on one request resolve the
    way two links do: one wins, the other is told it has already been used.

    **`request_id` is not decoration.** There is deliberately no lookup by email here. The
    id was handed only to the browser that made the request, so the only party who can
    spend this code's attempts is the one who asked for it - and an earlier design without
    it let anyone who knew an address fire five wrong guesses at whatever request the
    victim had live, which, if a burnt code took its link with it, is sign-in denial aimed
    at exactly the accounts (email-only, self-hosted) whose recovery path is that inbox. It
    also removes a genuine nondeterminism: two tabs can each leave a live row for the same
    address, and an `(email, live)` lookup would resolve them by an unordered `.first()`.

    Every rejection is the same sentence (`_CODE_REJECTED`), including the case where the
    `request_id` names nothing at all.
    """
    await enforce_rate_limit(
        f"auth:email-verify-code:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(db=db, uuid=body.request_id, purpose="sign_in")
    if (
        auth_request is None
        or auth_request["code_hash"] is None
        or auth_request["invalidated_at"] is not None
        or auth_request["used_at"] is not None
        or _has_expired(auth_request)
    ):
        raise UnauthorizedException(_CODE_REJECTED)

    if not hmac.compare_digest(auth_request["code_hash"], hash_sign_in_code(body.code)):
        # Charged before the 401 is raised, and atomically - the cap is the whole defense
        # of a six-digit secret, so it must not be walkable by firing guesses in parallel.
        await register_failed_code_attempt(
            db, request_id=auth_request["id"], max_attempts=settings.SIGN_IN_CODE_ATTEMPTS_MAX
        )
        raise UnauthorizedException(_CODE_REJECTED)

    # The authoritative single-use gate, same as the link's - see
    # `claim_authentication_request`. A correct code that loses this race lost to the link
    # in its own email, or to a second tab, and either way a session was already issued.
    if not await claim_authentication_request(db, request_id=auth_request["id"]):
        raise UnauthorizedException(_CODE_REJECTED)

    outcome = await resolve_identity(db=db, provider="email", email=auth_request["email"])
    return await _start_onboarding_or_sign_in(response, outcome)


@router.post("/google", response_model=AuthOutcome)
async def auth_with_google(
    request: Request,
    body: GoogleAuthRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Verifies a Google ID token and either signs the caller in (existing account,
    linking the Google provider first if it hasn't been already) or hands back an
    onboarding session (new account).
    """
    await enforce_rate_limit(
        f"auth:google:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    google_user = await verify_google_id_token(body.credential)
    if google_user is None:
        raise UnauthorizedException("Invalid Google credential.")

    outcome = await resolve_identity(
        db=db,
        provider="google",
        email=google_user.email,
        provider_user_id=google_user.google_id,
        name=google_user.name,
        avatar=google_user.avatar,
    )
    return await _start_onboarding_or_sign_in(response, outcome)


@router.post("/complete", response_model=AuthOutcome)
async def complete_profile(
    request: Request,
    body: ProfileCompletionRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Creates the `User` row (and its `AuthenticationProvider` link) for a verified
    identity that had no account yet, then signs the new user in. This is the *only*
    place a `User` row is ever created.

    Rate limited per-IP because the username check below is an availability oracle:
    someone holding a single onboarding token could otherwise walk a wordlist through
    it and learn which usernames are taken.
    """
    await enforce_rate_limit(
        f"auth:complete:ip:{client_ip(request)}",
        settings.AUTH_COMPLETE_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    token_data = await verify_onboarding_token(body.onboarding_token, db)
    if token_data is None:
        raise UnauthorizedException("This onboarding session is invalid or has expired.")

    if await crud_users.exists(db=db, username=body.username):
        raise DuplicateValueException("Username not available")

    if await crud_users.exists(db=db, email=token_data.email):
        # Someone else completed onboarding for this email first - e.g. a concurrent
        # request reusing the same onboarding token/browser tab.
        raise DuplicateValueException("An account with this email already exists")

    user_internal = UserCreateInternal(
        name=body.name,
        username=body.username,
        email=token_data.email,
        profile_image_url=token_data.avatar or "https://profileimageurl.com",
    )

    try:
        created_user = await crud_users.create(
            db=db, object=user_internal, commit=False, schema_to_select=UserReadInternal, return_as_model=True
        )
        await crud_authentication_providers.create(
            db=db,
            object=AuthenticationProviderCreate(
                user_id=created_user.id, provider=token_data.provider, provider_user_id=token_data.provider_user_id
            ),
            commit=False,
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise DuplicateValueException("An account with this email or username already exists") from None

    # Single-use: a second `/auth/complete` call with the same onboarding token must
    # never create (or attempt to create) a second account.
    await blacklist_token(body.onboarding_token, db)

    tokens = await issue_tokens(response, created_user.uuid)
    return AuthOutcome(status="authenticated", **tokens)


def _elapsed(since: datetime) -> str:
    """How long ago `since` was, phrased to stay readable at both ends of the range
    `_warn_if_revoked` has to cover.

    Milliseconds are what separate a tab race from a replay, and a replay can arrive days
    after the theft - at which point `518400.000s` is a number nobody reads at a glance.
    So: seconds under a minute, `timedelta`'s own rendering above it.
    """
    seconds = (datetime.now(UTC) - since).total_seconds()
    if seconds < 60:
        return f"{seconds:.3f}s"
    return str(timedelta(seconds=round(seconds)))


async def _warn_if_revoked(refresh_token: str, db: AsyncSession) -> None:
    """Log a refresh token that failed verification *because it had been revoked*, which
    is the strongest evidence available that a refresh cookie has been stolen.

    `verify_token` can't report that difference and shouldn't have to - see
    `core.security.revocation_time` for why - so the question is asked again here, on a
    path that has already decided to answer 401. A malformed cookie is noise and stays
    silent; only a token this server actually issued and then spent gets a line.

    `WARNING` rather than `info`, and the level is load-bearing: the app configures no
    logging of its own beyond `configure_logging`, and `uvicorn` configures only its own
    loggers, so anything below `WARNING` is dropped on the floor in exactly the session
    where someone is trying to work out what happened. Same reasoning, at more length, on
    `api.dependencies.fetch_owned_or_raise`.

    **The elapsed time is the point, and it is why `token_blacklist.revoked_at` exists.**
    Rotation's documented two-tab race (see `refresh_access_token`) lands the losing tab
    on this exact branch, so an unconditional "your token was stolen" would cry wolf on a
    benign and not-especially-rare event. That race resolves in milliseconds; a stolen
    cookie is replayed minutes or hours later. The line therefore reports the gap and what
    it means, and leaves the reading to whoever is looking - both are live, and nothing
    the server can see distinguishes them beyond this.
    """
    revoked_at = await revocation_time(refresh_token, db)
    if revoked_at is None:
        return

    logger.warning(
        "A revoked refresh token was presented (subject: %s) %s after it was revoked. Under a second is "
        "the two-tab rotation race POST /auth/refresh documents; a longer gap is worth investigating as "
        "a stolen cookie.",
        token_subject(refresh_token) or "unknown",
        _elapsed(revoked_at),
    )


@router.post("/refresh")
async def refresh_access_token(
    request: Request, response: Response, db: AsyncSession = Depends(async_get_db)
) -> dict[str, str]:
    """Exchanges the httpOnly `refresh_token` cookie (set by `issue_tokens`) for a new
    access token *and a new refresh token*. See `DECISIONS.md` for why this is the one
    cookie-authenticated endpoint in this flow and why that's still CSRF-safe.

    The presented refresh token is blacklisted and replaced rather than reused. Without
    rotation a single leaked cookie stays valid for the whole
    `REFRESH_TOKEN_EXPIRE_DAYS` window with nothing to revoke it and no way to notice;
    with rotation, the theft has a much shorter useful life and a second use of the same
    token fails outright. The trade-off is that two tabs refreshing at the exact same
    moment will race, and the loser gets a 401 - see `DECISIONS.md`.

    That second use is also the loudest signal this app gets that a cookie has been
    stolen, so it is logged: `_warn_if_revoked` separates "a token we revoked, presented
    again" from "garbage", which the 401 deliberately does not. The *response* is
    identical either way - it must never become an oracle for whether a token was ever
    real.
    """
    await enforce_rate_limit(
        f"auth:refresh:ip:{client_ip(request)}",
        settings.AUTH_REFRESH_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise UnauthorizedException("Refresh token missing.")

    user_data = await verify_token(refresh_token, TokenType.REFRESH, db)
    if not user_data:
        # Only reached on a failure, so the extra lookup costs nothing on the happy path.
        await _warn_if_revoked(refresh_token, db)
        raise UnauthorizedException("Invalid refresh token.")

    # Spend the presented token before minting its replacement, so a crash between the
    # two leaves the caller signed out rather than holding two live refresh tokens.
    await blacklist_token(refresh_token, db)

    return await issue_tokens(response, user_data.user_uuid)


@router.post("/logout")
async def logout(
    response: Response,
    access_token: str = Depends(oauth2_scheme),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
    db: AsyncSession = Depends(async_get_db),
) -> dict[str, str]:
    """End the caller's session.

    Blacklists both the access and refresh tokens and clears the refresh cookie, so the
    pair stops working immediately rather than remaining valid until expiry. 401 when no
    refresh cookie is present or either token fails to decode.
    """
    try:
        if not refresh_token:
            raise UnauthorizedException("Refresh token not found")

        await blacklist_tokens(access_token=access_token, refresh_token=refresh_token, db=db)
        response.delete_cookie(key="refresh_token")

        return {"message": "Logged out successfully"}

    except JWTError:
        raise UnauthorizedException("Invalid token.")
