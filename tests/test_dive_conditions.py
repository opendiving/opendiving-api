"""The worker's `fetch_dive_conditions` against Postgres, with Open-Meteo behind a mock transport.

The cron is unscoped - it takes every candidate in the database - so each test narrows the
candidate query to the accounts it made (`mine`), leaving every other test's dives alone.
Redis is `FakeRedis`, which records expiries without enforcing them.
"""

import json
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.v1 import dives as dives_module
from src.app.core.config import settings
from src.app.core.db.database import async_engine
from src.app.core.utils import cache
from src.app.core.worker.functions import fetch_dive_conditions
from src.app.core.worker.settings import WorkerSettings
from src.app.models import Dive, DiveConditions, DiveDiveSite, DiveSite, User
from src.app.schemas.dive import DiveUpdateRequest
from src.app.services import dive_conditions
from src.app.services.dive_conditions import (
    COOLDOWN_KEY,
    LOCK_KEY,
    LOCK_LIFETIME_SECONDS,
    run_dive_conditions,
)
from src.app.services.export import load_export_bundle, write_divejson
from src.app.services.logbook_import import plan_import, write_import
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.generators import create_user
from tests.helpers.import_parts import load_one
from tests.helpers.open_meteo import FakeOpenMeteo, dahab_answers, dahab_body, fixture

pytestmark = pytest.mark.skipif(not db_available(), reason="Postgres is not reachable")

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
# 10:00 at +03:00 at the Blue Hole.
DAHAB_DIVE = datetime(2026, 9, 15, 7, 0, tzinfo=UTC)
BLUE_HOLE = (28.572, 34.537)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_the_app_engine() -> AsyncGenerator[None]:
    """The cron opens its own session from the module-level `local_session`, and a pooled
    asyncpg connection belongs to the loop that opened it."""
    await async_engine.dispose()
    yield
    await async_engine.dispose()


@pytest.fixture(autouse=True)
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(cache, "client", fake)
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_PROVIDER", "open-meteo")
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_API_KEY", None)
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 400)
    return fake


@pytest.fixture
def mine(monkeypatch: pytest.MonkeyPatch) -> Callable[..., User]:
    """Make an account whose dives the cron may take; nobody else's are candidates here."""
    accounts: set[int] = set()
    real = dive_conditions._candidates

    async def scoped(db: AsyncSession, now: datetime, limit: int) -> list[Any]:
        return [one for one in await real(db, now, 1_000_000) if one.user_id in accounts][:limit]

    monkeypatch.setattr(dive_conditions, "_candidates", scoped)

    def make(db: Session) -> User:
        user = create_user(db)
        accounts.add(user.id)
        return user

    return make


def _site(db: Session, user: User, position: tuple[float, float] | None = BLUE_HOLE, **fields: Any) -> DiveSite:
    site = DiveSite(
        user_id=user.id,
        name=f"Blue Hole {datetime.now(UTC).timestamp()}",
        latitude=None if position is None else position[0],
        longitude=None if position is None else position[1],
        notes="",
        **fields,
    )
    db.add(site)
    db.commit()
    return site


def _dive(
    db: Session,
    user: User,
    *,
    site: DiveSite | None = None,
    start_time: datetime = DAHAB_DIVE,
    offset: int | None = 180,
    start_date_only: bool = False,
    **fields: Any,
) -> Dive:
    dive = Dive(
        user_id=user.id,
        dive_number=1,
        start_time=start_time,
        utc_offset_minutes=offset,
        submerged_time=2400,
        notes="",
        **fields,
    )
    db.add(dive)
    db.commit()
    if offset is None:
        # The ORM writes the column's default for a `None`; an unknown offset is a Core write,
        # as the importer's is.
        db.execute(
            update(Dive).where(Dive.id == dive.id).values(utc_offset_minutes=None, start_date_only=start_date_only)
        )
        db.commit()
    if site is not None:
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=0))
        db.commit()
    return dive


def _row(db: Session, dive: Dive) -> DiveConditions | None:
    db.expire_all()
    return db.execute(select(DiveConditions).where(DiveConditions.dive_id == dive.id)).scalar_one_or_none()


def _fresh(db: Session, dive: Dive) -> Dive:
    db.expire_all()
    return db.get_one(Dive, dive.id)


async def _read(async_db: AsyncSession, user: User, dive: Dive) -> dict[str, Any]:
    response = await dives_module.read_dive(
        request=MagicMock(method="GET"),
        uuid=dive.uuid,
        current_user={"id": user.id, "uuid": user.uuid, "is_superuser": False},
        db=async_db,
    )
    await async_db.rollback()
    return response if isinstance(response, dict) else response.model_dump(mode="json")


async def _patch(async_db: AsyncSession, user: User, dive: Dive, body: dict[str, Any]) -> None:
    await dives_module.patch_dive(
        request=MagicMock(),
        uuid=dive.uuid,
        values=DiveUpdateRequest.model_validate(body),
        current_user={"id": user.id, "uuid": user.uuid, "is_superuser": False},
        db=async_db,
    )


def _with(**changes: Any) -> Callable[[httpx.Request], httpx.Response]:
    """Dahab's answers with some hourly series replaced: `name=value` sets every sample."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dahab_body(request)
        for name, value in changes.items():
            if name in body["hourly"]:
                body["hourly"][name] = [value] * len(body["hourly"][name])
        return httpx.Response(200, json=body)

    return handler


class TestAFetchFillsTheEmptyFields:
    @pytest.mark.asyncio
    async def test_a_salt_water_dive_is_filled_marked_and_read(
        self, db: Session, async_db: AsyncSession, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        # Read once, so the detail is cached without conditions: the fill has to drop it.
        assert (await _read(async_db, diver, dive))["conditions"] is None

        with FakeOpenMeteo():
            await run_dive_conditions(NOW)

        filled = _fresh(db, dive)
        assert (filled.air_temperature, filled.weather, filled.waves) == (38.1, "clear", "slight")
        assert filled.current is None
        row = _row(db, dive)
        assert row is not None and row.state == "fetched"
        assert (row.filled_air_temperature, row.filled_weather, row.filled_waves) == (38.1, "clear", "slight")
        assert (row.latitude, row.longitude) == (28.57, 34.54)
        assert row.tide_state == "high"

        conditions = (await _read(async_db, diver, dive))["conditions"]
        assert conditions["provider"] == "open-meteo"
        assert conditions["at"] == "2026-09-15T10:00:00+03:00"
        assert conditions["utc_offset_minutes"] == 180
        assert conditions["position"] == {"latitude": 28.57, "longitude": 34.54}
        assert conditions["attribution"].startswith("[Weather data by Open-Meteo.com](https://open-meteo.com/)")
        assert conditions["filled"] == {"air_temperature": 38.1, "weather": "clear", "waves": "slight"}
        assert conditions["weather"]["wind_speed"] == 5.77
        assert conditions["weather"]["wind_direction"] == 27
        sea = conditions["sea"]
        assert (sea["tide_state"], sea["wave_height"], sea["sea_surface_temperature"]) == ("high", 0.62, 27.7)
        assert sea["previous_extreme"]["at"] == "2026-09-15T09:30:00+03:00"
        assert sea["previous_extreme"]["kind"] == "high"
        assert sea["next_extreme"]["at"] == "2026-09-15T15:00:00+03:00"
        assert sea["next_extreme"]["kind"] == "low"

    @pytest.mark.asyncio
    async def test_a_value_the_diver_recorded_is_never_overwritten(
        self, db: Session, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver), weather="overcast", air_temperature=30.0)

        with FakeOpenMeteo():
            await run_dive_conditions(NOW)

        filled = _fresh(db, dive)
        assert (filled.air_temperature, filled.weather, filled.waves) == (30.0, "overcast", "slight")
        row = _row(db, dive)
        assert row is not None
        assert (row.filled_air_temperature, row.filled_weather, row.filled_waves) == (None, None, "slight")
        assert row.air_temperature == 38.1

    @pytest.mark.asyncio
    async def test_a_weather_typed_while_the_provider_answers_is_kept(
        self, db: Session, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))

        def typed_meanwhile(request: httpx.Request) -> httpx.Response:
            if "archive" in request.url.host:
                db.execute(update(Dive).where(Dive.id == dive.id).values(weather="rain"))
                db.commit()
            return dahab_answers(request)

        with FakeOpenMeteo(typed_meanwhile):
            await run_dive_conditions(NOW)

        assert _fresh(db, dive).weather == "rain"
        row = _row(db, dive)
        assert row is not None and row.filled_weather is None and row.filled_air_temperature == 38.1

    @pytest.mark.asyncio
    async def test_a_dive_moved_while_the_provider_answers_gets_no_row(
        self, db: Session, mine: Callable[..., User]
    ) -> None:
        """The snapshot decided what to ask; a dive whose time moved meanwhile is answered for
        its new time on the next run, not given the old one's."""
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))

        def moved_meanwhile(request: httpx.Request) -> httpx.Response:
            if "archive" in request.url.host:
                db.execute(update(Dive).where(Dive.id == dive.id).values(start_time=DAHAB_DIVE + timedelta(hours=2)))
                db.commit()
            return dahab_answers(request)

        with FakeOpenMeteo(moved_meanwhile):
            assert "1 moved" in await run_dive_conditions(NOW)

        assert _row(db, dive) is None
        assert _fresh(db, dive).weather is None

    @pytest.mark.asyncio
    async def test_a_redis_outage_costs_the_bookkeeping_and_not_the_fill(
        self, db: Session, mine: Callable[..., User], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lock, the budget, the cool-down and the strike count all fail open."""
        monkeypatch.setattr(cache, "client", FakeRedis(failing=True))
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))

        with FakeOpenMeteo():
            await run_dive_conditions(NOW)

        row = _row(db, dive)
        assert row is not None and row.state == "fetched"
        assert _fresh(db, dive).weather == "clear"

    @pytest.mark.asyncio
    async def test_a_figure_the_provider_should_not_send_is_no_figure(
        self, db: Session, async_db: AsyncSession, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))

        with FakeOpenMeteo(_with(weather_code=2**31)):
            await run_dive_conditions(NOW)

        row = _row(db, dive)
        assert row is not None and row.weather_code is None
        assert _fresh(db, dive).weather is None
        assert (await _read(async_db, diver, dive))["conditions"]["weather"]["weather_code"] is None

    @pytest.mark.asyncio
    async def test_fresh_water_asks_nothing_about_the_sea(self, db: Session, mine: Callable[..., User]) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver, water_type="fresh"))

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        assert provider.asked("sea") == []
        row = _row(db, dive)
        assert row is not None and row.sea_model is None and row.weather_model == "best_match"
        assert _fresh(db, dive).waves is None

    @pytest.mark.asyncio
    async def test_a_sea_series_with_no_values_gives_no_sea(self, db: Session, mine: Callable[..., User]) -> None:
        """2019 at Dahab: before the marine model's start, every sample is null."""
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))

        def before_the_model(request: httpx.Request) -> httpx.Response:
            if "marine" in request.url.host:
                return httpx.Response(200, json=fixture("dahab-2019-06-10-marine.json"))
            return dahab_answers(request)

        with FakeOpenMeteo(before_the_model):
            await run_dive_conditions(NOW)

        row = _row(db, dive)
        assert row is not None and row.sea_model is None
        assert (_fresh(db, dive).weather, _fresh(db, dive).waves) == ("clear", None)

    @pytest.mark.asyncio
    async def test_a_dive_dated_today_is_answered(self, db: Session, mine: Callable[..., User]) -> None:
        """The archive refuses an end date after today, so the request stops at today."""
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver), start_time=NOW - timedelta(hours=3))

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        (weather,) = provider.asked("weather")
        assert (weather.url.params["start_date"], weather.url.params["end_date"]) == ("2026-10-23", "2026-10-23")
        row = _row(db, dive)
        assert row is not None and row.state == "fetched"

    @pytest.mark.asyncio
    async def test_the_entry_fix_is_the_position_before_the_pin(self, db: Session, mine: Callable[..., User]) -> None:
        diver = mine(db)
        _dive(db, diver, site=_site(db, diver), entry_latitude=27.91, entry_longitude=34.33)

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        params = provider.asked("weather")[0].url.params
        assert (params["latitude"], params["longitude"]) == ("27.91", "34.33")


class TestAWallClockWithoutAnOffset:
    @pytest.mark.asyncio
    async def test_a_winter_date_resolves_to_the_winter_offset(self, db: Session, mine: Callable[..., User]) -> None:
        """The zone's name is read from the answer, its offset on the dive's own date from
        `zoneinfo`: Cairo was +02:00 on 2026-01-15, though a request made in summer is
        labelled +03:00."""
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver), start_time=datetime(2026, 1, 15, 10, 0, tzinfo=UTC), offset=None)

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        weather = provider.asked("weather")[0].url.params
        assert weather["timezone"] == "auto"
        assert (weather["start_date"], weather["end_date"]) == ("2026-01-14", "2026-01-16")
        row = _row(db, dive)
        assert row is not None
        assert row.utc_offset_minutes == 120
        assert row.at == datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        assert _fresh(db, dive).utc_offset_minutes is None
        # Not re-candidated on the next run: NULL against NULL is not distinct.
        with FakeOpenMeteo() as again:
            await run_dive_conditions(NOW)
        assert again.requests == []


class TestTheDiversEdit:
    @pytest.mark.asyncio
    async def test_a_different_value_is_the_divers_and_clears_its_mark(
        self, db: Session, async_db: AsyncSession, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)

        await _patch(async_db, diver, dive, {"weather": "overcast"})

        row = _row(db, dive)
        assert row is not None
        assert (row.filled_weather, row.filled_air_temperature, row.filled_waves) == (None, 38.1, "slight")
        assert _fresh(db, dive).weather == "overcast"

    @pytest.mark.asyncio
    async def test_an_empty_value_from_a_stale_form_leaves_the_fill(
        self, db: Session, async_db: AsyncSession, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)

        await _patch(async_db, diver, dive, {"weather": None, "air_temperature": None, "waves": None, "rating": 4})

        refreshed = _fresh(db, dive)
        assert (refreshed.air_temperature, refreshed.weather, refreshed.waves, refreshed.rating) == (
            38.1,
            "clear",
            "slight",
            4,
        )
        row = _row(db, dive)
        assert row is not None and row.filled_weather == "clear"

    @pytest.mark.asyncio
    async def test_a_value_changed_by_another_route_loses_its_mark_on_the_next_run(
        self, db: Session, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)
        db.execute(update(Dive).where(Dive.id == dive.id).values(waves="rough"))
        db.commit()

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        assert provider.requests == []
        row = _row(db, dive)
        assert row is not None and row.filled_waves is None and row.filled_weather == "clear"


class TestWhenTheInputsChange:
    @pytest.mark.asyncio
    async def test_a_moved_time_drops_the_fetched_values_and_asks_again(
        self, db: Session, async_db: AsyncSession, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)
        await _patch(async_db, diver, dive, {"weather": "overcast"})
        await _patch(async_db, diver, dive, {"start_time": "2026-09-14T10:00:00+03:00"})

        # The new day's answer has no temperature, so the old fill must go rather than stay.
        with FakeOpenMeteo(_with(temperature_2m=None)) as provider:
            await run_dive_conditions(NOW + timedelta(minutes=5))

        assert provider.asked("weather")[0].url.params["start_date"] == "2026-09-14"
        row = _row(db, dive)
        assert row is not None
        assert row.at == datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
        assert row.fetched_at == NOW + timedelta(minutes=5)
        refreshed = _fresh(db, dive)
        assert refreshed.air_temperature is None
        assert refreshed.weather == "overcast"
        assert (row.filled_weather, row.filled_waves) == (None, "slight")

    @pytest.mark.asyncio
    async def test_a_moved_pin_asks_about_the_new_place(self, db: Session, mine: Callable[..., User]) -> None:
        diver = mine(db)
        site = _site(db, diver)
        dive = _dive(db, diver, site=site, weather="overcast")
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)
        db.execute(update(DiveSite).where(DiveSite.id == site.id).values(latitude=28.6, longitude=34.56))
        db.commit()

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        params = provider.asked("weather")[0].url.params
        assert (params["latitude"], params["longitude"]) == ("28.60", "34.56")
        row = _row(db, dive)
        assert row is not None and (row.latitude, row.longitude) == (28.6, 34.56)
        assert _fresh(db, dive).weather == "overcast"

    @pytest.mark.asyncio
    async def test_a_nudge_inside_the_square_asks_nothing(self, db: Session, mine: Callable[..., User]) -> None:
        diver = mine(db)
        site = _site(db, diver)
        _dive(db, diver, site=site)
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)
        db.execute(update(DiveSite).where(DiveSite.id == site.id).values(latitude=28.5721))
        db.commit()

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        assert provider.requests == []

    @pytest.mark.asyncio
    async def test_a_half_way_pin_rounds_the_same_on_both_sides(self, db: Session, mine: Callable[..., User]) -> None:
        """Python's `round(28.575, 2)` is 28.57 and Postgres's `round(28.575::numeric, 2)` is
        28.58; one rounding on both sides means the row it writes is not stale the next run."""
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver, position=(28.575, 34.537)))

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)
        assert provider.asked("weather")[0].url.params["latitude"] == "28.58"
        row = _row(db, dive)
        assert row is not None and row.input_latitude == 28.58

        with FakeOpenMeteo() as again:
            await run_dive_conditions(NOW)
        assert again.requests == []

    @pytest.mark.asyncio
    async def test_a_new_derivation_refills_a_marked_field_and_leaves_a_typed_one(
        self, db: Session, mine: Callable[..., User], monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver), air_temperature=30.0)
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)
        monkeypatch.setattr(dive_conditions, "DERIVATION_VERSION", dive_conditions.DERIVATION_VERSION + 1)
        # Past the answer cache's hour, so the provider is asked again and answers otherwise.
        redis.store.clear()

        with FakeOpenMeteo(_with(weather_code=3)):
            await run_dive_conditions(NOW)

        refreshed = _fresh(db, dive)
        assert (refreshed.weather, refreshed.air_temperature) == ("overcast", 30.0)
        row = _row(db, dive)
        assert row is not None
        assert row.derivation_version == dive_conditions.DERIVATION_VERSION
        assert (row.filled_weather, row.filled_air_temperature) == ("overcast", None)

    @pytest.mark.asyncio
    async def test_a_site_pinned_after_the_dive_was_skipped_answers_it(
        self, db: Session, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        site = _site(db, diver, position=None)
        dive = _dive(db, diver, site=site)
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)
        row = _row(db, dive)
        assert row is not None and (row.state, row.reason) == ("skipped", "no_position")

        db.execute(update(DiveSite).where(DiveSite.id == site.id).values(latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1]))
        db.commit()
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)

        row = _row(db, dive)
        assert row is not None and row.state == "fetched"


class TestWhatIsPassedOver:
    @pytest.mark.asyncio
    async def test_the_provider_unset_asks_nothing_and_writes_nothing(
        self, db: Session, mine: Callable[..., User], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_PROVIDER", "")
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))

        with FakeOpenMeteo() as provider:
            assert await fetch_dive_conditions({}) == "Dive conditions are off"

        assert provider.requests == []
        assert _row(db, dive) is None

    @pytest.mark.asyncio
    async def test_a_date_only_dive_is_skipped(self, db: Session, mine: Callable[..., User]) -> None:
        diver = mine(db)
        dive = _dive(
            db,
            diver,
            site=_site(db, diver),
            start_time=datetime(2026, 9, 15, tzinfo=UTC),
            offset=None,
            start_date_only=True,
        )

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        assert provider.requests == []
        row = _row(db, dive)
        assert row is not None and (row.state, row.reason) == ("skipped", "no_time")

    @pytest.mark.asyncio
    async def test_a_dive_with_no_position_is_skipped_and_logged(
        self, db: Session, mine: Callable[..., User], caplog: pytest.LogCaptureFixture
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver)

        with FakeOpenMeteo() as provider, caplog.at_level("INFO"):
            await run_dive_conditions(NOW)

        assert provider.requests == []
        row = _row(db, dive)
        assert row is not None and (row.state, row.reason) == ("skipped", "no_position")
        assert f"Dive {dive.id} skipped: no position." in caplog.text

    @pytest.mark.asyncio
    async def test_a_deleted_accounts_dives_get_no_row(self, db: Session, mine: Callable[..., User]) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        db.execute(update(User).where(User.id == diver.id).values(is_deleted=True, deleted_at=NOW))
        db.commit()

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        assert provider.requests == []
        assert _row(db, dive) is None

    @pytest.mark.asyncio
    async def test_a_future_dive_gets_no_row_until_its_day(self, db: Session, mine: Callable[..., User]) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver), start_time=NOW + timedelta(days=2))

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)
        assert provider.requests == []
        assert _row(db, dive) is None

        with FakeOpenMeteo():
            await run_dive_conditions(NOW + timedelta(days=2, hours=3))
        row = _row(db, dive)
        assert row is not None and row.state == "fetched"


class TestFailures:
    @pytest.mark.asyncio
    async def test_a_refused_request_backs_off_and_is_charged_to_the_dive(
        self, db: Session, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        refused = FakeOpenMeteo(lambda request: httpx.Response(400, json={"error": True, "reason": "range"}))

        with refused:
            await run_dive_conditions(NOW)
        row = _row(db, dive)
        assert row is not None
        assert (row.state, row.reason, row.attempts) == ("failed", "weather_400", 1)
        assert row.next_attempt_at == NOW + timedelta(hours=1)

        with refused:
            await run_dive_conditions(NOW + timedelta(minutes=30))
        assert len(refused.requests) == 1

        with refused:
            await run_dive_conditions(NOW + timedelta(hours=1))
        row = _row(db, dive)
        assert row is not None and row.attempts == 2
        assert row.next_attempt_at == NOW + timedelta(hours=3)
        assert (row.filled_weather, _fresh(db, dive).weather) == (None, None)

    @pytest.mark.asyncio
    async def test_a_dive_given_up_on_is_asked_afresh_after_an_input_change(
        self, db: Session, async_db: AsyncSession, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        refused = FakeOpenMeteo(lambda request: httpx.Response(400, json={"error": True, "reason": "range"}))
        with refused:
            await run_dive_conditions(NOW)
        db.execute(
            update(DiveConditions)
            .where(DiveConditions.dive_id == dive.id)
            .values(attempts=dive_conditions.ATTEMPTS_CAP, deferrals=3, next_attempt_at=None)
        )
        db.commit()
        await _patch(async_db, diver, dive, {"start_time": "2026-09-14T10:00:00+03:00"})

        with refused:
            await run_dive_conditions(NOW)

        row = _row(db, dive)
        assert row is not None
        assert (row.attempts, row.deferrals) == (1, 0)
        assert row.next_attempt_at == NOW + timedelta(hours=1)

    @pytest.mark.asyncio
    async def test_a_429_stops_the_run_and_cools_down(
        self, db: Session, mine: Callable[..., User], redis: FakeRedis
    ) -> None:
        diver = mine(db)
        site = _site(db, diver)
        older, newer = _dive(db, diver, site=site), _dive(db, diver, site=site)

        with FakeOpenMeteo(lambda request: httpx.Response(429)) as provider:
            await run_dive_conditions(NOW)

        assert len(provider.requests) == 1
        assert _row(db, older) is None and _row(db, newer) is None
        assert redis.expiries[COOLDOWN_KEY] == 15 * 60

        with FakeOpenMeteo() as later:
            assert "Cooling down" in await run_dive_conditions(NOW)
        assert later.requests == []

    @pytest.mark.asyncio
    async def test_a_refused_key_stops_the_run_for_an_hour(
        self, db: Session, mine: Callable[..., User], redis: FakeRedis, caplog: pytest.LogCaptureFixture
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))

        with FakeOpenMeteo(lambda request: httpx.Response(401)):
            await run_dive_conditions(NOW)

        assert _row(db, dive) is None
        assert redis.expiries[COOLDOWN_KEY] == 60 * 60
        assert "DIVE_CONDITIONS_OPEN_METEO_API_KEY" in caplog.text

    @pytest.mark.asyncio
    async def test_over_budget_leaves_every_dive_pending(
        self, db: Session, mine: Callable[..., User], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 0)
        diver = mine(db)
        site = _site(db, diver)
        dives = [_dive(db, diver, site=site), _dive(db, diver, site=site)]

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        assert provider.requests == []
        assert all(_row(db, dive) is None for dive in dives)

    @pytest.mark.asyncio
    async def test_a_timeout_moves_on_to_the_next_dive(self, db: Session, mine: Callable[..., User]) -> None:
        diver = mine(db)
        stuck = _dive(db, diver, site=_site(db, diver, position=(10.0, 10.0)))
        fine = _dive(db, diver, site=_site(db, diver), start_time=DAHAB_DIVE - timedelta(days=1))

        def slow_at_one_place(request: httpx.Request) -> httpx.Response:
            if request.url.params["latitude"] == "10.00":
                raise httpx.ReadTimeout("slow", request=request)
            return dahab_answers(request)

        with FakeOpenMeteo(slow_at_one_place):
            await run_dive_conditions(NOW)

        assert _row(db, stuck) is None
        fetched = _row(db, fine)
        assert fetched is not None and fetched.state == "fetched"

    @pytest.mark.asyncio
    async def test_two_failures_in_a_row_stop_the_run(self, db: Session, mine: Callable[..., User]) -> None:
        diver = mine(db)
        for latitude in (10.0, 11.0, 12.0):
            _dive(db, diver, site=_site(db, diver, position=(latitude, 10.0)))

        with FakeOpenMeteo(lambda request: httpx.Response(503)) as provider:
            await run_dive_conditions(NOW)

        assert len(provider.requests) == 2

    @pytest.mark.asyncio
    async def test_the_third_timeout_on_one_dive_defers_it(self, db: Session, mine: Callable[..., User]) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))

        def times_out(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        for minute in range(3):
            with FakeOpenMeteo(times_out):
                await run_dive_conditions(NOW + timedelta(minutes=minute))
            if minute < 2:
                assert _row(db, dive) is None

        row = _row(db, dive)
        assert row is not None
        assert (row.state, row.reason, row.attempts, row.deferrals) == ("failed", "deferred", 0, 1)
        assert row.next_attempt_at == NOW + timedelta(minutes=2, hours=1)

        with FakeOpenMeteo(times_out) as provider:
            await run_dive_conditions(NOW + timedelta(minutes=30))
        assert provider.requests == []

        with FakeOpenMeteo(times_out):
            await run_dive_conditions(NOW + timedelta(hours=2))
        row = _row(db, dive)
        assert row is not None and row.deferrals == 2
        assert row.next_attempt_at == NOW + timedelta(hours=4)

        with FakeOpenMeteo():
            await run_dive_conditions(NOW + timedelta(hours=4))
        row = _row(db, dive)
        assert row is not None and row.state == "fetched"


class TestTheRunsOwnLock:
    @pytest.mark.asyncio
    async def test_a_run_while_one_holds_the_lock_returns_at_once(
        self, db: Session, mine: Callable[..., User], redis: FakeRedis
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        await redis.set(LOCK_KEY, "another run", nx=True, px=LOCK_LIFETIME_SECONDS * 1000)

        with FakeOpenMeteo() as provider:
            assert await run_dive_conditions(NOW) == "A run is already in progress"

        assert provider.requests == []
        assert _row(db, dive) is None
        assert redis.store[LOCK_KEY] == b"another run"

    @pytest.mark.asyncio
    async def test_the_lock_lapses_by_itself_and_is_released_after_a_run(
        self, db: Session, mine: Callable[..., User], redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Held for the run's longest allowed length, so a run killed mid-flight costs one
        lifetime; a lapsed lock - `FakeRedis` records expiries without enforcing them, so the
        lapse is the key's removal - lets the next run in."""
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        taken: list[int | None] = []
        real_set = redis.set

        async def watching(key: str, value: object, **kwargs: Any) -> bool | None:
            if key == LOCK_KEY:
                taken.append(kwargs.get("px"))
            return await real_set(key, value, **kwargs)

        monkeypatch.setattr(redis, "set", watching)
        await redis.set(LOCK_KEY, "a killed run", nx=True, px=LOCK_LIFETIME_SECONDS * 1000)
        await redis.delete(LOCK_KEY)

        with FakeOpenMeteo():
            await run_dive_conditions(NOW)

        assert LOCK_LIFETIME_SECONDS * 1000 in taken
        assert LOCK_LIFETIME_SECONDS >= dive_conditions.RUN_SECONDS + 2 * 40
        assert LOCK_KEY not in redis.store
        row = _row(db, dive)
        assert row is not None and row.state == "fetched"

    def test_the_cron_keeps_arqs_per_tick_ids(self) -> None:
        (job,) = [job for job in WorkerSettings.cron_jobs if job.name == "cron:fetch_dive_conditions"]
        assert job.job_id is None
        assert job.minute is None


class TestExportAndImport:
    @staticmethod
    async def _export(async_db: AsyncSession, user_id: int) -> dict[str, Any]:
        bundle = await load_export_bundle(async_db, user_id=user_id)
        chunks = [chunk async for chunk in write_divejson(async_db, bundle, exported_at=NOW)]
        await async_db.rollback()
        document: dict[str, Any] = json.loads(b"".join(chunks))
        return document

    @staticmethod
    async def _import(async_db: AsyncSession, user_id: int, document: dict[str, Any]) -> None:
        with await load_one(json.dumps(document).encode(), "logbook.divejson") as loaded:
            plan = await plan_import(async_db, user_id=user_id, loaded=loaded, resolution_ran=True, claim_links=True)
            await write_import(async_db, user_id=user_id, loaded=loaded, plan=plan)
            await async_db.commit()

    @pytest.mark.asyncio
    async def test_the_record_round_trips_without_a_fetch(
        self, db: Session, async_db: AsyncSession, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        dive = _dive(db, diver, site=_site(db, diver))
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)

        document = await self._export(async_db, diver.id)
        (exported,) = document["dives"]
        conditions = exported["extensions"]["opendiving"]["conditions"]
        assert conditions["filled"] == {"air_temperature": 38.1, "weather": "clear", "waves": "slight"}
        assert (exported["air_temperature"], exported["weather"], exported["waves"]) == (38.1, "clear", "slight")
        # Changed in another tool that kept the record: imported as the diver's own.
        exported["weather"] = "rain"

        destination = mine(db)
        await self._import(async_db, destination.id, document)

        imported = db.execute(select(Dive).where(Dive.user_id == destination.id)).scalar_one()
        row = _row(db, imported)
        original = _row(db, dive)
        assert row is not None and original is not None
        assert row.fetched_at == original.fetched_at
        assert (row.filled_air_temperature, row.filled_weather, row.filled_waves) == (38.1, None, "slight")
        assert imported.weather == "rain"
        assert row.tide_state == "high"

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)
        assert provider.requests == []

    @pytest.mark.asyncio
    async def test_a_record_of_another_time_is_answered_afresh(
        self, db: Session, async_db: AsyncSession, mine: Callable[..., User]
    ) -> None:
        """A dive retimed in another tool comes back carrying the old record; its inputs are
        left null, so the worker finds it stale."""
        diver = mine(db)
        _dive(db, diver, site=_site(db, diver))
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)
        document = await self._export(async_db, diver.id)
        document["dives"][0]["started_at"] = "2026-09-14T10:00:00+03:00"

        destination = mine(db)
        await self._import(async_db, destination.id, document)
        imported = db.execute(select(Dive).where(Dive.user_id == destination.id)).scalar_one()
        row = _row(db, imported)
        assert row is not None and row.input_start_time is None

        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)
        assert provider.asked("weather")[0].url.params["start_date"] == "2026-09-14"
        row = _row(db, imported)
        assert row is not None and row.at == datetime(2026, 9, 14, 7, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("member", "value"),
        [
            ("utc_offset_minutes", 1440),
            ("provider", "x" * 33),
            ("sea.tide_state", "slack"),
            ("weather.weather_code", 2**31),
            ("weather.air_temperature", float("nan")),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_record_that_would_not_fit_is_left_out(
        self, db: Session, async_db: AsyncSession, mine: Callable[..., User], member: str, value: Any
    ) -> None:
        """Another tool can write anything under the key; the dive still imports, with no row."""
        diver = mine(db)
        _dive(db, diver, site=_site(db, diver))
        with FakeOpenMeteo():
            await run_dive_conditions(NOW)
        document = await self._export(async_db, diver.id)
        entry = document["dives"][0]["extensions"]["opendiving"]["conditions"]
        *path, last = member.split(".")
        for step in path:
            entry = entry[step]
        entry[last] = value

        destination = mine(db)
        await self._import(async_db, destination.id, document)

        imported = db.execute(select(Dive).where(Dive.user_id == destination.id)).scalar_one()
        assert _row(db, imported) is None
        assert imported.weather == "clear"

    @pytest.mark.asyncio
    async def test_a_dive_without_a_record_carries_no_extension(
        self, db: Session, async_db: AsyncSession, mine: Callable[..., User]
    ) -> None:
        diver = mine(db)
        _dive(db, diver)
        document = await self._export(async_db, diver.id)
        assert "extensions" not in document["dives"][0]
