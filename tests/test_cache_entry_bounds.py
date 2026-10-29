"""What `cache` stores, and what it does when Redis fails under a read.

The Redis that holds response bodies also holds the rate-limit counters, the passkey
challenges and the refresh grace, so one account's list page of long notes must not fill it,
and a read must not turn a Redis outage into a 500.
"""

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError, RedisError

from src.app.core.utils import cache as cache_module
from src.app.core.utils.cache import MAX_CACHED_RESPONSE_BYTES, cache, namespaced
from tests.helpers.fake_redis import FakeRedis

KEY = namespaced("notes:1")


def _request(method: str) -> Request:
    request = Mock(spec=Request)
    request.method = method
    return request


def _returning(value: Any) -> AsyncMock:
    return AsyncMock(return_value=value)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(cache_module, "client", fake)
    return fake


class TestTheEntrySize:
    @pytest.mark.asyncio
    async def test_an_oversized_response_is_served_and_not_stored(self, redis: FakeRedis) -> None:
        body = {"notes": "x" * MAX_CACHED_RESPONSE_BYTES}

        result = await cache(key_prefix="notes")(_returning(body))(_request("GET"), id=1)

        assert result == body
        assert redis.writes == []

    @pytest.mark.asyncio
    async def test_the_size_is_the_utf8_one(self, redis: FakeRedis) -> None:
        """Three bytes a character in UTF-8, six as a `\\uXXXX` escape: under the limit
        only when it is stored unescaped."""
        body = {"notes": "海" * (MAX_CACHED_RESPONSE_BYTES // 4)}

        await cache(key_prefix="notes")(_returning(body))(_request("GET"), id=1)

        assert redis.writes == [KEY]
        assert len(redis.store[KEY]) <= MAX_CACHED_RESPONSE_BYTES < len(json.dumps(body))

    @pytest.mark.asyncio
    async def test_non_ascii_round_trips(self, redis: FakeRedis) -> None:
        body = {"notes": "Ćwiczenia w Dahabie 🐠, 潜水", "site": "Blue Hole"}
        decorated = cache(key_prefix="notes")(_returning(body))

        missed = await decorated(_request("GET"), id=1)
        hit = await cache(key_prefix="notes")(_returning({"notes": "not this"}))(_request("GET"), id=1)

        assert missed == hit == body
        assert "🐠".encode() in redis.store[KEY]


class TestARedisFailureUnderARead:
    @staticmethod
    def _app() -> FastAPI:
        app = FastAPI()

        @app.get("/notes/{note_id}")
        @cache(key_prefix="notes", resource_id_name="note_id")
        async def read_note(request: Request, note_id: int) -> dict[str, Any]:
            return {"id": note_id, "notes": "Ras Mohammed"}

        return app

    def test_redis_down_answers_200(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        monkeypatch.setattr(cache_module, "client", FakeRedis(failing=True))

        with caplog.at_level(logging.WARNING, logger=cache_module.__name__):
            response = TestClient(self._app()).get("/notes/7")

        assert response.status_code == 200
        assert response.json() == {"id": 7, "notes": "Ras Mohammed"}
        assert "Response cache read failed (ConnectionError)" in caplog.text

    def test_a_failed_write_answers_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        redis = Mock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(side_effect=ConnectionError("redis is down"))
        monkeypatch.setattr(cache_module, "client", redis)

        response = TestClient(self._app()).get("/notes/7")

        assert response.status_code == 200
        assert response.json() == {"id": 7, "notes": "Ras Mohammed"}
        redis.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_invalidation_still_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A write whose invalidation cannot reach Redis leaves a stale entry behind, which
        is not the same as a read going uncached."""
        monkeypatch.setattr(cache_module, "client", FakeRedis(failing=True))

        with pytest.raises(RedisError):
            await cache(key_prefix="notes")(_returning({}))(_request("PATCH"), id=1)
