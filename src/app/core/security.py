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
from .schemas import (
    DiveFileTokenData,
    GoogleUserInfo,
    OnboardingTokenData,
    TokenBlacklistCreate,
    TokenBlacklistRead,
    TokenData,
)

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


def generate_sign_in_code() -> str:
    """Generates the six-digit code printed beside the magic link in the same email
    (`POST /auth/email/request`, redeemed by `POST /auth/email/verify-code`).

    Six digits because a person retypes it across devices - the failure the link cannot
    fix, since a link signs in whichever device opens it and mail is often read on a
    different one. Zero-padded, so `000042` is as likely as any other value and the code
    is always exactly six characters to compare and to display.

    `secrets.randbelow` rather than `random`: the space is small enough that a
    predictable generator would be guessable outright, whatever the attempt cap does.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_sign_in_code(code: str) -> str:
    """Hashes a sign-in code for storage (`AuthenticationRequest.code_hash`).

    A fast hash, for a *different* reason than `hash_token` above. There the argument is
    entropy: 256 bits is beyond guessing, so slowing an attacker down buys nothing. Here
    there are barely 20 bits, and no key-stretching function saves a six-digit secret
    from an attacker holding the digest - a million SHA-256s is milliseconds, a million
    bcrypts is an afternoon, and an afternoon is well inside the code's usefulness to
    someone who already has the database.

    So this hash is not what protects the code. `code_attempts` is (see
    `crud.crud_authentication_requests.register_failed_code_attempt`), and this exists so
    that a live credential is not sitting in plaintext in logs, backups, or the admin
    panel's row view.
    """
    return hashlib.sha256(code.encode()).hexdigest()


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
        # treatment `services.email_service` gives its equally blocking SMTP client.
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
def _new_jti() -> str:
    """A unique id for one token issuance, so that no two tokens are ever byte-identical.

    Without it they can be. The claims that distinguish one token from the next are
    `sub`, `token_type` and `exp` - and `exp` has one-second resolution, so two tokens
    minted for the same subject inside the same wall-clock second encode to exactly the
    same string. Every revocable token here is revoked by storing that string in
    `token_blacklist`, which means identical tokens share a single blacklist entry:

    - `/auth/refresh` spends the presented cookie and *then* mints its replacement (see
      `refresh_access_token`). When the replacement collides with the token just spent,
      the caller is handed a refresh token that is already blacklisted and gets a 401 on
      their next refresh - a random-looking logout, since it only happens when two
      refreshes land in the same second.
    - `/auth/logout` blacklists the access token it was given, which also revokes any
      sibling session whose access token happens to be identical.
    - An onboarding token is blacklisted to make it single-use, so two issued for the
      same identity in the same second (a magic link opened twice, say) are spent
      together and the second `/auth/complete` fails.

    A random `jti` makes each issuance distinct, which is what makes keying the blacklist
    on the token string correct. Nothing ever reads the claim back, so tokens minted
    before it existed keep verifying and deploying this signs nobody out.
    """
    return uuid_pkg.uuid4().hex


async def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "jti": _new_jti(), "token_type": TokenType.ACCESS})
    encoded_jwt: str = jwt.encode(to_encode, SECRET_KEY.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


async def create_refresh_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "jti": _new_jti(), "token_type": TokenType.REFRESH})
    encoded_jwt: str = jwt.encode(to_encode, SECRET_KEY.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


async def verify_token(token: str, expected_token_type: TokenType, db: AsyncSession) -> TokenData | None:
    """Validates an access or refresh token - not blacklisted, unexpired, correctly
    signed, and of `expected_token_type` - and returns the subject it names, or `None`
    if any of that fails.

    Checking `token_type` is what stops a refresh token from being presented as an
    access token on a protected route, and vice versa: both are signed with the same
    key, but a refresh token lives days rather than minutes.

    `ValueError` covers a `sub` that isn't a uuid at all. That is a 401 rather than the
    500 an escaping exception would produce, which is what any token minted before the
    subject became the user's `uuid` (it used to be their username) now gets.
    """
    is_blacklisted = await crud_token_blacklist.exists(db, token=token)
    if is_blacklisted:
        return None

    try:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
        subject: str | None = payload.get("sub")
        token_type: str | None = payload.get("token_type")

        if subject is None or token_type != expected_token_type:
            return None

        return TokenData(user_uuid=uuid_pkg.UUID(subject))

    except JWTError, ValueError:
        return None


async def revocation_time(token: str, db: AsyncSession) -> datetime | None:
    """When `token` was deliberately revoked, or `None` if it never was.

    `verify_token` above deliberately declines to say. "A token we issued and then
    revoked, presented again" and "unparseable garbage" both come back as `None`, and
    that is the right trade for it: it runs on every authenticated request via
    `api.dependencies.get_current_user`, and widening its return type into a
    discriminated result would change the shape of the hottest path in the app for the
    benefit of one rare branch. Callers that need the distinction ask the question a
    second time, here - `api.v1.auth.refresh_access_token` is the only one, and only
    after it has already decided to answer 401.

    So this costs nothing on any path that succeeds, and one indexed lookup on one that
    doesn't (`TokenBlacklist.token` is unique and indexed).

    Not an authorization decision: a `None` here means "not revoked", which is a long way
    from "valid". Nothing may treat it as the latter.
    """
    row = await crud_token_blacklist.get(db, token=token, schema_to_select=TokenBlacklistRead, return_as_model=True)
    return row.revoked_at if isinstance(row, TokenBlacklistRead) else None


def token_subject(token: str) -> str | None:
    """The `sub` claim of a token whose fate has already been decided elsewhere, for a log
    line that can name the account involved.

    Every check `verify_token` makes is skipped bar the signature and expiry that
    `jwt.decode` enforces on its own, so this must never become an authorization
    decision - it exists to make a `WARNING` legible, and nothing more.

    `None` covers every way a token can fail to yield a subject, expiry included. The case
    this exists for is unaffected: a token being *reused* rather than expired is by
    definition still inside its own `exp`, so it decodes. Between a token's `exp` and
    `purge_expired_tokens` clearing its blacklist row there is a window where a revoked
    token no longer decodes, and the caller degrades to reporting an unknown subject
    rather than letting an exception escape a path that is already returning a 401.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
    except JWTError:
        return None

    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None


# -------------- onboarding tokens --------------
async def create_onboarding_token(data: OnboardingTokenData) -> str:
    """Creates a short-lived (`settings.ONBOARDING_TOKEN_EXPIRE_MINUTES`) JWT carrying a
    verified-but-accountless identity from `/auth/email/verify` or `/auth/google` to
    `/auth/complete`. Never persisted anywhere - the signature and expiry are all that
    back it, same as access/refresh tokens - but it *is* recorded in the token
    blacklist once used (see `blacklist_token`), making it single-use. Being blacklisted
    by value is exactly why it carries a `jti` - see `_new_jti`.
    """
    expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=settings.ONBOARDING_TOKEN_EXPIRE_MINUTES)
    to_encode: dict[str, Any] = {
        "email": data.email,
        "provider": data.provider,
        "provider_user_id": data.provider_user_id,
        "name": data.name,
        "avatar": data.avatar,
        "exp": expire,
        "jti": _new_jti(),
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
    already owns a parse of. That is also why this is the one token here with no `jti`:
    nothing revokes it by value, so two identical receipts are simply the same receipt
    (see `_new_jti` for what goes wrong when a *revocable* token collides).

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

    The row is keyed on the whole token string (`TokenBlacklist.token` is unique), which
    is only a per-issuance key because every revocable token carries a `jti` - see
    `_new_jti`. Anything minted here that should be revocable needs that claim.

    A token that can't be decoded can't be blacklisted, and the callers here reach this
    from routes that already authenticated - so a `JWTError` means a malformed token was
    handed in, which is a 401, not the unhandled 500 an escaping `JWTError` would
    produce.

    `exp` is seconds since the Unix epoch, i.e. UTC. `datetime.fromtimestamp` without a
    tzinfo would render that in the *host's* local zone and store the result in a
    `DateTime(timezone=True)` column - so a server in UTC+2 would file every entry two
    hours late and `purge_expired_tokens` would delete each one two hours early.

    `revoked_at` is stamped here rather than left to the column's `server_default`, so that
    it comes from the same clock as everything else in this module and can be frozen in a
    test alongside them. It answers a question `expires_at` cannot - see the column's own
    comment, and `api.v1.auth._warn_if_revoked`, which is its only reader.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
    except JWTError:
        raise UnauthorizedException("Invalid token.") from None

    exp_timestamp = payload.get("exp")
    if exp_timestamp is not None:
        expires_at = datetime.fromtimestamp(exp_timestamp, UTC)
        await crud_token_blacklist.create(
            db,
            object=TokenBlacklistCreate(token=token, expires_at=expires_at, revoked_at=datetime.now(UTC)),
        )


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
