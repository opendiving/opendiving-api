"""Tests that a dive read stops naming a dive site or trip the diver has deleted.

Both loaders keep their links when the thing they point at is soft-deleted - that is what
lets an export read the record back - so nothing but the query's own `WHERE` decides
whether the app renders an orphan. A stubbed session answers with whatever rows the stub
was handed, which is exactly the question here, so these run against a live Postgres and
skip themselves otherwise. See CONTRIBUTING.md for why a run on the host needs
`POSTGRES_SERVER=localhost` to make them execute.

The route-level halves - `erase_trip` and `erase_dive_site` dropping the dive caches so a
cached read cannot outlive the filter - live in `test_move_dives_on_delete.py`, against the
stubs that can see the invalidation call.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.core.config import settings
from src.app.core.db.database import Base
from src.app.crud.crud_dive_dive_sites import (
    get_dive_sites_for_dive,
    get_dive_sites_for_dives,
    replace_dive_sites_for_dive,
)
from src.app.crud.crud_trips import get_trip_uuids_by_ids
from src.app.models.dive import Dive
from src.app.models.dive_site import DiveSite
from src.app.models.trip import Trip
from src.app.models.user import User
from tests.conftest import sync_engine
from tests.helpers.generators import create_user


def _db_available() -> bool:
    try:
        with sync_engine.connect():
            return True
    except OperationalError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables() -> None:
    """Create any missing tables (idempotent), as in `test_move_dives_on_delete.py`."""
    if _db_available():
        Base.metadata.create_all(sync_engine)


@pytest_asyncio.fixture
async def async_db() -> AsyncGenerator[AsyncSession]:
    """An `AsyncSession` on its own engine - the loaders under test are async, while the
    `db` fixture used to seed rows is the sync one the rest of the suite shares."""
    engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def diver(db: Session) -> User:
    return create_user(db)


@pytest.fixture
def other_diver(db: Session) -> User:
    return create_user(db)


def _site(db: Session, user: User, *, is_deleted: bool = False) -> DiveSite:
    row = DiveSite(
        user_id=user.id,
        name=f"Pescador {uuid7().hex[-8:]}",
        location="Moalboal",
        notes="",
        is_deleted=is_deleted,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _trip(db: Session, user: User, *, is_deleted: bool = False) -> Trip:
    row = Trip(
        user_id=user.id,
        name=f"Visayas {uuid7().hex[-8:]}",
        start_date=date(2026, 6, 1),
        notes="",
        is_deleted=is_deleted,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _dive(db: Session, user: User, *, trip: Trip | None = None) -> Dive:
    row = Dive(
        user_id=user.id,
        trip_id=trip.id if trip is not None else None,
        dive_number=1,
        start_time=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        duration=1800,
        notes="",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.mark.skipif(not _db_available(), reason="No database connection available")
class TestDeletedDiveSitesAreNotRendered:
    @pytest.mark.asyncio
    async def test_a_deleted_site_drops_off_the_dive(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The bug in one assertion: `GET /dive-site/{uuid}` 404s for this site, so the
        dive page must not go on showing it."""
        live, deleted = _site(db, diver), _site(db, diver, is_deleted=True)
        dive = _dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[live.id, deleted.id])

        sites = await get_dive_sites_for_dive(async_db, dive_id=dive.id)

        assert [site.uuid for site in sites] == [live.uuid]

    @pytest.mark.asyncio
    async def test_a_dive_whose_only_site_is_deleted_reads_back_empty(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        deleted = _site(db, diver, is_deleted=True)
        dive = _dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[deleted.id])

        assert await get_dive_sites_for_dive(async_db, dive_id=dive.id) == []

    @pytest.mark.asyncio
    async def test_the_next_site_inherits_the_primary_slot(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Position 0 is the primary site every single-site surface shows. Deleting the
        primary promotes the one behind it rather than leaving the dive headed by a site
        that no longer exists - `position` is a sort key, not an identity."""
        deleted, second = _site(db, diver, is_deleted=True), _site(db, diver)
        dive = _dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[deleted.id, second.id])

        sites = await get_dive_sites_for_dive(async_db, dive_id=dive.id)

        assert [site.uuid for site in sites] == [second.uuid]

    @pytest.mark.asyncio
    async def test_the_batched_loader_hides_them_too(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """`GET /dives` enriches its rows through the batched loader, so a filter on the
        single-dive one alone would leave the list page still showing the deleted site."""
        live, deleted = _site(db, diver), _site(db, diver, is_deleted=True)
        with_live, with_deleted = _dive(db, diver), _dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=with_live.id, dive_site_ids=[live.id])
        await replace_dive_sites_for_dive(async_db, dive_id=with_deleted.id, dive_site_ids=[deleted.id])

        by_dive = await get_dive_sites_for_dives(async_db, dive_ids=[with_live.id, with_deleted.id])

        assert [site.uuid for site in by_dive[with_live.id]] == [live.uuid]
        # Present and empty rather than absent - the pre-seeded lists are what keep a dive
        # whose every site is gone from dropping out of the mapping its caller indexes.
        assert by_dive[with_deleted.id] == []


@pytest.mark.skipif(not _db_available(), reason="No database connection available")
class TestDeletedTripsAreNotRendered:
    @pytest.mark.asyncio
    async def test_a_deleted_trip_stops_resolving(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """A dive keeps its `trip_id` when the trip is deleted, so the miss here is what
        turns into the `trip_uuid: null` the dive reads answer with."""
        live, deleted = _trip(db, diver), _trip(db, diver, is_deleted=True)

        by_id = await get_trip_uuids_by_ids(async_db, trip_ids=[live.id, deleted.id], user_id=diver.id)

        assert by_id == {live.id: live.uuid}

    @pytest.mark.asyncio
    async def test_another_divers_trip_never_resolves(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """Not reachable through today's callers, which pass ids read off the caller's own
        dives - this pins the scope so a future caller sourcing ids elsewhere cannot leak
        another logbook's uuid."""
        theirs = _trip(db, other_diver)

        assert await get_trip_uuids_by_ids(async_db, trip_ids=[theirs.id], user_id=diver.id) == {}

    @pytest.mark.asyncio
    async def test_no_ids_is_no_query(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        assert await get_trip_uuids_by_ids(async_db, trip_ids=[], user_id=diver.id) == {}
