"""The Open-Meteo client: what it sends, what it caches, and how it sorts a failure."""

from datetime import date
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from src.app.core.config import PROJECT_USER_AGENT, settings
from src.app.core.utils import cache
from src.app.services import open_meteo
from src.app.services.open_meteo import Answered, CouldNotAsk, Refused
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.open_meteo import FakeOpenMeteo

DAY = date(2026, 9, 15)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(cache, "client", fake)
    return fake


@pytest.fixture(autouse=True)
def _free_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_API_KEY", None)
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 400)


class TestTheRequest:
    @pytest.mark.asyncio
    async def test_the_weather_request(self, redis: FakeRedis) -> None:
        with FakeOpenMeteo() as provider:
            answer = await open_meteo.fetch_weather(28.57, 34.54, DAY, date(2026, 9, 16), auto_zone=False)

        assert isinstance(answer, Answered)
        (request,) = provider.requests
        assert request.url.host == "archive-api.open-meteo.com"
        assert request.url.path == "/v1/archive"
        assert request.headers["User-Agent"] == PROJECT_USER_AGENT
        params = request.url.params
        assert (params["latitude"], params["longitude"]) == ("28.57", "34.54")
        assert (params["start_date"], params["end_date"]) == ("2026-09-15", "2026-09-16")
        assert params["timeformat"] == "unixtime"
        assert params["wind_speed_unit"] == "ms"
        assert params["hourly"].split(",") == list(open_meteo.WEATHER_VARIABLES)
        assert "timezone" not in params
        assert "apikey" not in params

    @pytest.mark.asyncio
    async def test_the_sea_request(self, redis: FakeRedis) -> None:
        with FakeOpenMeteo() as provider:
            await open_meteo.fetch_sea(28.57, 34.54, date(2026, 9, 14), date(2026, 9, 16))

        (request,) = provider.requests
        assert request.url.host == "marine-api.open-meteo.com"
        assert request.url.params["timeformat"] == "unixtime"
        assert request.url.params["hourly"].split(",") == list(open_meteo.SEA_VARIABLES)

    @pytest.mark.asyncio
    async def test_a_wall_clock_asks_for_the_zone(self, redis: FakeRedis) -> None:
        with FakeOpenMeteo() as provider:
            answer = await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=True)

        assert provider.requests[0].url.params["timezone"] == "auto"
        assert isinstance(answer, Answered)
        assert answer.timezone == "Africa/Cairo"

    @pytest.mark.asyncio
    async def test_a_key_moves_both_requests_to_the_customer_hosts(
        self, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_API_KEY", SecretStr("subscriber"))
        with FakeOpenMeteo() as provider:
            await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)
            await open_meteo.fetch_sea(28.57, 34.54, DAY, DAY)

        assert [request.url.host for request in provider.requests] == [
            "customer-archive-api.open-meteo.com",
            "customer-marine-api.open-meteo.com",
        ]
        assert all(request.url.params["apikey"] == "subscriber" for request in provider.requests)


class TestTheAnswerCache:
    @pytest.mark.asyncio
    async def test_a_second_ask_is_answered_from_the_cache(self, redis: FakeRedis) -> None:
        with FakeOpenMeteo() as provider:
            first = await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)
            second = await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)

        assert len(provider.requests) == 1
        assert first == second
        (key,) = [key for key in redis.store if key.startswith("conditions:v1:")]
        assert redis.expiries[key] == 3600

    @pytest.mark.asyncio
    async def test_the_zone_parameter_is_part_of_the_key(self, redis: FakeRedis) -> None:
        """A wall clock's `timezone=auto` answer and a UTC answer for the same days differ."""
        with FakeOpenMeteo() as provider:
            await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)
            await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=True)

        assert len(provider.requests) == 2

    @pytest.mark.asyncio
    async def test_a_failure_is_never_cached(self, redis: FakeRedis) -> None:
        with FakeOpenMeteo(lambda request: httpx.Response(503)) as provider:
            await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)
            await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)

        assert len(provider.requests) == 2

    @pytest.mark.asyncio
    async def test_a_redis_outage_asks_the_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cache, "client", FakeRedis(failing=True))
        with FakeOpenMeteo() as provider:
            answer = await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)

        assert isinstance(answer, Answered)
        assert len(provider.requests) == 1


class TestTheBudget:
    @pytest.mark.asyncio
    async def test_over_budget_asks_nothing(self, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 1)
        with FakeOpenMeteo() as provider:
            await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)
            answer = await open_meteo.fetch_sea(28.57, 34.54, DAY, DAY)

        assert answer is CouldNotAsk.BUDGET
        assert len(provider.requests) == 1

    @pytest.mark.asyncio
    async def test_a_cache_hit_spends_nothing(self, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 1)
        with FakeOpenMeteo():
            await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)
            answer = await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)

        assert isinstance(answer, Answered)


class TestAFailureIsSortedByWhoCausedIt:
    @pytest.mark.parametrize(
        ("status", "outcome"),
        [
            (400, Refused(400)),
            (429, CouldNotAsk.RATE_LIMITED),
            (401, CouldNotAsk.KEY_REFUSED),
            (403, CouldNotAsk.KEY_REFUSED),
            (500, CouldNotAsk.SERVER),
            (503, CouldNotAsk.SERVER),
        ],
    )
    @pytest.mark.asyncio
    async def test_by_status(self, redis: FakeRedis, status: int, outcome: Any) -> None:
        body = {"error": True, "reason": "Parameter 'end_date' is out of allowed range"}
        with FakeOpenMeteo(lambda request: httpx.Response(status, json=body)):
            assert await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False) == outcome

    @pytest.mark.asyncio
    async def test_a_timeout(self, redis: FakeRedis) -> None:
        def times_out(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with FakeOpenMeteo(times_out):
            assert await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False) is CouldNotAsk.TIMEOUT

    @pytest.mark.asyncio
    async def test_a_refused_connection(self, redis: FakeRedis) -> None:
        def refuses(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with FakeOpenMeteo(refuses):
            assert await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False) is CouldNotAsk.TRANSPORT

    @pytest.mark.asyncio
    async def test_a_body_without_the_series_is_the_provider_s_fault(self, redis: FakeRedis) -> None:
        with FakeOpenMeteo(lambda request: httpx.Response(200, json={"latitude": 28.5})):
            assert await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False) is CouldNotAsk.SERVER

    @pytest.mark.asyncio
    async def test_the_log_names_neither_the_url_nor_the_key(
        self, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_API_KEY", SecretStr("subscriber"))
        with FakeOpenMeteo(lambda request: httpx.Response(500)):
            await open_meteo.fetch_weather(28.57, 34.54, DAY, DAY, auto_zone=False)

        assert "open-meteo weather request answered 500" in caplog.text
        assert "subscriber" not in caplog.text
        assert "28.57" not in caplog.text
