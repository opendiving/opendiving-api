"""Unit tests for the Redis-backed fixed-window rate limiter used by the auth
endpoints (see `api.v1.auth`).
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import ConnectionError, RedisError, TimeoutError

from src.app.core.exceptions.http_exceptions import RateLimitException
from src.app.core.utils import rate_limit as rate_limit_module
from src.app.core.utils.rate_limit import enforce_rate_limit


class TestEnforceRateLimit:
    @pytest.mark.asyncio
    async def test_allows_requests_under_the_limit(self):
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(return_value=1)
            mock_cache.client.expire = AsyncMock(return_value=None)

            await enforce_rate_limit("key", max_requests=3, window_seconds=60)

            mock_cache.client.expire.assert_called_once_with("key", 60)

    @pytest.mark.asyncio
    async def test_only_sets_expiry_on_the_first_request_in_a_window(self):
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(return_value=2)
            mock_cache.client.expire = AsyncMock(return_value=None)

            await enforce_rate_limit("key", max_requests=3, window_seconds=60)

            mock_cache.client.expire.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_once_the_limit_is_exceeded(self):
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(return_value=4)
            mock_cache.client.ttl = AsyncMock(return_value=42)
            mock_cache.client.expire = AsyncMock(return_value=None)

            with pytest.raises(RateLimitException):
                await enforce_rate_limit("key", max_requests=3, window_seconds=60)

            # The window is intact, so nothing is repaired.
            mock_cache.client.expire.assert_not_called()

    @pytest.mark.asyncio
    async def test_repairs_a_counter_that_lost_its_window(self):
        """`incr` and `expire` are two round trips, so a Redis blip between them leaves a
        key that counts up forever and never expires - and from then on this limit rejects
        every request until someone deletes the key by hand. The lowest-limit callers are
        the most exposed: `geocode:provider` is 1 request per 1 second.
        """
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(return_value=4)
            mock_cache.client.ttl = AsyncMock(return_value=-1)
            mock_cache.client.expire = AsyncMock(return_value=None)

            with pytest.raises(RateLimitException):
                await enforce_rate_limit("key", max_requests=3, window_seconds=60)

            mock_cache.client.expire.assert_called_once_with("key", 60)

    @pytest.mark.asyncio
    async def test_does_not_check_the_window_on_the_happy_path(self):
        """The repair costs a round trip, so it only happens where the damage shows."""
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(return_value=2)
            mock_cache.client.ttl = AsyncMock(return_value=-1)
            mock_cache.client.expire = AsyncMock(return_value=None)

            await enforce_rate_limit("key", max_requests=3, window_seconds=60)

            mock_cache.client.ttl.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_raise_right_at_the_limit(self):
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(return_value=3)
            mock_cache.client.expire = AsyncMock(return_value=None)

            await enforce_rate_limit("key", max_requests=3, window_seconds=60)

    @pytest.mark.asyncio
    async def test_is_a_noop_without_redis_configured(self):
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client = None

            # Should not raise, even with an absurdly low limit - rate limiting is
            # defense-in-depth, not the primary security boundary.
            await enforce_rate_limit("key", max_requests=0, window_seconds=60)


class TestRedisIsDown:
    """The failure mode that matters in production, and the one this suite used to miss.

    `redis.Redis.from_pool` connects *lazily*, so in any shipped configuration
    `cache.client` is a live object regardless of whether Redis is actually up - the
    `client is None` branch is only reachable when the pool was never created at all.
    A real outage instead surfaces as `ConnectionError` from `incr`, which used to
    propagate and turn every rate-limited auth endpoint into a 500.
    """

    @pytest.fixture(autouse=True)
    def _reset_degraded_state(self):
        rate_limit_module._degraded = False
        yield
        rate_limit_module._degraded = False

    @pytest.mark.asyncio
    async def test_a_connection_error_fails_open_instead_of_raising(self):
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(side_effect=ConnectionError("Error 61 connecting to redis:6379"))

            # Must not raise: an unreachable Redis degrades throttling, it does not take
            # sign-in down with it.
            await enforce_rate_limit("key", max_requests=1, window_seconds=60)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [ConnectionError("down"), TimeoutError("slow"), RedisError("something else")])
    async def test_every_redis_error_fails_open(self, error):
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(side_effect=error)

            await enforce_rate_limit("key", max_requests=1, window_seconds=60)

    @pytest.mark.asyncio
    async def test_a_failure_on_expire_also_fails_open(self):
        """`expire` is only called on the first request in a window, so it has its own
        chance to be the call that hits a dead Redis.
        """
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(return_value=1)
            mock_cache.client.expire = AsyncMock(side_effect=ConnectionError("down"))

            await enforce_rate_limit("key", max_requests=1, window_seconds=60)

    @pytest.mark.asyncio
    async def test_the_outage_is_logged_once_not_per_request(self, caplog):
        """Failing open is deliberate but invisible - nothing about the app's behaviour
        changes. Warn, but only on entering the degraded state: during an outage the
        throttled endpoints are the ones being hammered, so per-call logging would bury
        the signal in its own noise.
        """
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(side_effect=ConnectionError("down"))

            with caplog.at_level(logging.WARNING, logger="src.app.core.utils.rate_limit"):
                for _ in range(5):
                    await enforce_rate_limit("key", max_requests=1, window_seconds=60)

            assert len([r for r in caplog.records if "not being enforced" in r.message]) == 1

    @pytest.mark.asyncio
    async def test_recovery_is_logged_and_rearms_the_warning(self, caplog):
        """A later, separate outage must warn again rather than be swallowed by the flag
        the first one set.
        """
        with (
            patch("src.app.core.utils.rate_limit.cache") as mock_cache,
            caplog.at_level(logging.INFO, logger="src.app.core.utils.rate_limit"),
        ):
            mock_cache.client.expire = AsyncMock(return_value=None)

            mock_cache.client.incr = AsyncMock(side_effect=ConnectionError("down"))
            await enforce_rate_limit("key", max_requests=5, window_seconds=60)

            mock_cache.client.incr = AsyncMock(return_value=1)
            await enforce_rate_limit("key", max_requests=5, window_seconds=60)

            mock_cache.client.incr = AsyncMock(side_effect=ConnectionError("down again"))
            await enforce_rate_limit("key", max_requests=5, window_seconds=60)

        assert len([r for r in caplog.records if "not being enforced" in r.message]) == 2
        assert len([r for r in caplog.records if "being enforced again" in r.message]) == 1

    @pytest.mark.asyncio
    async def test_the_limit_still_applies_when_redis_is_healthy(self):
        """Guard against the fail-open handler being written so broadly that it also
        swallows `RateLimitException`, which is this function's normal signal.
        """
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client.incr = AsyncMock(return_value=99)
            mock_cache.client.expire = AsyncMock(return_value=None)
            mock_cache.client.ttl = AsyncMock(return_value=42)

            with pytest.raises(RateLimitException):
                await enforce_rate_limit("key", max_requests=3, window_seconds=60)
