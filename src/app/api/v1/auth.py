"""Unified authentication & registration flow, plus session refresh/teardown.

The single entry point into the app: a caller either proves ownership of an email
address (magic link) or authenticates with Google, and *only then* do we ask "does an
account already exist for this identity?" (`resolve_identity`, in `services.auth_service`).
If so, they're signed in immediately. If not, a temporary onboarding session is issued
and no `User` row is created until profile completion (`POST /auth/complete`) succeeds -
there are no unverified users, and there is no separate sign up flow.

`POST /auth/refresh`/`POST /auth/logout` also live here (see `DECISIONS.md`) - they
used to sit in their own `login.py`/`logout.py` modules under a stale `"login"` tag,
left over from the old password-based flow.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

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
    hash_token,
    oauth2_scheme,
    verify_google_id_token,
    verify_onboarding_token,
    verify_token,
)
from ...core.utils.client_ip import client_ip
from ...core.utils.rate_limit import enforce_rate_limit
from ...crud.crud_authentication_providers import crud_authentication_providers
from ...crud.crud_authentication_requests import crud_authentication_requests
from ...crud.crud_users import crud_users
from ...schemas.auth import (
    AuthOutcome,
    EmailAuthRequest,
    EmailAuthRequestResponse,
    EmailVerifyRequest,
    GoogleAuthRequest,
    LinkCheckResponse,
    ProfileCompletionRequest,
)
from ...schemas.authentication_provider import AuthenticationProviderCreate
from ...schemas.authentication_request import AuthenticationRequestCreate, AuthenticationRequestUpdate
from ...schemas.user import UserCreateInternal, UserReadInternal
from ...services.auth_service import AuthenticatedUser, OnboardingRequired, issue_tokens, resolve_identity
from ...services.email_service import send_magic_link_email

router = APIRouter(prefix="/auth", tags=["auth"])

# Always the exact same response regardless of whether the email belongs to an
# existing account - see `EmailAuthRequestResponse`.
_EMAIL_REQUEST_RESPONSE = EmailAuthRequestResponse()


async def _start_onboarding_or_sign_in(
    response: Response, outcome: AuthenticatedUser | OnboardingRequired
) -> AuthOutcome:
    if isinstance(outcome, AuthenticatedUser):
        tokens = await issue_tokens(response, outcome.user["username"])
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
    """Step 1 of the email flow: generates a magic-link token and emails it.

    Always returns the same generic message, whether or not `email` belongs to an
    existing account - this must never be used to check if someone has signed up.
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
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.MAGIC_LINK_TOKEN_EXPIRE_MINUTES)
    await crud_authentication_requests.create(
        db=db,
        object=AuthenticationRequestCreate(
            email=email, token_hash=hash_token(raw_token), expires_at=expires_at, purpose="sign_in"
        ),
    )

    magic_link_url = f"{settings.FRONTEND_URL}/auth/verify?token={raw_token}"
    await send_magic_link_email(email=email, magic_link_url=magic_link_url)

    return _EMAIL_REQUEST_RESPONSE


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

    expires_at = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
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

    Re-opening/re-verifying the *same* link again (e.g. because a mail client's
    link-preview/security-scanning feature "detonated" it before a human clicked, or
    the user simply clicked twice) is deliberately not an error - it just re-confirms
    the same outcome, since `resolve_identity` is a pure lookup with no side effects
    of its own. Only an *expired* link, or one superseded by a newer request (see
    `request_email_link`), is rejected - see `AuthenticationRequest.invalidated_at`.
    """
    await enforce_rate_limit(
        f"auth:email-verify:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(db=db, token_hash=hash_token(body.token), purpose="sign_in")
    if auth_request is None:
        raise UnauthorizedException("This sign-in link is invalid.")

    if auth_request["invalidated_at"] is not None:
        raise UnauthorizedException("This sign-in link is no longer valid - a newer one was requested.")

    expires_at = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise UnauthorizedException("This sign-in link has expired.")

    if auth_request["used_at"] is None:
        await crud_authentication_requests.update(
            db=db, object=AuthenticationRequestUpdate(used_at=datetime.now(UTC)), id=auth_request["id"]
        )

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

    tokens = await issue_tokens(response, body.username)
    return AuthOutcome(status="authenticated", **tokens)


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
        raise UnauthorizedException("Invalid refresh token.")

    # Spend the presented token before minting its replacement, so a crash between the
    # two leaves the caller signed out rather than holding two live refresh tokens.
    await blacklist_token(refresh_token, db)

    return await issue_tokens(response, user_data.username_or_email)


@router.post("/logout")
async def logout(
    response: Response,
    access_token: str = Depends(oauth2_scheme),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
    db: AsyncSession = Depends(async_get_db),
) -> dict[str, str]:
    try:
        if not refresh_token:
            raise UnauthorizedException("Refresh token not found")

        await blacklist_tokens(access_token=access_token, refresh_token=refresh_token, db=db)
        response.delete_cookie(key="refresh_token")

        return {"message": "Logged out successfully"}

    except JWTError:
        raise UnauthorizedException("Invalid token.")
