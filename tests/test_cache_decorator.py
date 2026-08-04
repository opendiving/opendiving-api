"""Regression tests for the `cache` decorator (behavior should be unaffected by its typing refactor)."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import Request

from src.app.core.exceptions.cache_exceptions import InvalidRequestError, MissingClientError
from src.app.core.utils import cache as cache_module
from src.app.core.utils.cache import cache


def _make_request(method: str) -> Request:
    request = Mock(spec=Request)
    request.method = method
    return request


class TestCacheDecoratorGet:
    @pytest.mark.asyncio
    async def test_returns_cached_data_without_calling_wrapped_func(self):
        mock_redis = Mock()
        mock_redis.get = AsyncMock(return_value=b'{"cached": true}')

        func = AsyncMock()

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(key_prefix="item")(func)
            result = await decorated(_make_request("GET"), id=1)

        assert result == {"cached": True}
        func.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_wrapped_func_and_caches_result_on_miss(self):
        mock_redis = Mock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.expire = AsyncMock(return_value=True)

        func = AsyncMock(return_value={"value": 42})

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(key_prefix="item", expiration=120)(func)
            result = await decorated(_make_request("GET"), id=1)

        assert result == {"value": 42}
        func.assert_called_once()
        mock_redis.set.assert_called_once()
        cache_key = mock_redis.set.call_args[0][0]
        assert cache_key == "item:1"
        mock_redis.expire.assert_called_once_with(cache_key, 120)

    @pytest.mark.asyncio
    async def test_uses_resource_id_name_when_given(self):
        mock_redis = Mock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.expire = AsyncMock(return_value=True)

        func = AsyncMock(return_value={"value": 1})

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(key_prefix="user_{user_id}_items", resource_id_name="item_id")(func)
            await decorated(_make_request("GET"), user_id=7, item_id=99)

        cache_key = mock_redis.set.call_args[0][0]
        assert cache_key == "user_7_items:99"

    @pytest.mark.asyncio
    async def test_raises_invalid_request_error_when_get_has_invalidation_config(self):
        mock_redis = Mock()

        func = AsyncMock()

        with (
            patch.object(cache_module, "client", mock_redis),
            pytest.raises(InvalidRequestError),
        ):
            decorated = cache(key_prefix="item", to_invalidate_extra={"other": "{id}"})(func)
            await decorated(_make_request("GET"), id=1)

        func.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_missing_client_error_when_client_not_initialized(self):
        func = AsyncMock()

        with (
            patch.object(cache_module, "client", None),
            pytest.raises(MissingClientError),
        ):
            decorated = cache(key_prefix="item")(func)
            await decorated(_make_request("GET"), id=1)

        func.assert_not_called()


class TestCacheDecoratorNonGet:
    @pytest.mark.asyncio
    async def test_deletes_cache_key_and_returns_raw_result(self):
        mock_redis = Mock()
        mock_redis.delete = AsyncMock(return_value=1)

        func = AsyncMock(return_value={"message": "updated"})

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(key_prefix="item")(func)
            result = await decorated(_make_request("PATCH"), id=1)

        assert result == {"message": "updated"}
        mock_redis.delete.assert_called_once_with("item:1")

    @pytest.mark.asyncio
    async def test_invalidates_extra_keys(self):
        mock_redis = Mock()
        mock_redis.delete = AsyncMock(return_value=1)

        func = AsyncMock(return_value={"status": "updated"})

        with patch.object(cache_module, "client", mock_redis):
            decorated = cache(
                key_prefix="item_data",
                resource_id_name="item_id",
                to_invalidate_extra={"user_items": "{user_id}"},
            )(func)
            await decorated(_make_request("PUT"), item_id=1, user_id=7)

        deleted_keys = {call.args[0] for call in mock_redis.delete.call_args_list}
        assert deleted_keys == {"item_data:1", "user_items:7"}
