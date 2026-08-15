import logging

from redis.exceptions import RedisError

from ..exceptions.http_exceptions import RateLimitException
from . import cache

logger = logging.getLogger(__name__)

# Tracks whether we are currently in the "rate limiting isn't working" state, so the
# warning fires on entering it rather than on every request. During an outage the
# throttled endpoints are exactly the ones being hammered, so logging per call would
# bury the signal in its own noise. Reset on the next success, so a second, later
# outage warns again instead of passing silently.
_degraded = False


def _enter_degraded(reason: str) -> None:
    global _degraded
    if not _degraded:
        _degraded = True
        logger.warning("Rate limiting is not being enforced: %s", reason)


def _leave_degraded() -> None:
    global _degraded
    if _degraded:
        _degraded = False
        logger.info("Rate limiting is being enforced again.")


async def enforce_rate_limit(key: str, max_requests: int, window_seconds: int) -> None:
    """Enforces a fixed-window rate limit backed by Redis: increments the counter at
    `key`, setting it to expire after `window_seconds` the first time it's touched,
    and raises `RateLimitException` once it exceeds `max_requests` within that window.

    Fails **open** - if Redis can't be reached, the request is allowed through rather
    than rejected. Rate limiting here is defense-in-depth, not the primary security
    boundary (token expiry/single-use and generic responses are), so a Redis outage
    should degrade throttling rather than take sign-in down with it.

    Two distinct ways that happens, and both need handling:

    - **Not configured.** `cache.client is None`, i.e. `create_redis_cache_pool` never
      ran. Realistically only unit tests and stripped-down local runs.
    - **Configured but unreachable.** `redis.Redis.from_pool` connects *lazily*, so
      `cache.client` is a perfectly ordinary object that raises `ConnectionError` the
      moment you touch it. This is what an actual production Redis outage looks like,
      and checking for `None` does not catch it - an earlier version of this function
      only had the `None` branch, so every auth endpoint returned 500 the moment Redis
      blinked.

    `RateLimitException` is raised outside the `try` on purpose: it is this function's
    normal signal, not a Redis failure, and must not be swallowed by the handler.
    """
    if cache.client is None:
        _enter_degraded("the Redis cache client is not configured")
        return

    try:
        current = await cache.client.incr(key)
        if current == 1:
            await cache.client.expire(key, window_seconds)
        elif current > max_requests and await cache.client.ttl(key) < 0:
            # Self-heal a counter that lost its window. `incr` and `expire` are two round
            # trips, so a Redis blip between them leaves a key that counts up forever and
            # never expires - and from then on this limit rejects every request, for good,
            # until someone deletes the key by hand.
            #
            # The count is *started over*, not just given a TTL back: a counter with no
            # window has been accumulating for an unknown length of time, so its value
            # measures nothing, and re-arming the expiry alone would keep the caller
            # blocked for one more full window on the strength of a number that means
            # nothing. Starting fresh costs at most one window of accounting on a path no
            # caller can provoke, which is the same fail-open trade the docstring above
            # makes for an unreachable Redis. The check runs on the *rejecting* path only,
            # so the happy path still costs one round trip.
            logger.warning("Restarting the rate-limit window for %s: its counter had no expiry.", key)
            await cache.client.set(key, 1, ex=window_seconds)
            current = 1
    except RedisError as exc:
        _enter_degraded(f"Redis is unreachable ({type(exc).__name__})")
        return

    _leave_degraded()

    if current > max_requests:
        raise RateLimitException("Too many requests. Please try again later.")
