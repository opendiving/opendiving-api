from ..exceptions.http_exceptions import RateLimitException
from . import cache


async def enforce_rate_limit(key: str, max_requests: int, window_seconds: int) -> None:
    """Enforces a fixed-window rate limit backed by Redis: increments the counter at
    `key`, setting it to expire after `window_seconds` the first time it's touched,
    and raises `RateLimitException` once it exceeds `max_requests` within that window.

    A no-op if Redis isn't configured/connected (`cache.client is None`) - rate
    limiting degrades gracefully rather than hard-failing every request, since it's a
    defense-in-depth measure, not the primary security boundary (token expiry/single-use
    and generic responses are).
    """
    if cache.client is None:
        return

    current = await cache.client.incr(key)
    if current == 1:
        await cache.client.expire(key, window_seconds)

    if current > max_requests:
        raise RateLimitException("Too many requests. Please try again later.")
