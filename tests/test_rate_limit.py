"""Unit tests for the Redis-backed fixed-window rate limiter used by the auth
endpoints (see `api.v1.auth`).
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest

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

            with pytest.raises(RateLimitException):
                await enforce_rate_limit("key", max_requests=3, window_seconds=60)

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

    @pytest.mark.asyncio
    async def test_failing_open_is_logged_once(self, caplog):
        """Failing open is deliberate, but it is also invisible: a Redis outage silently
        removes throttling from every endpoint at once. Warn - and only once, since the
        flooded log is exactly what an outage produces.
        """
        with patch("src.app.core.utils.rate_limit.cache") as mock_cache:
            mock_cache.client = None
            rate_limit_module._warned_disabled = False

            with caplog.at_level(logging.WARNING, logger="src.app.core.utils.rate_limit"):
                await enforce_rate_limit("key", max_requests=1, window_seconds=60)
                await enforce_rate_limit("key", max_requests=1, window_seconds=60)
                await enforce_rate_limit("key", max_requests=1, window_seconds=60)

            warnings = [r for r in caplog.records if "rate limiting is disabled" in r.message]
            assert len(warnings) == 1
