import logging

from ..exceptions.http_exceptions import RateLimitException
from . import cache

logger = logging.getLogger(__name__)

# Set once the "Redis is gone, so nothing is being rate limited" warning has been
# emitted. Without this the warning would fire on every single throttled request, which
# in an outage is exactly the traffic that floods a log.
_warned_disabled = False


async def enforce_rate_limit(key: str, max_requests: int, window_seconds: int) -> None:
    """Enforces a fixed-window rate limit backed by Redis: increments the counter at
    `key`, setting it to expire after `window_seconds` the first time it's touched,
    and raises `RateLimitException` once it exceeds `max_requests` within that window.

    A no-op if Redis isn't configured/connected (`cache.client is None`) - rate
    limiting degrades gracefully rather than hard-failing every request, since it's a
    defense-in-depth measure, not the primary security boundary (token expiry/single-use
    and generic responses are).

    That failing open is the right call, but it is silent by construction: a Redis
    outage removes throttling from every endpoint at once and nothing about the app's
    behaviour changes visibly. So the first such call logs a warning - once - to make
    the condition greppable after the fact.
    """
    if cache.client is None:
        global _warned_disabled
        if not _warned_disabled:
            _warned_disabled = True
            logger.warning("Redis cache client unavailable - rate limiting is disabled for all endpoints.")
        return

    current = await cache.client.incr(key)
    if current == 1:
        await cache.client.expire(key, window_seconds)

    if current > max_requests:
        raise RateLimitException("Too many requests. Please try again later.")
