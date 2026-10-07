"""`GET /dives/recent-volumes`: the distinct cylinder volumes the caller's dives used, the most
recently used first, bounded by `until` as the picker lookups are.

The key and the route's wiring run anywhere; the ordering and the exclusions run against
Postgres.
"""

import fnmatch
from collections.abc import Generator
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import dives as dives_module
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.core.utils.cache import _format_prefix
from src.app.models.dive import Dive
from src.app.models.dive_mixture import DiveMixture
from src.app.models.user import User
from src.app.schemas.lookup import lookup_bound
from tests.conftest import db_available

CURRENT_USER = {"id": 7, "uuid": uuid7(), "username": "ada", "is_superuser": False}

PATH = "/api/v1/dives/recent-volumes"


class TestTheCacheKey:
    def test_it_sits_under_the_sweep_every_dive_write_runs(self) -> None:
        key = _format_prefix(dives_module._RECENT_VOLUMES_CACHE_KEY_PREFIX, {"user_id": 7, "until": None})

        assert fnmatch.fnmatchcase(key, "user_7_dives:*")

    def test_two_spellings_of_one_instant_share_it_and_two_instants_do_not(self) -> None:
        def key(until: datetime | date) -> str:
            return _format_prefix(
                dives_module._RECENT_VOLUMES_CACHE_KEY_PREFIX, {"user_id": 7, "until": lookup_bound(until)}
            )

        assert key(datetime.fromisoformat("2026-06-02T10:00:00+02:00")) == key(datetime(2026, 6, 2, 8, 0, tzinfo=UTC))
        assert key(date(2026, 6, 2)) != key(datetime(2026, 6, 2, 8, 0, tzinfo=UTC))


class _FakeRedis:
    """Enough of the client for `@cache` to run; the app's lifespan points a real pool at the
    compose hostname, which does not resolve here."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value.encode()

    async def expire(self, key: str, seconds: int) -> None:
        pass


@pytest.fixture(scope="module")
def app() -> Any:
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def client(app: Any) -> Generator[TestClient]:
    # After the lifespan, which assigns the cache client on startup.
    app.dependency_overrides[get_current_user] = lambda: CURRENT_USER
    app.dependency_overrides[async_get_db] = lambda: MagicMock()
    with TestClient(app) as test_client, patch("src.app.core.utils.cache.client", _FakeRedis()):
        yield test_client
    app.dependency_overrides = {}


class TestTheRoute:
    @pytest.mark.parametrize(
        ("params", "bound"),
        [
            ({}, None),
            ({"until": "2026-06-02T10:00:00+02:00"}, datetime(2026, 6, 2, 8, 0, tzinfo=UTC)),
            ({"until": "2026-06-02T10:00:00"}, datetime(2026, 6, 2, 10, 0, tzinfo=UTC)),
            ({"until": "2026-06-02"}, datetime(2026, 6, 2, 23, 59, 59, 999999, tzinfo=UTC)),
        ],
    )
    def test_until_reaches_the_query_as_the_lookups_read_it(
        self, client: TestClient, params: dict[str, str], bound: datetime | None
    ) -> None:
        query = AsyncMock(return_value=[11.1, 15.0])
        with patch.object(dives_module, "get_recent_volumes", query):
            response = client.get(PATH, params=params)

        assert response.status_code == 200, response.text
        assert response.json() == {"volumes": [11.1, 15.0]}
        assert query.await_args is not None
        assert query.await_args.kwargs == {"user_id": CURRENT_USER["id"], "bound": bound, "limit": 10}

    def test_anything_else_is_a_422(self, client: TestClient) -> None:
        assert client.get(PATH, params={"until": "yesterday"}).status_code == 422


# ------------------------------------------------------------------ against Postgres


def _day(day: int) -> datetime:
    return datetime(2026, 6, day, 9, 0, tzinfo=UTC)


def _dive(db: Session, user: User, start: datetime, *volumes: float | None, is_deleted: bool = False) -> Dive:
    dive = Dive(user_id=user.id, dive_number=1, start_time=start, duration=1800, notes="", is_deleted=is_deleted)
    db.add(dive)
    db.commit()
    db.add_all(DiveMixture(dive_id=dive.id, volume=volume) for volume in volumes)
    db.commit()
    return dive


async def _volumes(async_db: AsyncSession, user: User, until: datetime | None = None) -> list[float]:
    # Past `@cache`, which these tests have no Redis for.
    helper: Any = dives_module._cached_recent_volumes
    result = await helper.__wrapped__(None, user_id=user.id, db=async_db, until=until)
    volumes: list[float] = result.volumes
    return volumes


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheVolumes:
    @pytest.mark.asyncio
    async def test_by_last_use_then_the_smaller_each_once(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        _dive(db, diver, _day(1), 12.0)
        _dive(db, diver, _day(3), 15.0, 11.1)
        _dive(db, diver, _day(5), 12.0, 12.0)

        assert await _volumes(async_db, diver) == [12.0, 11.1, 15.0]

    @pytest.mark.asyncio
    async def test_at_most_ten_the_most_recent(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        for day in range(1, 13):
            _dive(db, diver, _day(day), float(day))

        assert await _volumes(async_db, diver) == [float(day) for day in range(12, 2, -1)]

    @pytest.mark.asyncio
    async def test_a_dive_after_until_counts_for_nothing(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        _dive(db, diver, _day(1), 12.0)
        _dive(db, diver, _day(3), 15.0)
        _dive(db, diver, _day(5), 12.0, 7.0)

        # 12.0 ranks by its use on the 1st; 7.0 was only ever used after the bound.
        assert await _volumes(async_db, diver, _day(4)) == [15.0, 12.0]
        # Inclusive: the dive at the bound itself counts.
        assert await _volumes(async_db, diver, _day(3)) == [15.0, 12.0]
        assert await _volumes(async_db, diver, _day(2)) == [12.0]

    @pytest.mark.asyncio
    async def test_a_date_only_dive_on_a_bare_date_s_day_counts(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        dive = _dive(db, diver, datetime(2026, 6, 2, tzinfo=UTC), 10.0)
        dive.utc_offset_minutes, dive.start_date_only = None, True
        db.commit()
        _dive(db, diver, datetime(2026, 6, 3, 1, 0, tzinfo=UTC), 18.0)

        assert await _volumes(async_db, diver, lookup_bound(date(2026, 6, 2))) == [10.0]

    @pytest.mark.asyncio
    async def test_deleted_dives_other_divers_and_unrecorded_volumes_count_for_nothing(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        _dive(db, diver, _day(1), 12.0, None)
        _dive(db, diver, _day(5), 15.0, is_deleted=True)
        _dive(db, other_diver, _day(6), 18.0)
        _dive(db, diver, _day(7), None)

        assert await _volumes(async_db, diver) == [12.0]

    @pytest.mark.asyncio
    async def test_an_empty_log_is_an_empty_list(self, async_db: AsyncSession, diver: User) -> None:
        assert await _volumes(async_db, diver) == []
