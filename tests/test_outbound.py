"""Tests for `core/utils/outbound.py`, the mechanism every third-party provider call shares.

The callers' own suites hold each provider's numbers and what a failure means there; these hold
the bounds themselves, once, so a caller cannot opt out of one by accident.
"""

from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import anyio
import httpx
import pytest
from redis.exceptions import ConnectionError

from src.app.core.config import PROJECT_USER_AGENT
from src.app.core.exceptions.http_exceptions import RateLimitException
from src.app.core.utils import outbound
from src.app.core.utils.outbound import Failed, Fetched, FetchFailure

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_URL = "https://provider.example/api"


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    """Route every client the helper builds through `handler`, recording what it was sent."""
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    def build(**kwargs: Any) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(outbound.httpx, "AsyncClient", build)
    return sent


async def _fetch(url: str = _URL, **kwargs: Any) -> Fetched | Failed:
    return await outbound.fetch_capped(url, **{"timeout": 1.0, "deadline": 1.0, "max_bytes": 1024, **kwargs})


class TestFetchCapped:
    @pytest.mark.asyncio
    async def test_returns_the_status_headers_and_body(self, monkeypatch: pytest.MonkeyPatch):
        _serve(monkeypatch, lambda request: httpx.Response(200, content=b"{}", headers={"content-type": "x/y"}))

        fetched = await _fetch()

        assert isinstance(fetched, Fetched)
        assert (fetched.status, fetched.headers["content-type"], fetched.body) == (200, "x/y", b"{}")

    @pytest.mark.asyncio
    async def test_a_body_over_the_cap_is_a_failure(self, monkeypatch: pytest.MonkeyPatch):
        _serve(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 1025))

        assert await _fetch() == Failed(FetchFailure.TOO_LARGE)

    @pytest.mark.asyncio
    async def test_a_body_exactly_at_the_cap_is_kept(self, monkeypatch: pytest.MonkeyPatch):
        _serve(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 1024))

        fetched = await _fetch()

        assert isinstance(fetched, Fetched)
        assert len(fetched.body) == 1024

    @pytest.mark.asyncio
    async def test_a_host_that_trickles_under_the_read_timeout_misses_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Each chunk arrives well inside the per-read timeout, so only the deadline can end it."""

        async def trickle() -> AsyncIterator[bytes]:
            while True:
                await anyio.sleep(0.05)
                yield b"x"

        _serve(monkeypatch, lambda request: httpx.Response(200, content=trickle()))

        with anyio.fail_after(5):
            assert await _fetch(timeout=1.0, deadline=0.2) == Failed(FetchFailure.DEADLINE)

    @pytest.mark.asyncio
    async def test_a_malformed_url_is_a_failure_rather_than_an_exception(self):
        """`httpx.InvalidURL` is not an `HTTPError`; it is what a typo'd port in a setting raises."""
        fetched = await _fetch("http://provider.example:8O80/api")

        assert fetched == Failed(FetchFailure.TRANSPORT, error="InvalidURL")

    @pytest.mark.asyncio
    async def test_a_transport_error_is_a_failure(self, monkeypatch: pytest.MonkeyPatch):
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable", request=request)

        _serve(monkeypatch, unreachable)

        assert await _fetch() == Failed(FetchFailure.TRANSPORT, error="ConnectError")

    @pytest.mark.asyncio
    async def test_a_status_outside_accept_fails_with_that_status(self, monkeypatch: pytest.MonkeyPatch):
        _serve(monkeypatch, lambda request: httpx.Response(204))

        assert await _fetch() == Failed(FetchFailure.STATUS, status=204)
        assert isinstance(await _fetch(accept=range(200, 300)), Fetched)

    @pytest.mark.asyncio
    async def test_redirects_are_not_followed(self, monkeypatch: pytest.MonkeyPatch):
        sent = _serve(
            monkeypatch,
            lambda request: httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"}),
        )

        assert await _fetch() == Failed(FetchFailure.STATUS, status=302)
        assert [str(request.url) for request in sent] == [_URL]

    @pytest.mark.asyncio
    async def test_always_sends_a_user_agent(self, monkeypatch: pytest.MonkeyPatch):
        sent = _serve(monkeypatch, lambda request: httpx.Response(200))

        await _fetch()
        await _fetch(user_agent="Custom/1.0", headers={"Accept": "application/json"}, params={"q": "a b"})

        assert sent[0].headers["user-agent"] == PROJECT_USER_AGENT
        assert (sent[1].headers["user-agent"], sent[1].headers["accept"]) == ("Custom/1.0", "application/json")
        assert sent[1].url.params["q"] == "a b"

    def test_the_project_user_agent_names_the_project_and_nobody_in_it(self):
        assert PROJECT_USER_AGENT.startswith("OpenDiving ")
        assert "@" not in PROJECT_USER_AGENT


class TestClaimProviderSlot:
    @pytest.mark.asyncio
    async def test_a_free_slot_is_claimed_under_the_given_key(self):
        with patch.object(outbound, "enforce_rate_limit", new_callable=AsyncMock) as limit:
            assert await outbound.claim_provider_slot("p:key", 3, 10) is True

        limit.assert_awaited_once_with("p:key", 3, 10)

    @pytest.mark.asyncio
    async def test_a_full_cap_is_a_false_without_waiting_by_default(self):
        with (
            patch.object(outbound, "enforce_rate_limit", AsyncMock(side_effect=RateLimitException("full"))),
            patch.object(outbound, "sleep", new_callable=AsyncMock) as slept,
        ):
            assert await outbound.claim_provider_slot("p:key", 1, 1) is False

        slept.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_wait_is_spent_once_before_giving_up(self):
        limit = AsyncMock(side_effect=RateLimitException("full"))
        with (
            patch.object(outbound, "enforce_rate_limit", limit),
            patch.object(outbound, "sleep", new_callable=AsyncMock) as slept,
        ):
            assert await outbound.claim_provider_slot("p:key", 1, 1, wait_seconds=0.5) is False

        slept.assert_awaited_once_with(0.5)
        assert limit.await_count == 2


class _FakeRedis:
    def __init__(self, *, failing: bool = False) -> None:
        self.store: dict[str, bytes] = {}
        self.expiries: dict[str, int | None] = {}
        self.failing = failing

    async def get(self, key: str) -> bytes | None:
        if self.failing:
            raise ConnectionError("redis is down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.failing:
            raise ConnectionError("redis is down")
        self.store[key] = value.encode()
        self.expiries[key] = ex


class TestProviderCache:
    @pytest.mark.asyncio
    async def test_a_written_answer_reads_back_through_parse(self):
        redis = _FakeRedis()
        with patch("src.app.core.utils.cache.client", redis):
            await outbound.write_cached("k", '{"n": 2}', ttl_seconds=60)
            assert await outbound.read_cached("k", lambda body: body["n"] * 2) == 4

        assert redis.expiries == {"k": 60}

    @pytest.mark.asyncio
    async def test_no_redis_is_a_miss_and_a_silent_write(self):
        with patch("src.app.core.utils.cache.client", None):
            await outbound.write_cached("k", "[]", ttl_seconds=60)
            assert await outbound.read_cached("k", list) is None

    @pytest.mark.asyncio
    async def test_a_failing_redis_is_a_miss_and_a_silent_write(self):
        with patch("src.app.core.utils.cache.client", _FakeRedis(failing=True)):
            await outbound.write_cached("k", "[]", ttl_seconds=60)
            assert await outbound.read_cached("k", list) is None

    @pytest.mark.asyncio
    async def test_an_entry_that_will_not_decode_or_parse_is_a_miss(self):
        redis = _FakeRedis()
        redis.store = {"garbage": b"not json", "shape": b'{"a": 1}'}

        def refuse(body: Any) -> Any:
            raise TypeError("wrong shape")

        with patch("src.app.core.utils.cache.client", redis):
            assert await outbound.read_cached("garbage", list) is None
            assert await outbound.read_cached("shape", refuse) is None
