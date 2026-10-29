"""`GET /dive-conditions/lookup`, the dive form's preview of what the conditions worker fills.

The handler runs against Postgres with Open-Meteo behind a mock transport and Redis as
`FakeRedis`, as the worker's tests do; the query parsing and the disabled instance run through
FastAPI on an app with no database behind it.
"""

from collections.abc import AsyncGenerator, Generator
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient
from fastcrud.exceptions.http_exceptions import NotFoundException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import dive_conditions as lookup_module
from src.app.core.config import settings
from src.app.core.db.database import async_engine, async_get_db
from src.app.core.setup import create_application
from src.app.core.utils import cache
from src.app.models import Dive, DiveConditions, DiveDiveSite, DiveSite, User
from src.app.schemas.dive import WaterType
from src.app.schemas.dive_conditions import ConditionsFilled, DiveConditionsLookupRead
from src.app.services import dive_conditions, open_meteo
from src.app.services.dive_conditions import COOLDOWN_KEY, run_dive_conditions
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.generators import create_user
from tests.helpers.open_meteo import FakeOpenMeteo, dahab_answers

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
# 10:00 at +03:00 at the Blue Hole.
DAHAB_DIVE = datetime(2026, 9, 15, 10, 0, tzinfo=timezone(timedelta(hours=3)))
BLUE_HOLE = (28.572, 34.537)

needs_db = pytest.mark.skipif(not db_available(), reason="Postgres is not reachable")


@pytest.fixture(autouse=True)
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(cache, "client", fake)
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_PROVIDER", "open-meteo")
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_API_KEY", None)
    monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 400)
    return fake


@pytest.fixture(autouse=True)
def per_user_limit() -> Generator[AsyncMock]:
    with patch.object(lookup_module, "enforce_rate_limit", new_callable=AsyncMock) as limit:
        yield limit


def _caller(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "is_superuser": False}


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


async def _lookup(
    async_db: AsyncSession,
    user: User,
    start_time: datetime | date = DAHAB_DIVE,
    *,
    site: DiveSite | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    water_type: WaterType | None = None,
) -> dict[str, Any]:
    result = await lookup_module.read_dive_conditions_lookup(
        current_user=_caller(user),
        db=async_db,
        start_time=start_time,
        dive_site_uuid=None if site is None else site.uuid,
        latitude=latitude,
        longitude=longitude,
        water_type=water_type,
    )
    await async_db.rollback()
    return result.model_dump(mode="json")


NOTHING = {"air_temperature": None, "weather": None, "waves": None}


@needs_db
class TestAnAnswer:
    @pytest.mark.asyncio
    async def test_the_sites_pin_is_answered_with_the_workers_fill(
        self, db: Session, async_db: AsyncSession, per_user_limit: AsyncMock
    ) -> None:
        diver = create_user(db)
        site = _site(db, diver)

        with FakeOpenMeteo() as provider:
            body = await _lookup(async_db, diver, site=site)

        assert body["provider"] == "open-meteo"
        assert body["attribution"] == open_meteo.ATTRIBUTION
        assert body["at"] == "2026-09-15T10:00:00+03:00"
        assert body["utc_offset_minutes"] == 180
        # What the worker writes for the same dive, in `test_dive_conditions.py`.
        assert body["filled"] == {"air_temperature": 38.1, "weather": "clear", "waves": "slight"}
        weather = provider.asked("weather")[0].url.params
        assert (weather["latitude"], weather["longitude"]) == ("28.57", "34.54")
        assert (weather["start_date"], weather["end_date"]) == ("2026-09-15", "2026-09-16")
        assert len(provider.asked("sea")) == 1
        assert per_user_limit.await_args is not None
        assert per_user_limit.await_args.args[0] == f"dive-conditions-lookup:user:{diver.id}"

    @pytest.mark.asyncio
    async def test_a_wall_clock_is_read_in_the_zone_the_provider_names(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)

        with FakeOpenMeteo() as provider:
            body = await _lookup(
                async_db, diver, datetime(2026, 1, 15, 10, 0), latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1]
            )

        weather = provider.asked("weather")[0].url.params
        assert weather["timezone"] == "auto"
        assert (weather["start_date"], weather["end_date"]) == ("2026-01-14", "2026-01-16")
        assert (body["at"], body["utc_offset_minutes"]) == ("2026-01-15T10:00:00+02:00", 120)

    @pytest.mark.asyncio
    async def test_an_explicit_position_wins_over_the_sites_pin(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        site = _site(db, diver)

        with FakeOpenMeteo() as provider:
            await _lookup(async_db, diver, site=site, latitude=27.9123, longitude=34.3256)

        for request in provider.requests:
            assert (request.url.params["latitude"], request.url.params["longitude"]) == ("27.91", "34.33")

    @pytest.mark.asyncio
    async def test_fresh_water_asks_nothing_of_the_sea(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        site = _site(db, diver, water_type="salt")

        with FakeOpenMeteo() as provider:
            body = await _lookup(async_db, diver, site=site, water_type=WaterType.FRESH)

        assert provider.asked("sea") == []
        assert body["filled"] == {"air_temperature": 38.1, "weather": "clear", "waves": None}

    @pytest.mark.asyncio
    async def test_the_sites_fresh_water_counts_where_the_form_names_none(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        site = _site(db, diver, water_type="fresh")

        with FakeOpenMeteo() as provider:
            await _lookup(async_db, diver, site=site)

        assert provider.asked("sea") == []

    @pytest.mark.asyncio
    async def test_nothing_is_written(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        site = _site(db, diver)

        with FakeOpenMeteo():
            await _lookup(async_db, diver, site=site)

        db.expire_all()
        assert db.execute(select(Dive).where(Dive.user_id == diver.id)).first() is None
        assert db.execute(select(DiveConditions).join(Dive).where(Dive.user_id == diver.id)).first() is None


@needs_db
class TestTheWorkersLaterFetch:
    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        """The cron opens its own session from the module-level `local_session`, and a pooled
        asyncpg connection belongs to the loop that opened it."""
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @pytest.mark.asyncio
    async def test_reads_the_answer_the_preview_cached_and_writes_what_it_showed(
        self, db: Session, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        diver = create_user(db)
        site = _site(db, diver)
        real = dive_conditions._candidates

        async def mine(session: AsyncSession, now: datetime, limit: int) -> list[Any]:
            return [one for one in await real(session, now, 1_000_000) if one.user_id == diver.id][:limit]

        monkeypatch.setattr(dive_conditions, "_candidates", mine)
        with FakeOpenMeteo():
            shown = (await _lookup(async_db, diver, site=site))["filled"]

        # Saved with the previewed fields blank, as the form sends them.
        dive = Dive(
            user_id=diver.id,
            dive_number=1,
            start_time=DAHAB_DIVE.astimezone(UTC),
            utc_offset_minutes=180,
            submerged_time=2400,
            notes="",
        )
        db.add(dive)
        db.commit()
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=0))
        db.commit()
        with FakeOpenMeteo() as provider:
            await run_dive_conditions(NOW)

        assert provider.requests == []
        db.expire_all()
        written = db.get_one(Dive, dive.id)
        assert {"air_temperature": written.air_temperature, "weather": written.weather, "waves": written.waves} == (
            shown
        )


@needs_db
class TestInputsWithNothingToAnswer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "start_time",
        [date(2026, 9, 15), datetime.now(UTC) + timedelta(days=2)],
        ids=["a date alone", "a day after today"],
    )
    async def test_every_value_is_null_and_nothing_is_asked(
        self, db: Session, async_db: AsyncSession, start_time: datetime | date
    ) -> None:
        diver = create_user(db)
        site = _site(db, diver)

        with FakeOpenMeteo() as provider:
            body = await _lookup(async_db, diver, start_time, site=site)

        assert provider.requests == []
        assert (body["at"], body["utc_offset_minutes"], body["filled"]) == (None, None, NOTHING)
        assert body["attribution"] == open_meteo.ATTRIBUTION

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "start_time",
        [datetime(1, 1, 1), datetime(1939, 12, 31, 10, 0, tzinfo=timezone(timedelta(hours=3)))],
        ids=["the calendar's first day with no offset", "the day before the archive"],
    )
    async def test_a_day_before_the_archive_is_null_and_nothing_is_asked(
        self, db: Session, async_db: AsyncSession, start_time: datetime
    ) -> None:
        diver = create_user(db)
        site = _site(db, diver)

        with FakeOpenMeteo() as provider:
            body = await _lookup(async_db, diver, start_time, site=site)

        assert provider.requests == []
        assert (body["at"], body["utc_offset_minutes"], body["filled"]) == (None, None, NOTHING)

    @pytest.mark.asyncio
    async def test_no_position_is_null(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        unpinned = _site(db, diver, position=None)

        with FakeOpenMeteo() as provider:
            alone = await _lookup(async_db, diver)
            with_site = await _lookup(async_db, diver, site=unpinned)

        assert provider.requests == []
        assert alone["filled"] == with_site["filled"] == NOTHING

    @pytest.mark.asyncio
    async def test_a_request_the_provider_refuses_is_null(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)

        with FakeOpenMeteo(lambda request: httpx.Response(400, json={"error": True})):
            body = await _lookup(async_db, diver, latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1])

        assert (body["at"], body["filled"]) == (None, NOTHING)


@needs_db
class TestTheSite:
    @pytest.mark.asyncio
    async def test_another_divers_site_is_a_404(self, db: Session, async_db: AsyncSession) -> None:
        diver, other = create_user(db), create_user(db)
        theirs = _site(db, other)

        with FakeOpenMeteo() as provider, pytest.raises(NotFoundException):
            await _lookup(async_db, diver, site=theirs)

        assert provider.requests == []


@needs_db
class TestTheProviderCouldNotBeAsked:
    @pytest.mark.asyncio
    async def test_a_failing_provider_is_a_503(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)

        with FakeOpenMeteo(lambda request: httpx.Response(502)), pytest.raises(HTTPException) as raised:
            await _lookup(async_db, diver, latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1])

        assert raised.value.status_code == 503
        assert raised.value.detail

    @pytest.mark.asyncio
    async def test_a_failing_sea_call_is_a_503_too(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)

        def no_sea(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503) if "marine" in request.url.host else dahab_answers(request)

        with FakeOpenMeteo(no_sea), pytest.raises(HTTPException) as raised:
            await _lookup(async_db, diver, latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1])

        assert raised.value.status_code == 503

    @pytest.mark.asyncio
    async def test_a_spent_budget_is_a_503_and_shared_with_the_worker(
        self, db: Session, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        diver = create_user(db)
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 1)

        with FakeOpenMeteo() as provider, pytest.raises(HTTPException) as raised:
            # The weather call takes the one slot; the sea call finds none.
            await _lookup(async_db, diver, latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1])

        assert raised.value.status_code == 503
        assert len(provider.requests) == 1

    @pytest.mark.asyncio
    async def test_a_rate_limit_cools_the_worker_down_too(
        self, db: Session, async_db: AsyncSession, redis: FakeRedis
    ) -> None:
        diver = create_user(db)

        with FakeOpenMeteo(lambda request: httpx.Response(429)), pytest.raises(HTTPException):
            await _lookup(async_db, diver, latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1])

        assert await redis.get(COOLDOWN_KEY) is not None
        assert await run_dive_conditions(NOW) == "Cooling down after the provider refused a request"

    @pytest.mark.asyncio
    async def test_a_cool_down_asks_nothing(self, db: Session, async_db: AsyncSession, redis: FakeRedis) -> None:
        diver = create_user(db)
        await redis.set(COOLDOWN_KEY, "1", ex=60)

        with FakeOpenMeteo() as provider, pytest.raises(HTTPException) as raised:
            await _lookup(async_db, diver, latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1])

        assert raised.value.status_code == 503
        assert provider.requests == []

    @pytest.mark.asyncio
    async def test_the_form_is_not_kept_waiting_on_the_workers_deadline(
        self, db: Session, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        diver = create_user(db)
        asked: list[tuple[float, float]] = []
        real = open_meteo.fetch_capped

        async def recorded(url: str, **kwargs: Any) -> Any:
            asked.append((kwargs["timeout"], kwargs["deadline"]))
            return await real(url, **kwargs)

        monkeypatch.setattr(open_meteo, "fetch_capped", recorded)
        with FakeOpenMeteo():
            await _lookup(async_db, diver, latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1])

        assert asked == [(open_meteo.PREVIEW_TIMEOUT_SECONDS, open_meteo.PREVIEW_DEADLINE_SECONDS)] * 2
        assert open_meteo.PREVIEW_DEADLINE_SECONDS < open_meteo.DEADLINE_SECONDS


@pytest.fixture(scope="module")
def lookup_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`, as the geocoder's tests build one."""
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


class TestTheRoute:
    """Through FastAPI, with the preview stubbed: what reaches it from the query string, and
    what never gets that far."""

    @pytest.fixture
    def preview(self) -> Generator[AsyncMock]:
        answered = DiveConditionsLookupRead(
            provider="open-meteo",
            attribution=open_meteo.ATTRIBUTION,
            filled=ConditionsFilled(air_temperature=None, weather=None, waves=None),
        )
        with patch.object(lookup_module, "preview_conditions", new_callable=AsyncMock, return_value=answered) as stub:
            yield stub

    @pytest.fixture
    def client(self, lookup_app: Any) -> Generator[TestClient]:
        lookup_app.dependency_overrides[get_current_user] = lambda: {"id": 1, "uuid": "x", "is_superuser": False}
        lookup_app.dependency_overrides[async_get_db] = lambda: MagicMock()
        with TestClient(lookup_app) as test_client:
            yield test_client
        lookup_app.dependency_overrides = {}

    @pytest.mark.parametrize(
        ("sent", "reached"),
        [
            ("2026-09-15T10:00:00+03:00", datetime(2026, 9, 15, 10, 0, tzinfo=timezone(timedelta(hours=3)))),
            ("2026-09-15T10:00:00", datetime(2026, 9, 15, 10, 0)),
            ("2026-09-15", date(2026, 9, 15)),
        ],
    )
    def test_every_shape_the_form_holds_reaches_the_preview(
        self, client: TestClient, preview: AsyncMock, sent: str, reached: datetime | date
    ) -> None:
        response = client.get("/api/v1/dive-conditions/lookup", params={"start_time": sent, "water_type": "fresh"})

        assert response.status_code == 200, response.text
        assert preview.await_args is not None
        assert preview.await_args.kwargs["start_time"] == reached
        assert type(preview.await_args.kwargs["start_time"]) is type(reached)
        assert preview.await_args.kwargs["water_type"] == "fresh"

    @pytest.mark.parametrize("half", [{"latitude": 28.57}, {"longitude": 34.54}])
    def test_half_a_position_is_a_422(self, client: TestClient, preview: AsyncMock, half: dict[str, float]) -> None:
        response = client.get("/api/v1/dive-conditions/lookup", params={"start_time": "2026-09-15", **half})

        assert response.status_code == 422
        preview.assert_not_awaited()

    def test_a_start_time_is_required(self, client: TestClient, preview: AsyncMock) -> None:
        assert client.get("/api/v1/dive-conditions/lookup").status_code == 422

    def test_an_instance_fetching_no_conditions_has_no_route(
        self, lookup_app: Any, preview: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_PROVIDER", "")

        # Signed out, so the 404 is the route's absence rather than anything about the caller.
        with TestClient(lookup_app) as anonymous:
            response = anonymous.get("/api/v1/dive-conditions/lookup", params={"start_time": "2026-09-15"})

        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
        preview.assert_not_awaited()

    def test_signed_out_is_a_401_where_conditions_are_fetched(self, lookup_app: Any) -> None:
        with TestClient(lookup_app) as anonymous:
            response = anonymous.get("/api/v1/dive-conditions/lookup", params={"start_time": "2026-09-15"})

        assert response.status_code == 401
