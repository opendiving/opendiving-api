"""`GET /dive-conditions/current`, the map's weather and sea now at a position.

The service runs against Postgres, for the position's rounding, with Open-Meteo behind a mock
transport and Redis as `FakeRedis`, as the lookup's tests do; the query parsing and the disabled
instance run through FastAPI on an app with no database behind it.
"""

import math
from collections.abc import Generator
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import dive_conditions as route_module
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.core.utils import cache
from src.app.schemas.dive_conditions import DiveConditionsCurrentRead
from src.app.services import open_meteo
from src.app.services.dive_conditions import COOLDOWN_KEY, current_conditions, run_dive_conditions
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.generators import create_user
from tests.helpers.open_meteo import FakeOpenMeteo, current_answers, current_sea_answers, forecast_answers

# 19:20 at +02:00 at the Blue Hole.
NOW = datetime(2026, 11, 9, 17, 20, tzinfo=UTC)
BLUE_HOLE = (28.5724, 34.5371)

needs_db = pytest.mark.skipif(not db_available(), reason="Postgres is not reachable")


@pytest.fixture(autouse=True)
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(cache, "client", fake)
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_PROVIDER", "open-meteo")
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_API_KEY", None)
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 400)
    return fake


async def _current(
    async_db: AsyncSession, now: datetime = NOW, position: tuple[float, float] = BLUE_HOLE
) -> DiveConditionsCurrentRead | None:
    result = await current_conditions(async_db, latitude=position[0], longitude=position[1], now=now)
    await async_db.rollback()
    return result


def _body(result: DiveConditionsCurrentRead | None) -> dict[str, Any]:
    assert result is not None
    return result.model_dump(mode="json")


def _hours(zone: str, times: list[int]) -> httpx.Response:
    """An answer naming `zone` with `times` as its hours and one value each."""
    count = len(times)
    hourly: dict[str, list[Any]] = {"time": times}
    hourly.update({name: [20.0] * count for name in open_meteo.WEATHER_VARIABLES})
    hourly["weather_code"] = [3] * count
    return httpx.Response(200, json={"timezone": zone, "utc_offset_seconds": 0, "hourly": hourly})


def _sea_alone(sea: Any) -> Any:
    """The recorded forecast for `NOW`, and `sea` for the marine model."""
    weather = forecast_answers(NOW)
    return lambda request: sea(request) if "marine" in request.url.host else weather(request)


THE_SEA_AT_NOW = {
    "model": "best_match",
    "sea_level_height": -0.15,
    "tide_state": "rising",
    "previous_extreme": {"kind": "low", "at": "2026-11-09T17:00:00+02:00", "height": -0.23},
    "next_extreme": {"kind": "high", "at": "2026-11-09T23:00:00+02:00", "height": 0.26},
    "sea_surface_temperature": 28.2,
    "wave_height": 0.3,
    "wave_period": 2.6,
    "swell_wave_height": 0.16,
}


@needs_db
class TestAnAnswer:
    @pytest.mark.asyncio
    async def test_the_hour_nearest_now_at_the_rounded_position(self, async_db: AsyncSession) -> None:
        with FakeOpenMeteo(current_answers(NOW)) as provider:
            body = _body(await _current(async_db))

        assert body["attribution"] == (
            "[Weather data by Open-Meteo.com](https://open-meteo.com/) "
            "([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)), with tide times derived from it"
        )
        assert body == {
            "provider": "open-meteo",
            "attribution": open_meteo.TIDES_ATTRIBUTION,
            "at": "2026-11-09T19:00:00+02:00",
            "position": {"latitude": 28.57, "longitude": 34.54},
            "weather": {
                "model": "best_match",
                "air_temperature": 24.0,
                "relative_humidity": 36.0,
                "wind_speed": 5.12,
                "wind_direction": 343.0,
                "wind_gusts": 8.2,
                "cloud_cover": 0.0,
                "precipitation": 0.0,
                "pressure": 1019.6,
                "weather_code": 0,
            },
            "sea": THE_SEA_AT_NOW,
        }
        (request,) = provider.asked("forecast")
        assert (request.url.host, request.url.path) == ("api.open-meteo.com", "/v1/forecast")
        params = request.url.params
        assert (params["latitude"], params["longitude"]) == ("28.57", "34.54")
        assert (params["past_hours"], params["forecast_hours"]) == ("1", "2")
        assert (params["timezone"], params["timeformat"], params["wind_speed_unit"]) == ("auto", "unixtime", "ms")
        assert params["hourly"].split(",") == list(open_meteo.WEATHER_VARIABLES)
        assert "start_date" not in params

    @pytest.mark.asyncio
    async def test_the_sea_is_asked_for_a_day_either_side_of_the_hour(self, async_db: AsyncSession) -> None:
        with FakeOpenMeteo(current_answers(NOW)) as provider:
            await _current(async_db)

        assert len(provider.requests) == 2
        (request,) = provider.asked("sea")
        assert (request.url.host, request.url.path) == ("marine-api.open-meteo.com", "/v1/marine")
        params = request.url.params
        assert (params["latitude"], params["longitude"]) == ("28.57", "34.54")
        assert (params["past_hours"], params["forecast_hours"]) == ("24", "25")
        assert (params["timeformat"], params["hourly"].split(",")) == ("unixtime", list(open_meteo.SEA_VARIABLES))
        assert "timezone" not in params and "start_date" not in params

    @pytest.mark.asyncio
    async def test_the_sea_is_read_at_the_weathers_hour(self, async_db: AsyncSession) -> None:
        """Past the half hour, both read 18:00 UTC: an hour later on the recorded series."""
        later = NOW.replace(minute=40)

        with FakeOpenMeteo(_sea_alone(current_sea_answers(NOW))):
            body = _body(await _current(async_db, later))

        assert body["at"] == "2026-11-09T20:00:00+02:00"
        assert body["sea"]["sea_level_height"] == -0.04
        assert body["sea"]["wave_height"] == 0.26

    @pytest.mark.asyncio
    async def test_each_turn_is_spelled_at_the_offset_its_zone_has_then(self, async_db: AsyncSession) -> None:
        """Berlin leaves summer time at 01:00 UTC on 2026-10-25: the low three hours before is
        +02:00, and the high three hours after +01:00."""
        now = datetime(2026, 10, 25, 0, 50, tzinfo=UTC)
        hour = int(datetime(2026, 10, 25, 1, 0, tzinfo=UTC).timestamp())
        hours = range(-24, 25)
        # A twelve-hour tide, low at 22:00 UTC and high at 04:00 UTC.
        sea = {
            "time": [hour + offset * 3600 for offset in hours],
            "sea_level_height_msl": [round(-math.cos(2 * math.pi * (offset + 3) / 12), 3) for offset in hours],
        }

        def answer(request: httpx.Request) -> httpx.Response:
            if "marine" in request.url.host:
                return httpx.Response(200, json={"timezone": "GMT", "hourly": sea})
            return _hours("Europe/Berlin", [hour - 3600, hour, hour + 3600])

        with FakeOpenMeteo(answer):
            body = _body(await _current(async_db, now, (52.52, 13.40)))

        assert body["at"] == "2026-10-25T02:00:00+01:00"
        assert body["sea"]["tide_state"] == "rising"
        assert body["sea"]["previous_extreme"] == {"kind": "low", "at": "2026-10-25T00:00:00+02:00", "height": -1.0}
        assert body["sea"]["next_extreme"] == {"kind": "high", "at": "2026-10-25T05:00:00+01:00", "height": 1.0}

    @pytest.mark.asyncio
    async def test_past_the_half_hour_the_next_hour_is_nearest(self, async_db: AsyncSession) -> None:
        later = NOW.replace(minute=40)

        with FakeOpenMeteo(forecast_answers(later)):
            body = _body(await _current(async_db, later))

        assert body["at"] == "2026-11-09T20:00:00+02:00"
        assert body["weather"]["air_temperature"] == 23.5

    @pytest.mark.asyncio
    async def test_the_hour_is_spelled_at_the_offset_its_zone_has_then(self, async_db: AsyncSession) -> None:
        """Berlin leaves summer time at 01:00 UTC on 2026-10-25: the hour after is +01:00 even
        while the answer's own `utc_offset_seconds`, now's, would say otherwise."""
        now = datetime(2026, 10, 25, 0, 40, tzinfo=UTC)
        hour = int(datetime(2026, 10, 25, 0, 0, tzinfo=UTC).timestamp())

        with FakeOpenMeteo(lambda request: _hours("Europe/Berlin", [hour - 3600, hour, hour + 3600])):
            body = _body(await _current(async_db, now, (52.52, 13.40)))

        assert body["at"] == "2026-10-25T02:00:00+01:00"

    @pytest.mark.asyncio
    async def test_a_key_moves_the_request_to_the_customer_host(
        self, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_API_KEY", SecretStr("subscriber"))

        with FakeOpenMeteo(current_answers(NOW)) as provider:
            await _current(async_db)

        assert [request.url.host for request in provider.requests] == [
            "customer-api.open-meteo.com",
            "customer-marine-api.open-meteo.com",
        ]
        assert {request.url.params["apikey"] for request in provider.requests} == {"subscriber"}


@needs_db
class TestTheAnswerCache:
    @pytest.mark.asyncio
    async def test_the_same_place_in_the_same_hour_is_asked_once(
        self, async_db: AsyncSession, redis: FakeRedis
    ) -> None:
        with FakeOpenMeteo(current_answers(NOW)) as provider:
            first = await _current(async_db)
            # Another diver, a few hundred metres away, later in the hour.
            second = await _current(async_db, NOW.replace(minute=50), (28.5710, 34.5440))

        assert len(provider.requests) == 2
        assert first is not None and second is not None
        assert first.position == second.position
        assert second.at is not None and second.at.hour == 20
        assert first.sea is not None and second.sea is not None
        (forecast,) = [key for key in redis.store if key.startswith("conditions:v1:forecast:")]
        assert forecast.startswith("conditions:v1:forecast:28.57:34.54:2026-11-09T17:")
        (sea,) = [key for key in redis.store if key.startswith("conditions:v1:sea:")]
        assert sea.startswith("conditions:v1:sea:28.57:34.54:2026-11-09T17:GMT:")
        assert redis.expiries[forecast] == redis.expiries[sea] == 3600

    @pytest.mark.asyncio
    async def test_the_next_hour_asks_again(self, async_db: AsyncSession) -> None:
        later = NOW.replace(hour=18)

        with FakeOpenMeteo(current_answers(NOW)) as provider:
            await _current(async_db)
        with FakeOpenMeteo(current_answers(later)) as provider:
            await _current(async_db, later)

        assert len(provider.requests) == 2


@needs_db
class TestNothingForThePosition:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            httpx.Response(400, json={"error": True, "reason": "Latitude must be in range"}),
            _hours("Africa/Cairo", []),
            _hours("Nowhere/Atlantis", [int(NOW.timestamp())]),
        ],
        ids=["a refused request", "no hours", "a zone this server cannot read"],
    )
    async def test_at_position_weather_and_sea_are_null(self, async_db: AsyncSession, answer: httpx.Response) -> None:
        with FakeOpenMeteo(lambda request: answer) as provider:
            body = _body(await _current(async_db))

        assert body == {
            "provider": "open-meteo",
            "attribution": open_meteo.READINGS_ATTRIBUTION,
            "at": None,
            "position": None,
            "weather": None,
            "sea": None,
        }
        assert provider.asked("sea") == []


@needs_db
class TestNoSeaForThePosition:
    """The weather answered and the sea did not: the weather, with `sea` null."""

    @staticmethod
    def _inland(request: httpx.Request) -> httpx.Response:
        times = current_sea_answers(NOW)(request).json()["hourly"]["time"]
        hourly = {"time": times, **{name: [None] * len(times) for name in open_meteo.SEA_VARIABLES}}
        return httpx.Response(200, json={"timezone": "GMT", "hourly": hourly})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sea",
        [
            _inland,
            lambda request: httpx.Response(400, json={"error": True, "reason": "No data"}),
            lambda request: httpx.Response(502),
            lambda request: httpx.Response(429),
        ],
        ids=["inland", "a refused request", "a failing marine model", "a rate limit"],
    )
    async def test_the_weather_without_the_sea(self, async_db: AsyncSession, sea: Any) -> None:
        with FakeOpenMeteo(_sea_alone(sea)):
            body = _body(await _current(async_db))

        assert body["at"] == "2026-11-09T19:00:00+02:00"
        assert body["weather"]["air_temperature"] == 24.0
        assert body["sea"] is None
        assert body["attribution"] == open_meteo.READINGS_ATTRIBUTION

    @pytest.mark.asyncio
    async def test_a_rate_limit_on_the_sea_cools_the_worker_down(
        self, async_db: AsyncSession, redis: FakeRedis
    ) -> None:
        with FakeOpenMeteo(_sea_alone(lambda request: httpx.Response(429))):
            await _current(async_db)

        assert await redis.get(COOLDOWN_KEY) is not None

    @pytest.mark.asyncio
    async def test_the_last_of_the_budget_answers_the_weather(
        self, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 1)

        with FakeOpenMeteo(current_answers(NOW)) as provider:
            body = _body(await _current(async_db))

        assert body["weather"] is not None and body["sea"] is None
        assert provider.asked("sea") == []


@needs_db
class TestTheProviderCouldNotBeAsked:
    @pytest.mark.asyncio
    async def test_a_failing_provider_is_none(self, async_db: AsyncSession) -> None:
        with FakeOpenMeteo(lambda request: httpx.Response(502)):
            assert await _current(async_db) is None

    @pytest.mark.asyncio
    async def test_the_budget_is_the_workers(self, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 1)

        with FakeOpenMeteo() as provider:
            await open_meteo.fetch_weather(28.57, 34.54, date(2026, 9, 15), date(2026, 9, 15), auto_zone=False)
            assert await _current(async_db) is None

        assert len(provider.requests) == 1

    @pytest.mark.asyncio
    async def test_a_rate_limit_cools_the_worker_down_too(self, async_db: AsyncSession, redis: FakeRedis) -> None:
        with FakeOpenMeteo(lambda request: httpx.Response(429)):
            assert await _current(async_db) is None

        assert await redis.get(COOLDOWN_KEY) is not None
        assert await run_dive_conditions(NOW) == "Cooling down after the provider refused a request"

    @pytest.mark.asyncio
    async def test_a_cool_down_asks_nothing(self, async_db: AsyncSession, redis: FakeRedis) -> None:
        await redis.set(COOLDOWN_KEY, "1", ex=60)

        with FakeOpenMeteo(forecast_answers(NOW)) as provider:
            assert await _current(async_db) is None

        assert provider.requests == []

    @pytest.mark.asyncio
    async def test_the_map_is_not_kept_waiting_on_the_workers_deadline(
        self, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[tuple[float, float]] = []
        real = open_meteo.fetch_capped

        async def recorded(url: str, **kwargs: Any) -> Any:
            asked.append((kwargs["timeout"], kwargs["deadline"]))
            return await real(url, **kwargs)

        monkeypatch.setattr(open_meteo, "fetch_capped", recorded)
        with FakeOpenMeteo(current_answers(NOW)):
            await _current(async_db)

        assert asked == [(open_meteo.PREVIEW_TIMEOUT_SECONDS, open_meteo.PREVIEW_DEADLINE_SECONDS)] * 2


@needs_db
class TestTheHandler:
    @pytest.mark.asyncio
    async def test_each_account_has_its_own_budget(
        self, db: Session, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        diver = create_user(db)
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_CURRENT_RATE_LIMIT_PER_USER", 7)
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_CURRENT_RATE_LIMIT_WINDOW_SECONDS", 60)

        with (
            patch.object(route_module, "enforce_rate_limit", new_callable=AsyncMock) as limit,
            FakeOpenMeteo(forecast_answers(datetime.now(UTC))),
        ):
            await route_module.read_dive_conditions_current(
                current_user={"id": diver.id, "uuid": diver.uuid, "is_superuser": False},
                db=async_db,
                latitude=BLUE_HOLE[0],
                longitude=BLUE_HOLE[1],
            )

        limit.assert_awaited_once_with(f"dive-conditions-current:user:{diver.id}", 7, 60)

    @pytest.mark.asyncio
    async def test_a_provider_that_could_not_be_asked_is_a_503(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)

        with (
            patch.object(route_module, "enforce_rate_limit", new_callable=AsyncMock),
            FakeOpenMeteo(lambda request: httpx.Response(502)),
            pytest.raises(HTTPException) as raised,
        ):
            await route_module.read_dive_conditions_current(
                current_user={"id": diver.id, "uuid": diver.uuid, "is_superuser": False},
                db=async_db,
                latitude=BLUE_HOLE[0],
                longitude=BLUE_HOLE[1],
            )

        assert raised.value.status_code == 503
        assert raised.value.detail


@pytest.fixture(scope="module")
def current_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`, as the geocoder's tests build one."""
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


class TestTheRoute:
    """Through FastAPI, with the service stubbed: what reaches it from the query string, and what
    never gets that far."""

    @pytest.fixture
    def service(self) -> Generator[AsyncMock]:
        answered = DiveConditionsCurrentRead(provider="open-meteo", attribution=open_meteo.READINGS_ATTRIBUTION)
        with (
            patch.object(route_module, "current_conditions", new_callable=AsyncMock, return_value=answered) as stub,
            patch.object(route_module, "enforce_rate_limit", new_callable=AsyncMock),
        ):
            yield stub

    @pytest.fixture
    def client(self, current_app: Any) -> Generator[TestClient]:
        current_app.dependency_overrides[get_current_user] = lambda: {"id": 1, "uuid": "x", "is_superuser": False}
        current_app.dependency_overrides[async_get_db] = lambda: MagicMock()
        with TestClient(current_app) as test_client:
            yield test_client
        current_app.dependency_overrides = {}

    def test_the_position_reaches_the_service(self, client: TestClient, service: AsyncMock) -> None:
        response = client.get("/api/v1/dive-conditions/current", params={"latitude": 28.49, "longitude": -34.51})

        assert response.status_code == 200, response.text
        assert response.json() == {
            "provider": "open-meteo",
            "attribution": open_meteo.READINGS_ATTRIBUTION,
            "at": None,
            "position": None,
            "weather": None,
            "sea": None,
        }
        assert service.await_args is not None
        assert (service.await_args.kwargs["latitude"], service.await_args.kwargs["longitude"]) == (28.49, -34.51)

    @pytest.mark.parametrize(
        "params",
        [
            {"latitude": 90.5, "longitude": 34.5},
            {"latitude": 28.5, "longitude": -180.5},
            {"latitude": 28.5},
            {"longitude": 34.5},
            {},
        ],
        ids=["latitude out of range", "longitude out of range", "no longitude", "no latitude", "nothing"],
    )
    def test_a_position_outside_the_globe_or_half_of_one_is_a_422(
        self, client: TestClient, service: AsyncMock, params: dict[str, float]
    ) -> None:
        assert client.get("/api/v1/dive-conditions/current", params=params).status_code == 422
        service.assert_not_awaited()

    def test_an_instance_fetching_no_conditions_has_no_route(
        self, current_app: Any, service: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_PROVIDER", "")

        # Signed out, so the 404 is the route's absence rather than anything about the caller.
        with TestClient(current_app) as anonymous:
            response = anonymous.get("/api/v1/dive-conditions/current", params={"latitude": 28.5, "longitude": 34.5})

        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
        service.assert_not_awaited()

    def test_signed_out_is_a_401_where_conditions_are_fetched(self, current_app: Any) -> None:
        with TestClient(current_app) as anonymous:
            response = anonymous.get("/api/v1/dive-conditions/current", params={"latitude": 28.5, "longitude": 34.5})

        assert response.status_code == 401
