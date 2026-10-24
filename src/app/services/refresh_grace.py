"""The pair a refresh minted, held for the reuse grace so a retried presentation gets it back.

`POST /auth/refresh` spends the presented token before it answers, so a reply lost on its way
back leaves the client holding a token the server has already spent. Without this, the retry
is indistinguishable from a replay and ends the session. With it, a presentation inside
`REFRESH_TOKEN_REUSE_GRACE_SECONDS` is answered with the very pair the first one received -
the same pair rather than a fresh one, because minting again would spend the successor the
first presenter may be holding, and that presenter's next refresh would then land past the
grace and revoke the session.

In Redis with a TTL of the grace, keyed on a digest of the spent token, so the database
never holds a live refresh token. **Fails toward the behaviour without a grace**: a hold
that cannot be written or read is simply absent, and the retry gets the 401 and the sign-in
it would have got anyway - as it does on a Key Value instance that evicted or restarted
inside the minute.
"""

import json
import logging

from redis.exceptions import RedisError

from ..core.config import settings
from ..core.security import hash_token
from ..core.utils import cache
from .auth_service import IssuedTokens

logger = logging.getLogger(__name__)

# House key format, `domain:action:dimension:value`, matching `core.utils.rate_limit`.
_PREFIX = "auth:refresh-grace:token"


def _key(spent_token: str) -> str:
    return f"{_PREFIX}:{hash_token(spent_token)}"


async def hold_successor(spent_token: str, pair: IssuedTokens) -> None:
    """Hold `pair` as the answer to `spent_token` for the grace, or do nothing."""
    if cache.client is None:
        return
    payload = json.dumps(
        {"access_token": pair.access_token, "refresh_token": pair.refresh_token, "max_age": pair.max_age}
    )
    try:
        await cache.client.set(_key(spent_token), payload, ex=settings.REFRESH_TOKEN_REUSE_GRACE_SECONDS)
    except RedisError as exc:
        logger.warning("Could not hold a rotated refresh pair for its grace: %s", type(exc).__name__)


async def held_successor(spent_token: str) -> IssuedTokens | None:
    """The pair `spent_token` was rotated into, if it is still held."""
    if cache.client is None:
        return None
    try:
        raw = await cache.client.get(_key(spent_token))
    except RedisError as exc:
        logger.warning("Could not read a held refresh pair: %s", type(exc).__name__)
        return None
    if raw is None:
        return None
    stored = json.loads(raw)
    return IssuedTokens(
        access_token=stored["access_token"], refresh_token=stored["refresh_token"], max_age=stored["max_age"]
    )
