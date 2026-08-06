import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from fastapi.security import OAuth2PasswordBearer
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from jose import JWTError, jwt
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db.crud_token_blacklist import crud_token_blacklist
from .schemas import GoogleUserInfo, OnboardingTokenData, TokenBlacklistCreate, TokenData

SECRET_KEY: SecretStr = settings.SECRET_KEY
ALGORITHM = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES
REFRESH_TOKEN_EXPIRE_DAYS = settings.REFRESH_TOKEN_EXPIRE_DAYS

# Only used to *validate* a Bearer access token on protected routes (see
# `api.dependencies.get_current_user`) - there's deliberately no `tokenUrl` endpoint
# that issues one via a username/password form anymore. `OAuth2PasswordBearer` is used
# purely for its "extract the Bearer token from the Authorization header, and describe
# that in the OpenAPI schema" behavior, not its password-grant semantics.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/email/verify", auto_error=True)


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"
    # A short-lived, never-persisted session proving a verified identity (email or
    # Google) that doesn't have a `User` row yet - see `create_onboarding_token`/
    # `verify_onboarding_token` below, and `POST /auth/complete`.
    ONBOARDING = "onboarding"


# -------------- magic-link tokens --------------
def generate_secure_token() -> str:
    """Generates a cryptographically secure, single-use random token for the email
    magic-link flow (`POST /auth/email/request`). URL-safe so it can go straight into
    a query string with no extra encoding.
    """
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    """Hashes a magic-link token for storage (`AuthenticationRequest.token_hash`).

    Only the hash is ever persisted - a DB leak alone can't be used to mint valid
    magic links, only to invalidate/enumerate them. `raw_token` already has 256 bits
    of entropy from `secrets.token_urlsafe`, so a fast hash (rather than a
    deliberately slow, salted one like bcrypt) is appropriate here: this is unlike a
    user-chosen password, which needs defense against low-entropy guessing.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()



# -------------- google id token verification --------------
async def verify_google_id_token(credential: str) -> GoogleUserInfo | None:
    """Verify a Google Identity Services ID token and extract the account info from it.

    Parameters
    ----------
    credential: str
        The `credential` JWT returned to the frontend by Google's Identity Services
        library, forwarded here unmodified.

    Returns
    -------
    GoogleUserInfo | None
        The verified account info if `credential` is a genuine, non-expired Google ID
        token issued for this app (checked via the `aud` claim matching
        `settings.GOOGLE_CLIENT_ID`) and its email is Google-verified, `None` otherwise.
    """
    if not settings.GOOGLE_CLIENT_ID:
        return None

    try:
        # `verify_oauth2_token` validates the signature (against Google's published
        # public keys), expiry, issuer, and - via `audience` - that this token was
        # actually issued for *this* app's OAuth client, not some other one.
        payload = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), audience=settings.GOOGLE_CLIENT_ID
        )
    except (GoogleAuthError, ValueError):
        return None

    if not payload.get("email_verified") or not payload.get("email") or not payload.get("sub"):
        return None

    return GoogleUserInfo(
        google_id=payload["sub"],
        email=payload["email"],
        name=payload.get("name") or payload["email"].split("@")[0],
        avatar=payload.get("picture"),
    )


# -------------- access / refresh tokens --------------
async def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "token_type": TokenType.ACCESS})
    encoded_jwt: str = jwt.encode(to_encode, SECRET_KEY.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


async def create_refresh_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "token_type": TokenType.REFRESH})
    encoded_jwt: str = jwt.encode(to_encode, SECRET_KEY.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


async def verify_token(token: str, expected_token_type: TokenType, db: AsyncSession) -> TokenData | None:
    """Verify a JWT token and return TokenData if valid.

    Parameters
    ----------
    token: str
        The JWT token to be verified.
    expected_token_type: TokenType
        The expected type of token (access or refresh)
    db: AsyncSession
        Database session for performing database operations.

    Returns
    -------
    TokenData | None
        TokenData instance if the token is valid, None otherwise.
    """
    is_blacklisted = await crud_token_blacklist.exists(db, token=token)
    if is_blacklisted:
        return None

    try:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
        username_or_email: str | None = payload.get("sub")
        token_type: str | None = payload.get("token_type")

        if username_or_email is None or token_type != expected_token_type:
            return None

        return TokenData(username_or_email=username_or_email)

    except JWTError:
        return None


# -------------- onboarding tokens --------------
async def create_onboarding_token(data: OnboardingTokenData) -> str:
    """Creates a short-lived (`settings.ONBOARDING_TOKEN_EXPIRE_MINUTES`) JWT carrying a
    verified-but-accountless identity from `/auth/email/verify` or `/auth/google` to
    `/auth/complete`. Never persisted anywhere - the signature and expiry are all that
    back it, same as access/refresh tokens - but it *is* recorded in the token
    blacklist once used (see `blacklist_token`), making it single-use.
    """
    expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=settings.ONBOARDING_TOKEN_EXPIRE_MINUTES)
    to_encode: dict[str, Any] = {
        "email": data.email,
        "provider": data.provider,
        "provider_user_id": data.provider_user_id,
        "name": data.name,
        "avatar": data.avatar,
        "exp": expire,
        "token_type": TokenType.ONBOARDING,
    }
    encoded_jwt: str = jwt.encode(to_encode, SECRET_KEY.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


async def verify_onboarding_token(token: str, db: AsyncSession) -> OnboardingTokenData | None:
    """Validates an onboarding JWT: not blacklisted (i.e. not already used to complete
    a profile), not expired, and well-formed. Returns the identity it carries, or
    `None` if any of that fails.
    """
    is_blacklisted = await crud_token_blacklist.exists(db, token=token)
    if is_blacklisted:
        return None

    try:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
    except JWTError:
        return None

    if payload.get("token_type") != TokenType.ONBOARDING:
        return None

    email = payload.get("email")
    provider = payload.get("provider")
    if not email or not provider:
        return None

    return OnboardingTokenData(
        email=email,
        provider=provider,
        provider_user_id=payload.get("provider_user_id"),
        name=payload.get("name"),
        avatar=payload.get("avatar"),
    )


# -------------- blacklisting --------------
async def blacklist_tokens(access_token: str, refresh_token: str, db: AsyncSession) -> None:
    """Blacklist both access and refresh tokens.

    Parameters
    ----------
    access_token: str
        The access token to blacklist
    refresh_token: str
        The refresh token to blacklist
    db: AsyncSession
        Database session for performing database operations.
    """
    for token in [access_token, refresh_token]:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
        exp_timestamp = payload.get("exp")
        if exp_timestamp is not None:
            expires_at = datetime.fromtimestamp(exp_timestamp)
            await crud_token_blacklist.create(db, object=TokenBlacklistCreate(token=token, expires_at=expires_at))


async def blacklist_token(token: str, db: AsyncSession) -> None:
    payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
    exp_timestamp = payload.get("exp")
    if exp_timestamp is not None:
        expires_at = datetime.fromtimestamp(exp_timestamp)
        await crud_token_blacklist.create(db, object=TokenBlacklistCreate(token=token, expires_at=expires_at))
