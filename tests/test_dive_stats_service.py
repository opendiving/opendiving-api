"""Unit tests for the dive-stats recalculation service."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.app.models.user_dive_stats import UserDiveStats
from src.app.services.dive_stats import recalculate_dive_stats


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
