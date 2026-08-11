import hashlib
import secrets
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import anyio
from fastapi.security import OAuth2PasswordBearer
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from jose import JWTError, jwt
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db.crud_token_blacklist import crud_token_blacklist
from .exceptions.http_exceptions import UnauthorizedException
from .schemas import DiveFileTokenData, GoogleUserInfo, OnboardingTokenData, TokenBlacklistCreate, TokenData

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
    # Not a session at all: a receipt from `POST /dive/parse` attesting that this server
    # parsed a specific set of bytes for a specific user - see `create_dive_file_token`/
    # `verify_dive_file_token` below, and `PUT /dive/{uuid}/file`. Carries no authority;
    # the upload route still checks that the caller owns the dive.
    DIVE_FILE = "dive_file"


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

    def _verify() -> dict[str, Any]:
        # `verify_oauth2_token` validates the signature (against Google's published
        # public keys), expiry, issuer, and - via `audience` - that this token was
        # actually issued for *this* app's OAuth client, not some other one.
        #
        # Fetching those public keys is a *blocking* HTTPS call (`google.auth.transport.
        # requests` is built on `requests`), made whenever the library's key cache is cold
        # or stale. Left on the event loop it stalls every other in-flight request for the
        # duration of a round trip to Google, so it goes to a worker thread - the same
        # treatment `services.email_service` gives Resend's equally blocking client.
        verified: dict[str, Any] = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), audience=settings.GOOGLE_CLIENT_ID
        )
        return verified

    try:
        payload = await anyio.to_thread.run_sync(_verify)
    except GoogleAuthError, ValueError:
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


# -------------- dive file tokens --------------
def create_dive_file_token(*, user_uuid: uuid_pkg.UUID, sha256: str, parser_key: str) -> str:
    """Mints the receipt `POST /dive/parse` hands back with the parsed dive.

    Binds three things the upload route needs to trust: who parsed the file, exactly
    which bytes were parsed (by content hash), and which parser succeeded. `PUT
    /dive/{uuid}/file` re-hashes the body it receives and stores the file only if the
    hash matches, so the only bytes that can ever enter `dive_file` are bytes this
    server has already parsed.

    Deliberately *not* blacklisted after use, unlike `create_onboarding_token`:
    re-uploading the same file to the same dive is an idempotent no-op by design, and
    the token confers no authority to replay - it can only ever store bytes its holder
    already owns a parse of.

    The parser's `content_type` is deliberately absent. That value ends up in a response
    header on download, so it is resolved from `parser_key` against the live registry at
    store time rather than carried here, where a forged token could dictate it.
    """
    expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=settings.DIVE_FILE_TOKEN_EXPIRE_MINUTES)
    to_encode: dict[str, Any] = {
        "user_uuid": str(user_uuid),
        "sha256": sha256,
        "parser_key": parser_key,
        "exp": expire,
        "token_type": TokenType.DIVE_FILE,
    }
    encoded_jwt: str = jwt.encode(to_encode, SECRET_KEY.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


def verify_dive_file_token(token: str) -> DiveFileTokenData | None:
    """Validates a dive-file token: well-formed, unexpired, correctly typed and complete.
    Returns what it attests, or `None` if any of that fails.

    Checking `token_type` is what stops an access token - which the frontend also holds,
    and which is signed with the same key - from being presented here as a parse receipt.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
    except JWTError:
        return None

    if payload.get("token_type") != TokenType.DIVE_FILE:
        return None

    user_uuid = payload.get("user_uuid")
    sha256 = payload.get("sha256")
    parser_key = payload.get("parser_key")
    if not user_uuid or not sha256 or not parser_key:
        return None

    return DiveFileTokenData(user_uuid=user_uuid, sha256=sha256, parser_key=parser_key)


# -------------- blacklisting --------------
async def _blacklist_one(token: str, db: AsyncSession) -> None:
    """Record a single token as revoked until its own `exp` passes.

    A token that can't be decoded can't be blacklisted, and the callers here reach this
    from routes that already authenticated - so a `JWTError` means a malformed token was
    handed in, which is a 401, not the unhandled 500 an escaping `JWTError` would
    produce.

    `exp` is seconds since the Unix epoch, i.e. UTC. `datetime.fromtimestamp` without a
    tzinfo would render that in the *host's* local zone and store the result in a
    `DateTime(timezone=True)` column - so a server in UTC+2 would file every entry two
    hours late and `purge_expired_tokens` would delete each one two hours early.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
    except JWTError:
        raise UnauthorizedException("Invalid token.") from None

    exp_timestamp = payload.get("exp")
    if exp_timestamp is not None:
        expires_at = datetime.fromtimestamp(exp_timestamp, UTC)
        await crud_token_blacklist.create(db, object=TokenBlacklistCreate(token=token, expires_at=expires_at))


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
        await _blacklist_one(token, db)


async def blacklist_token(token: str, db: AsyncSession) -> None:
    await _blacklist_one(token, db)
