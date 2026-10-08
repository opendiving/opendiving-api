"""The dive-stats service: the stored recalculation, mocked, and the figures derived on read,
against Postgres."""

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.v1 import users as users_module
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_site import DiveSite
from src.app.models.user import User
from src.app.models.user_dive_stats import UserDiveStats
from src.app.schemas.user_dive_stats import UserDiveStatsReadInternal
from src.app.services.dive_stats import SitesAndDiveDays, recalculate_dive_stats, sites_and_dive_days
from tests.conftest import db_available
from tests.helpers.generators import create_dive_site


def _make_db_mock(aggregate_row: tuple, existing_stats: UserDiveStats | None, species_seen: int = 0):
    """Build a mock AsyncSession whose `execute` calls return the given aggregate
    row (count, max_depth, total_time) first, then the existing stats row.

    `species_seen` comes back from `db.scalar` rather than `db.execute`: it is a second,
    separate query over `dive_species` (see the service's docstring for why it is not a
    fourth column on the aggregate above).
    """
    aggregate_result = MagicMock()
    aggregate_result.one.return_value = aggregate_row

    stats_result = MagicMock()
    stats_result.scalar_one_or_none.return_value = existing_stats

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[aggregate_result, stats_result])
    db.scalar = AsyncMock(return_value=species_seen)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    return db


class TestRecalculateDiveStats:
    @pytest.mark.asyncio
    async def test_creates_new_stats_row_when_none_exists(self):
        db = _make_db_mock(aggregate_row=(3, 40.0, 5400), existing_stats=None)

        stats = await recalculate_dive_stats(db, user_id=1)

        db.add.assert_called_once()
        added_stats = db.add.call_args[0][0]
        assert added_stats.user_id == 1
        assert added_stats.total_dives == 3
        assert added_stats.max_depth == 40.0
        assert added_stats.total_time == 5400
        assert added_stats.species_seen == 0
        assert stats is added_stats
        db.commit.assert_awaited_once()
        db.refresh.assert_awaited_once_with(added_stats)

    @pytest.mark.asyncio
    async def test_updates_existing_stats_row(self):
        existing_stats = UserDiveStats(user_id=1, total_dives=1, max_depth=10.0, total_time=1000, species_seen=5)
        db = _make_db_mock(aggregate_row=(4, 55.5, 7200), existing_stats=existing_stats, species_seen=2)

        stats = await recalculate_dive_stats(db, user_id=1)

        db.add.assert_not_called()
        assert stats is existing_stats
        assert stats.total_dives == 4
        assert stats.max_depth == 55.5
        assert stats.total_time == 7200
        # **This branch, not just the insert.** `species_seen` used to be preserved here
        # because nothing derived it; now it is derived, and this is the branch nearly every
        # real write takes - the insert above only ever runs once per account. A change that
        # only fixed the insert would leave every existing diver at whatever they had, which
        # for all of them is 0, for good. The stale 5 going to 2 is the whole assertion.
        assert stats.species_seen == 2
        db.commit.assert_awaited_once()
        db.refresh.assert_awaited_once_with(existing_stats)

    @pytest.mark.asyncio
    async def test_a_diver_who_has_seen_nothing_reads_zero_rather_than_null(self):
        """`COUNT(DISTINCT ...)` over no rows is 0, but `db.scalar` returning `None` at all
        is worth guarding: the column is `NOT NULL`, so a null would be an IntegrityError on
        every dive write rather than a wrong number."""
        db = _make_db_mock(aggregate_row=(1, 10.0, 100), existing_stats=None, species_seen=None)

        stats = await recalculate_dive_stats(db, user_id=1)

        assert stats.species_seen == 0

    @pytest.mark.asyncio
    async def test_handles_no_dives(self):
        db = _make_db_mock(aggregate_row=(0, 0, 0), existing_stats=None)

        stats = await recalculate_dive_stats(db, user_id=1)

        assert stats.total_dives == 0
        assert stats.max_depth == 0.0
        assert stats.total_time == 0

    @pytest.mark.asyncio
    async def test_skips_commit_when_commit_is_false(self):
        db = _make_db_mock(aggregate_row=(1, 10.0, 100), existing_stats=None)

        await recalculate_dive_stats(db, user_id=1, commit=False)

        db.commit.assert_not_awaited()
        db.refresh.assert_not_awaited()


DERIVED = SitesAndDiveDays(dive_site_count=3, first_dive_on=date(2014, 3, 8), last_dive_on=date(2026, 10, 5))


class TestTheRoute:
    """Both branches carry the derived figures: the zeroed one is every diver whose stats row
    a dive write has not created yet."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row",
        [
            None,
            UserDiveStatsReadInternal(
                user_id=1, total_dives=4, max_depth=30.0, total_time=7200, species_seen=2, created_at=datetime.now(UTC)
            ),
        ],
    )
    async def test_reads_the_derived_figures_beside_the_stored_ones(
        self, monkeypatch: pytest.MonkeyPatch, row: UserDiveStatsReadInternal | None
    ) -> None:
        monkeypatch.setattr(users_module.crud_user_dive_stats, "get", AsyncMock(return_value=row))
        monkeypatch.setattr(users_module, "sites_and_dive_days", AsyncMock(return_value=DERIVED))

        stats = await users_module.read_dive_stats(MagicMock(), current_user={"id": 1, "uuid": uuid4()}, db=MagicMock())

        assert (stats.dive_site_count, stats.first_dive_on, stats.last_dive_on) == tuple(DERIVED)
        assert stats.total_dives == (0 if row is None else 4)


# ------------------------------------------------------------------ against Postgres

PLUS_7 = timezone(timedelta(hours=7))
MINUS_10 = timezone(timedelta(hours=-10))


def _dive(db: Session, user: User, start: datetime, *, sites: tuple[DiveSite, ...] = (), **columns: Any) -> Dive:
    """A dive at `start`, its offset the one `start` carries, naming `sites` in order."""
    offset = start.utcoffset()
    dive = Dive(
        user_id=user.id,
        dive_number=1,
        start_time=start,
        utc_offset_minutes=0 if offset is None else int(offset.total_seconds() // 60),
        duration=1800,
        notes="",
        **columns,
    )
    db.add(dive)
    db.commit()
    db.add_all(DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=n) for n, site in enumerate(sites))
    db.commit()
    return dive


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestSitesAndDiveDays:
    @pytest.mark.asyncio
    async def test_a_logbook_with_no_dives_has_no_sites_and_no_days(self, async_db: AsyncSession, diver: User) -> None:
        assert await sites_and_dive_days(async_db, user_id=diver.id) == (0, None, None)

    @pytest.mark.asyncio
    async def test_counts_each_site_a_live_dive_names_once_at_any_position(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        reef, wall, wreck, unvisited, gone = (create_dive_site(db, diver) for _ in range(5))
        at = datetime(2026, 6, 1, 9, tzinfo=UTC)
        _dive(db, diver, at, sites=(reef, wall))
        _dive(db, diver, at, sites=(wall, wreck))
        _dive(db, diver, at)
        _dive(db, diver, at, sites=(gone,), is_deleted=True)
        _dive(db, other_diver, at, sites=(unvisited,))

        assert (await sites_and_dive_days(async_db, user_id=diver.id)).dive_site_count == 3

    @pytest.mark.asyncio
    async def test_the_days_are_the_end_dives_own_local_ones(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        first = _dive(db, diver, datetime(2014, 3, 8, 0, 30, tzinfo=PLUS_7))
        last = _dive(db, diver, datetime(2026, 10, 5, 23, 30, tzinfo=MINUS_10))
        _dive(db, diver, datetime(2020, 1, 1, 9, tzinfo=UTC))
        _dive(db, diver, datetime(2010, 1, 1, 9, tzinfo=UTC), is_deleted=True)
        _dive(db, diver, datetime(2027, 1, 1, 9, tzinfo=UTC), is_deleted=True)
        _dive(db, other_diver, datetime(2000, 1, 1, 9, tzinfo=UTC))
        assert (first.start_time.astimezone(UTC).date(), last.start_time.astimezone(UTC).date()) == (
            date(2014, 3, 7),
            date(2026, 10, 6),
        )

        derived = await sites_and_dive_days(async_db, user_id=diver.id)

        assert (derived.first_dive_on, derived.last_dive_on) == (date(2014, 3, 8), date(2026, 10, 5))

    @pytest.mark.asyncio
    async def test_a_dive_with_an_unknown_offset_or_only_a_date_keeps_its_recorded_day(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        wall_clock = _dive(db, diver, datetime(2015, 5, 1, 23, 30, tzinfo=UTC))
        bare_date = _dive(db, diver, datetime(2025, 5, 1, tzinfo=UTC))
        for dive, date_only in ((wall_clock, False), (bare_date, True)):
            db.execute(
                update(Dive).where(Dive.id == dive.id).values(utc_offset_minutes=None, start_date_only=date_only)
            )
        db.commit()

        derived = await sites_and_dive_days(async_db, user_id=diver.id)

        assert (derived.first_dive_on, derived.last_dive_on) == (date(2015, 5, 1), date(2025, 5, 1))
