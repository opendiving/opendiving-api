"""The WebAuthn challenge store: Redis keys that live exactly as long as one ceremony.

Redis's second auth job, and the one place in this app where it is **not** optional.
`core.utils.rate_limit` fails *open* on an outage because throttling is
defense-in-depth; a challenge is the whole anti-replay guarantee, so an unreachable
Redis here means the ceremony cannot be verified at all and both endpoints answer 503.
The failure domains are meant to stay independent: with Redis down, passkeys stop and
the magic link (pure Postgres) keeps working, which is the resilience argument for
having three sign-in methods at all.

Deliberately not an `authentication_request` row. Conditional UI arms on every
signed-out page view that supports it, so challenges are minted at page-view frequency -
a Postgres row per view is the unbounded table shape `plans/account-deletion.md` caught
`authentication_request` in, while a TTL key cleans itself up with no sweep job.
"""

import logging
import uuid as uuid_pkg

from fastapi import HTTPException
from redis.exceptions import RedisError
from webauthn.helpers import generate_challenge

from ..core.config import settings
from ..core.utils import cache

logger = logging.getLogger(__name__)

# House key format, `domain:action:dimension:value`, matching `core.utils.rate_limit`.
_SIGN_IN_PREFIX = "auth:passkey-challenge:flow"
_REGISTRATION_PREFIX = "auth:passkey-challenge:user"

_UNAVAILABLE = "Passkey sign-in is temporarily unavailable. Use your email instead."


class ChallengeStoreUnavailable(HTTPException):
    """Redis is not reachable, so no ceremony can be started or completed.

    An `HTTPException` rather than one of `core.exceptions.http_exceptions`' classes for
    the same reason `POST /contact` and `species.resolve` raise their own: there is no
    503 class in there. The message names the email fallback, because that is the whole
    point of the method still working.
    """

    def __init__(self) -> None:
        super().__init__(status_code=503, detail=_UNAVAILABLE)


def _client() -> cache.Redis:
    if cache.client is None:
        logger.warning("A passkey ceremony was attempted with no Redis client configured.")
        raise ChallengeStoreUnavailable()
    return cache.client


async def store_sign_in_challenge() -> tuple[str, bytes]:
    """Mint a challenge for an anonymous assertion and return `(flow_id, challenge)`.

    The `flow_id` is what the browser hands back to `POST /auth/passkey/verify`, and it
    is the only thing that names this challenge - the ceremony identifies no account, so
    there is nothing else it could be keyed on.
    """
    challenge = generate_challenge()
    flow_id = str(uuid_pkg.uuid4())
    try:
        await _client().set(f"{_SIGN_IN_PREFIX}:{flow_id}", challenge, ex=settings.PASSKEY_CHALLENGE_TTL_SECONDS)
    except RedisError as exc:
        logger.warning("Could not store a passkey sign-in challenge: %s", type(exc).__name__)
        raise ChallengeStoreUnavailable() from exc
    return flow_id, challenge


async def store_registration_challenge(user_id: int) -> bytes:
    """Mint a challenge for a signed-in registration ceremony.

    Keyed by user, so there is one pending registration per account: two tabs racing both
    fail, because the second tab's `options` call overwrites the challenge the first tab's
    `verify` then presents. Named and accepted - it self-heals on a retry, and the
    alternative (a flow id per tab) is unbounded keys for a ceremony nobody runs twice.
    """
    challenge = generate_challenge()
    try:
        await _client().set(f"{_REGISTRATION_PREFIX}:{user_id}", challenge, ex=settings.PASSKEY_CHALLENGE_TTL_SECONDS)
    except RedisError as exc:
        logger.warning("Could not store a passkey registration challenge: %s", type(exc).__name__)
        raise ChallengeStoreUnavailable() from exc
    return challenge


async def _consume(key: str) -> bytes | None:
    """`GETDEL` the challenge at `key`: single-use **even when verification then fails**.

    Spending it on the attempt rather than on success is the point - a captured assertion
    must not be retriable against a still-live challenge, and a verify that failed for any
    reason has already burned the one it presented.
    """
    try:
        stored = await _client().getdel(key)
    except RedisError as exc:
        logger.warning("Could not read a passkey challenge: %s", type(exc).__name__)
        raise ChallengeStoreUnavailable() from exc

    if stored is None:
        return None
    # The pool is created `decode_responses=False` (`core.setup`), so this is already
    # bytes - the encode is for a test client configured the other way, and costs nothing.
    return stored if isinstance(stored, bytes) else str(stored).encode()


async def consume_sign_in_challenge(flow_id: str) -> bytes | None:
    """The challenge `flow_id` stands for, or `None` if it expired or was already spent."""
    return await _consume(f"{_SIGN_IN_PREFIX}:{flow_id}")


async def consume_registration_challenge(user_id: int) -> bytes | None:
    """The challenge this user's pending registration was started with, if any."""
    return await _consume(f"{_REGISTRATION_PREFIX}:{user_id}")
