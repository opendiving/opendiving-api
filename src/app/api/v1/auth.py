"""Unified authentication & registration flow.

The single entry point into the app: a caller either proves ownership of an email
address (magic link) or authenticates with Google, and *only then* do we ask "does an
account already exist for this identity?" (`resolve_identity`, in `services.auth_service`).
If so, they're signed in immediately. If not, a temporary onboarding session is issued
and no `User` row is created until profile completion (`POST /auth/complete`) succeeds -
there are no unverified users, and there is no separate sign up flow.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, UnauthorizedException
from ...core.schemas import OnboardingTokenData
from ...core.security import (
    blacklist_token,
    create_onboarding_token,
    generate_secure_token,
    hash_token,
    verify_google_id_token,
    verify_onboarding_token,
)
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


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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
    """Step 1 of the email flow: generates a single-use magic-link token and emails it.

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
        f"auth:email-request:ip:{_client_ip(request)}",
        settings.MAGIC_LINK_REQUEST_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    # Only one active token per email: invalidate any still-pending request(s) rather
    # than leaving them valid alongside the new one. FastCRUD's `update(...,
    # allow_multiple=True)` raises `NoResultFound` when zero rows match (the common
    # case - most emails won't have a pending request), so check first rather than
    # treating "nothing to invalidate" as an error (mirrors the token-blacklist purge
    # job's `count()`-before-`delete()` pattern in `core.worker.functions`).
    pending_count = await crud_authentication_requests.count(db, email=email, used_at=None)
    if pending_count > 0:
        await crud_authentication_requests.update(
            db=db,
            object=AuthenticationRequestUpdate(used_at=datetime.now(UTC)),
            allow_multiple=True,
            email=email,
            used_at=None,
        )

    raw_token = generate_secure_token()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.MAGIC_LINK_TOKEN_EXPIRE_MINUTES)
    await crud_authentication_requests.create(
        db=db,
        object=AuthenticationRequestCreate(email=email, token_hash=hash_token(raw_token), expires_at=expires_at),
    )

    magic_link_url = f"{settings.FRONTEND_URL}/auth/verify?token={raw_token}"
    await send_magic_link_email(email=email, magic_link_url=magic_link_url)

    return _EMAIL_REQUEST_RESPONSE


@router.post("/email/verify", response_model=AuthOutcome)
async def verify_email_link(
    request: Request,
    body: EmailVerifyRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Step 2 of the email flow: validates the magic-link token and either signs the
    caller in (existing account) or hands back an onboarding session (new one).
    """
    await enforce_rate_limit(
        f"auth:email-verify:ip:{_client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(db=db, token_hash=hash_token(body.token))
    if auth_request is None:
        raise UnauthorizedException("This sign-in link is invalid.")

    if auth_request["used_at"] is not None:
        raise UnauthorizedException("This sign-in link has already been used.")

    expires_at = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise UnauthorizedException("This sign-in link has expired.")

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
        f"auth:google:ip:{_client_ip(request)}",
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
    """
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
