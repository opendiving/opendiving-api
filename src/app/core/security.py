import hashlib
import logging
import secrets
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import anyio
import httpx
from fastapi import HTTPException
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
    LogbookImportTokenData,
    OnboardingTokenData,
    TokenBlacklistCreate,
    TokenBlacklistRead,
    TokenData,
)

logger = logging.getLogger(__name__)

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
    # The way back into an account inside its deletion grace period: minted when a
    # verified identity resolves to a soft-deleted row, spent by `POST /auth/restore` -
    # see `create_restore_token`/`verify_restore_token` below. Its own member rather than
    # a reused `ONBOARDING`, so neither is accepted at the other's endpoint.
    RESTORE = "restore"
    # Not a session at all: a receipt from `POST /dive/parse` attesting that this server
    # parsed a specific set of bytes for a specific user - see `create_dive_file_token`/
    # `verify_dive_file_token` below, and `PUT /dive/{uuid}/file`. Carries no authority;
    # the upload route still checks that the caller owns the dive.
    DIVE_FILE = "dive_file"
    # The same kind of receipt for a whole logbook: `POST /import/divejson/preview` read
    # these bytes for this user and reported what importing them would do - see
    # `create_logbook_import_token`/`verify_logbook_import_token`, and
    # `POST /import/divejson`. Its own member rather than a reused `DIVE_FILE` so a parse
    # receipt cannot be spent at the import endpoint or the other way round.
    LOGBOOK_IMPORT = "logbook_import"


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


# -------------- google authorization code exchange --------------
# `token_endpoint` from the OpenID discovery document at
# <https://accounts.google.com/.well-known/openid-configuration>, which is also the only
# thing that documents Google's PKCE support: it advertises
# `"code_challenge_methods_supported": ["plain", "S256"]`, and neither of Google's web-flow
# guides mentions PKCE at all. Confirmed still advertised on 2026-08-27.
#
# Hard-coded rather than discovered at startup. Fetching the document would buy a URL that
# has not moved in a decade, at the cost of one more thing that can be down when the app
# boots.
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Set by hand to match this repo's other outbound calls rather than to supply a bound that
# would otherwise be missing - httpx defaults to 5 seconds anyway. `services.geocoding_
# service` is the precedent, including the deadline below: the timeout is per socket read,
# so a host that dribbles bytes is bounded by the deadline and by nothing else.
_GOOGLE_TOKEN_TIMEOUT = httpx.Timeout(5.0)
_GOOGLE_TOKEN_DEADLINE_SECONDS = 10.0

# Four paths answer with this - Google unreachable, throttling, failing, or answering
# something unparseable - so it says what did not happen rather than why. "Could not reach
# Google" would be a guess that is wrong on three of the four.
_GOOGLE_UNAVAILABLE = "Could not complete the sign-in with Google. Please try again."


def _google_error_code(response: httpx.Response) -> str:
    """The short OAuth `error` enum from a refusal, for a DEBUG line and nothing else.

    Deliberately narrow. Google's refusal body carries an `error_description` alongside the
    enum, and this app's rule is that nothing from a Google token response reaches a log
    line at a level that ships - so the description is never read, and even the enum only
    appears under `LOG_LEVEL=DEBUG`, which is what an operator turns on to find out whether
    they are looking at `invalid_client` (a wrong secret) or `redirect_uri_mismatch` (a
    redirect URI nobody registered).
    """
    try:
        payload = response.json()
    except ValueError:
        return "unreadable"
    return str(payload.get("error", "unnamed")) if isinstance(payload, dict) else "unnamed"


async def exchange_google_code(*, code: str, code_verifier: str, redirect_uri: str) -> str | None:
    """Redeem a Google authorization code for the ID token it stands for.

    One POST, form-encoded as RFC 6749 §4.1.3 requires, carrying both halves of the OAuth
    client plus the PKCE verifier whose challenge the browser sent to the authorization
    endpoint. `httpx` rather than `google-auth-oauthlib`: it is already a dependency, and a
    second Google library would be a new supply-chain surface for one HTTP call.

    **Two failures, deliberately not one.** `None` means Google looked at the code and said
    no - expired, already redeemed, or a `redirect_uri`/verifier that does not match what it
    saw - which is a 401 to the caller. Google being unreachable, throttling this client,
    answering 5xx, or answering something that is not JSON is *this server* failing to do
    its job and raises 503 from here, because reporting it to a visitor as a bad credential
    would send them to re-try a sign-in that was never their problem.

    Nothing from the response is logged beyond a status code (see `_google_error_code`), and
    the secret only ever travels in the request body - never in the URL, and `core.setup`
    pins httpx's own logger to WARNING regardless of `LOG_LEVEL`, so nothing about the
    request is written out from there either.
    """
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        # Unreachable on a running instance - `Settings._require_google_client_secret`
        # refuses to boot a half-configured one, and the endpoint above is only useful with
        # an id. Kept so this function has an answer rather than an AttributeError.
        return None

    form = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET.get_secret_value(),
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    try:
        with anyio.fail_after(_GOOGLE_TOKEN_DEADLINE_SECONDS):
            # A client per call, as everywhere else here: sign-in with Google is not hot
            # enough for a pooled connection to still be warm, and a module-level client
            # would need lifespan wiring to be closed.
            async with httpx.AsyncClient(timeout=_GOOGLE_TOKEN_TIMEOUT) as client:
                response = await client.post(_GOOGLE_TOKEN_ENDPOINT, data=form)
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("Could not reach Google's token endpoint (%s).", type(exc).__name__)
        # A raw `HTTPException`: `core/exceptions/http_exceptions.py` has no class for 503,
        # same as `api/v1/contact.py` and `services/species_service.py`.
        raise HTTPException(status_code=503, detail=_GOOGLE_UNAVAILABLE) from None

    if response.status_code >= 500 or response.status_code == 429:
        # 429 sits with the 5xx rather than with the refusals below, even though it is a
        # 4xx: a throttled or quota-exhausted client is this server unable to complete the
        # exchange, not a visitor holding a bad code, and telling them to try signing in
        # again is the one piece of advice that cannot help.
        logger.warning("Google's token endpoint answered %s.", response.status_code)
        raise HTTPException(status_code=503, detail=_GOOGLE_UNAVAILABLE)

    if response.status_code != 200:
        logger.warning("Google refused an authorization code exchange (HTTP %s).", response.status_code)
        if logger.isEnabledFor(logging.DEBUG):
            # Guarded rather than left to the logger, so that a body this app has decided
            # not to log is not even parsed on the ordinary path.
            logger.debug("Google's refusal named error=%s.", _google_error_code(response))
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.warning("Google's token endpoint answered %s with a body that is not JSON.", response.status_code)
        raise HTTPException(status_code=503, detail=_GOOGLE_UNAVAILABLE) from None

    id_token = payload.get("id_token") if isinstance(payload, dict) else None
    if not isinstance(id_token, str) or not id_token:
        # A 200 with no ID token means the scope did not include `openid`, which is a
        # mistake in the authorization URL rather than anything the visitor did.
        logger.warning("Google's token response carried no id_token.")
        return None
    return id_token


# -------------- google id token verification --------------
async def verify_google_id_token(id_token: str) -> GoogleUserInfo | None:
    """Verify a Google ID token and extract the account info from it.

    The token arrives from `exchange_google_code` above - straight from Google's token
    endpoint over TLS, never through the browser. OIDC permits skipping signature
    verification on a token received that way, and this does it anyway: the same call also
    enforces `email_verified`, the presence of `email` and `sub`, and - through `audience` -
    that the token was issued for *this* app's OAuth client, which is what catches a
    half-swapped configuration. Keeping it also keeps `GoogleUserInfo` construction in one
    place.

    Returns
    -------
    GoogleUserInfo | None
        The verified account info, or `None` if the token is not a genuine, non-expired
        Google ID token for this client with a Google-verified email.
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
            id_token, google_requests.Request(), audience=settings.GOOGLE_CLIENT_ID
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


def _session_claim(session_uuid: uuid_pkg.UUID) -> dict[str, str]:
    """The `sid` claim, on both halves of the pair.

    **`sid` and `jti` are not the same identifier and neither replaces the other.** `jti`
    is per-*issuance*, which is what makes blacklisting a token by value a per-issuance
    revocation (see `_new_jti`); `sid` names the *device*, and is deliberately carried
    unchanged across every rotation, so it is the one thing about a session that survives
    `/auth/refresh` spending the cookie and minting an unrelated replacement.

    That identifier is what `DECISIONS.md` §"A reused refresh token is a `WARNING`" recorded
    as the missing prerequisite for its *Tier 3 - family revocation*. Tier 3 is still not
    implemented: a detected replay records an event and logs, and revokes nothing.
    """
    return {"sid": str(session_uuid)}


async def create_access_token(
    data: dict[str, Any], expires_delta: timedelta | None = None, session_uuid: uuid_pkg.UUID | None = None
) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "jti": _new_jti(), "token_type": TokenType.ACCESS})
    if session_uuid is not None:
        to_encode.update(_session_claim(session_uuid))
    encoded_jwt: str = jwt.encode(to_encode, SECRET_KEY.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


async def create_refresh_token(
    data: dict[str, Any], expires_delta: timedelta | None = None, session_uuid: uuid_pkg.UUID | None = None
) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "jti": _new_jti(), "token_type": TokenType.REFRESH})
    if session_uuid is not None:
        to_encode.update(_session_claim(session_uuid))
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

    The `sid` claim rides back on `TokenData` and is *not* validated here - this function
    says nothing about whether the session it names is still live, only what the token
    claims. `POST /auth/refresh` is the one caller that asks the second question, against
    the database, before it will mint a replacement.
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

        return TokenData(user_uuid=uuid_pkg.UUID(subject), session_uuid=_session_id(payload))

    except JWTError, ValueError:
        return None


def _session_id(payload: dict[str, Any]) -> uuid_pkg.UUID | None:
    """The `sid` claim off an already-decoded payload, or `None` if it is absent or is not
    a uuid.

    Absent is the ordinary case for one access-token lifetime after this feature ships, and
    for any token minted by an older build. Unparseable is not reachable through anything
    this app signs, and is tolerated rather than raised for the same reason `verify_token`
    tolerates a non-uuid `sub`: an escaping `ValueError` on a decode path is a 500 where a
    401 belongs.
    """
    raw = payload.get("sid")
    if not isinstance(raw, str):
        return None
    try:
        return uuid_pkg.UUID(raw)
    except ValueError:
        return None


def token_session_id(token: str) -> uuid_pkg.UUID | None:
    """The `sid` of a token whose validity has *already been established elsewhere*.

    Signature and expiry are still enforced (`jwt.decode` does both), but the blacklist and
    `token_type` checks are not - so this must never be the basis of an authorization
    decision, exactly as `token_subject` must not. The callers are routes whose sibling
    dependency `get_current_user` has already run `verify_token` over this same string and
    let the request through; all this adds is one more claim off it, without a second
    blacklist round trip on a path that has already paid for one.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
    except JWTError:
        return None

    return _session_id(payload)


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


# -------------- restore tokens --------------
async def create_restore_token(user_uuid: uuid_pkg.UUID) -> str:
    """Mints the token that `POST /auth/restore` spends to bring a soft-deleted account
    back, handed out by `_start_onboarding_or_sign_in` when a verified identity resolves
    to a row inside its deletion grace period.

    The mirror image of `create_onboarding_token`, deliberately: both carry a verified
    identity across one explicit decision the user has yet to make, neither is a session,
    both are single-use by blacklisting (hence the `jti`), and both borrow
    `ONBOARDING_TOKEN_EXPIRE_MINUTES` because the window they have to stay open for is the
    same one - somebody reading a screen and pressing a button.

    It is emphatically *not* the same token. `verify_onboarding_token` hard-checks
    `token_type`, so a distinct `TokenType.RESTORE` is what stops each from being redeemed
    at the other's endpoint. Both directions happen to be harmless today - an onboarding
    token names no user id to restore, a restore token names no email to create an account
    from - which is a property of today's payloads rather than of the design, and not
    something the next change to either should have to preserve.

    The subject is the `uuid` rather than the email the identity was proven with: on the
    passkey path there is no email in play at all, and the row's address can be rewritten
    by `verify_email_change` while the token is in flight.
    """
    expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=settings.ONBOARDING_TOKEN_EXPIRE_MINUTES)
    to_encode: dict[str, Any] = {
        "sub": str(user_uuid),
        "exp": expire,
        "jti": _new_jti(),
        "token_type": TokenType.RESTORE,
    }
    encoded_jwt: str = jwt.encode(to_encode, SECRET_KEY.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


async def verify_restore_token(token: str, db: AsyncSession) -> uuid_pkg.UUID | None:
    """Validates a restore JWT - not blacklisted (i.e. not already spent on a restore),
    unexpired, correctly signed, and of the right type - and returns the account it names.

    Says nothing about whether that account still exists or is still deleted: the purge can
    take it while the token is in flight, so the caller re-reads the row under a lock (see
    `POST /auth/restore`) rather than trusting this.
    """
    if await crud_token_blacklist.exists(db, token=token):
        return None

    try:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
    except JWTError:
        return None

    if payload.get("token_type") != TokenType.RESTORE:
        return None

    subject = payload.get("sub")
    if not isinstance(subject, str):
        return None

    try:
        return uuid_pkg.UUID(subject)
    except ValueError:
        return None


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


# -------------- logbook import tokens --------------
def create_logbook_import_token(*, user_uuid: uuid_pkg.UUID, sha256: str) -> str:
    """Mints the receipt `POST /import/divejson/preview` hands back with its report.

    Binds two things: who was shown the report, and exactly which bytes it was a report
    *about*. `POST /import/divejson` re-hashes the body it receives and refuses a mismatch,
    so the file a diver approves is the file that gets imported - the shape
    `create_dive_file_token` already uses for the parse-then-attach pair, minus its
    `parser_key`, which has no counterpart here.

    Deliberately not blacklisted after use, for the same reason that one is not: an import
    of a document whose uuids are already the caller's own creates nothing the second time
    (that is the whole idempotence invariant), and the token confers no authority to
    replay - it can only ever import bytes its holder was already shown a preview of.
    Hence no `jti` either.

    What it deliberately does **not** carry is the plan. Apply re-reads and re-plans the
    document inside its own transaction, because rows can be created or deleted between
    the two calls - a soft-deleted dive restored by hand, a site added under a name the
    document also uses - and a plan pinned at preview time would write against a database
    that had moved. The token is about the bytes; the decisions are made fresh.
    """
    expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=settings.IMPORT_TOKEN_EXPIRE_MINUTES)
    to_encode: dict[str, Any] = {
        "user_uuid": str(user_uuid),
        "sha256": sha256,
        "exp": expire,
        "token_type": TokenType.LOGBOOK_IMPORT,
    }
    encoded_jwt: str = jwt.encode(to_encode, SECRET_KEY.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


def verify_logbook_import_token(token: str) -> LogbookImportTokenData | None:
    """Validates a logbook-import token: well-formed, unexpired, correctly typed and
    complete. Returns what it attests, or `None` if any of that fails.

    Checking `token_type` is what stops an access token - which the frontend also holds,
    and which is signed with the same key - from being presented here as a preview receipt.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
    except JWTError:
        return None

    if payload.get("token_type") != TokenType.LOGBOOK_IMPORT:
        return None

    user_uuid = payload.get("user_uuid")
    sha256 = payload.get("sha256")
    if not user_uuid or not sha256:
        return None

    return LogbookImportTokenData(user_uuid=user_uuid, sha256=sha256)


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
