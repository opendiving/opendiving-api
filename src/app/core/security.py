from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, cast

import bcrypt
from fastapi.security import OAuth2PasswordBearer
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from jose import JWTError, jwt
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.crud_users import crud_users
from .config import settings
from .db.crud_token_blacklist import crud_token_blacklist
from .schemas import GoogleUserInfo, TokenBlacklistCreate, TokenData

SECRET_KEY: SecretStr = settings.SECRET_KEY
ALGORITHM = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES
REFRESH_TOKEN_EXPIRE_DAYS = settings.REFRESH_TOKEN_EXPIRE_DAYS

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/login")


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


async def verify_password(plain_password: str, hashed_password: str) -> bool:
    correct_password: bool = bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
    return correct_password


def get_password_hash(password: str) -> str:
    hashed_password: str = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    return hashed_password


async def authenticate_user(username_or_email: str, password: str, db: AsyncSession) -> dict[str, Any] | Literal[False]:
    if "@" in username_or_email:
        db_user = await crud_users.get(db=db, email=username_or_email, is_deleted=False)
    else:
        db_user = await crud_users.get(db=db, username=username_or_email, is_deleted=False)

    if not db_user:
        return False

    db_user = cast(dict[str, Any], db_user)
    # Google-only accounts (see `/login/google`) have no password to check against.
    if db_user["hashed_password"] is None:
        return False

    if not await verify_password(password, db_user["hashed_password"]):
        return False

    return db_user


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
    )


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
