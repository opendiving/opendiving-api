"""Integration tests for the dive-numbering service (`services/dive_numbering.py`).

Against a live Postgres, like `test_dive_check_constraints.py`, and for a sharper reason
than convenience: almost everything this service does is SQL the database evaluates -
a `LAG` window, a `count(*) FILTER`, an `UPDATE ... FROM` over a CTE that renumbers every
row in one statement. Mocking the session would assert that we built the query we built.

Automatically skipped when no database is reachable (e.g. running `pytest` outside the
project's docker compose setup).
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.models.dive import Dive
from src.app.models.user import User
from src.app.services.dive_numbering import renumber_dives, suggest_dive_number, summarize_numbering
from tests.conftest import db_available
from tests.helpers.generators import create_dive_log, log_day

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


async def _numbers_by_day(async_db: AsyncSession, user: User) -> list[int]:
    """The log's numbers in chronological order - what a renumber is judged on."""
    result = await async_db.execute(
        select(Dive.dive_number)
        .where(Dive.user_id == user.id, Dive.is_deleted.is_(False))
        .order_by(Dive.start_time, Dive.id)
    )
    return list(result.scalars())


class TestSuggestDiveNumber:
    @pytest.mark.asyncio
    async def test_first_dive_of_an_empty_log_is_number_one(self, async_db: AsyncSession, diver: User) -> None:
        suggestion = await suggest_dive_number(async_db, user_id=diver.id, start_time=log_day(0))

        assert suggestion.dive_number == 1
        assert suggestion.is_taken is False

    @pytest.mark.asyncio
    async def test_dive_logged_after_the_whole_log_continues_it(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        create_dive_log(db, diver, (1, 0), (2, 1), (3, 2))

        suggestion = await suggest_dive_number(async_db, user_id=diver.id, start_time=log_day(3))

        assert suggestion.dive_number == 4

    @pytest.mark.asyncio
    async def test_backfilled_dive_is_numbered_where_it_belongs_not_at_the_end(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The bug this endpoint exists to fix: "newest dive + 1" would suggest #213 for a
        dive that sits between #11 and #12."""
        create_dive_log(db, diver, (11, 0), (12, 10), (13, 11), (212, 100))

        suggestion = await suggest_dive_number(async_db, user_id=diver.id, start_time=log_day(5))

        assert suggestion.dive_number == 12

    @pytest.mark.asyncio
    async def test_dive_older_than_the_whole_log_is_number_one(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        create_dive_log(db, diver, (1, 5), (2, 6))

        suggestion = await suggest_dive_number(async_db, user_id=diver.id, start_time=log_day(0))

        assert suggestion.dive_number == 1

    @pytest.mark.asyncio
    async def test_reports_a_collision_without_refusing_to_suggest(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Back-filling produces collisions by construction - the diver is told, and the
        suggestion stands. `renumber_dives` is what reconciles the log afterwards."""
        create_dive_log(db, diver, (1, 0), (2, 10))

        suggestion = await suggest_dive_number(async_db, user_id=diver.id, start_time=log_day(5))

        assert suggestion.dive_number == 2
        assert suggestion.is_taken is True

    @pytest.mark.asyncio
    async def test_continues_a_log_that_starts_above_one(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """A diver whose first 46 dives are on paper starts this log at #47; the next dive
        is #51, not #5."""
        create_dive_log(db, diver, (47, 0), (48, 1), (49, 2), (50, 3))

        suggestion = await suggest_dive_number(async_db, user_id=diver.id, start_time=log_day(4))

        assert suggestion.dive_number == 51

    @pytest.mark.asyncio
    async def test_matches_the_dives_own_offset_not_the_stored_utc_instant(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """09:00+02:00 is 07:00 UTC, so this dive precedes the 08:00 UTC one - a
        comparison against the wall-clock hour would put it after."""
        create_dive_log(db, diver, (1, 0))
        db.add(Dive(user_id=diver.id, dive_number=2, start_time=log_day(1).replace(hour=8), duration=1800, notes=""))
        db.commit()

        suggestion = await suggest_dive_number(
            async_db,
            user_id=diver.id,
            start_time=log_day(1).replace(hour=9, tzinfo=timezone(timedelta(hours=2))),
        )

        assert suggestion.dive_number == 2

    @pytest.mark.asyncio
    async def test_ignores_deleted_dives(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        create_dive_log(db, diver, (1, 0), (2, 1))
        create_dive_log(db, diver, (3, 2), is_deleted=True, deleted_at=datetime.now(UTC))

        suggestion = await suggest_dive_number(async_db, user_id=diver.id, start_time=log_day(3))

        assert suggestion.dive_number == 3

    @pytest.mark.asyncio
    async def test_ignores_another_divers_log(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        create_dive_log(db, other_diver, (400, 0), (401, 1))
        create_dive_log(db, diver, (1, 0))

        suggestion = await suggest_dive_number(async_db, user_id=diver.id, start_time=log_day(2))

        assert suggestion.dive_number == 2
        assert suggestion.is_taken is False


class TestSummarizeNumbering:
    @pytest.mark.asyncio
    async def test_empty_log_has_nothing_to_say(self, async_db: AsyncSession, diver: User) -> None:
        summary = await summarize_numbering(async_db, user_id=diver.id)

        assert summary.total_dives == 0
        assert summary.lowest is None
        assert summary.highest is None
        assert summary.is_sequential is False

    @pytest.mark.asyncio
    async def test_clean_log_is_sequential(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        create_dive_log(db, diver, (1, 0), (2, 1), (3, 2))

        summary = await summarize_numbering(async_db, user_id=diver.id)

        assert (summary.total_dives, summary.lowest, summary.highest) == (3, 1, 3)
        assert summary.missing_count == 0
        assert summary.duplicate_count == 0
        assert summary.out_of_date_order_count == 0
        assert summary.is_sequential is True

    @pytest.mark.asyncio
    async def test_log_starting_above_one_is_still_sequential(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """#47-#49 with nothing missing is a tidy log, not a broken one - the first 46
        dives are simply in a paper logbook."""
        create_dive_log(db, diver, (47, 0), (48, 1), (49, 2))

        summary = await summarize_numbering(async_db, user_id=diver.id)

        assert (summary.lowest, summary.highest) == (47, 49)
        assert summary.is_sequential is True

    @pytest.mark.asyncio
    async def test_counts_gaps(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        create_dive_log(db, diver, (1, 0), (5, 1), (6, 2))

        summary = await summarize_numbering(async_db, user_id=diver.id)

        # #2, #3 and #4 are unused between #1 and #6.
        assert summary.missing_count == 3
        assert summary.is_sequential is False

    @pytest.mark.asyncio
    async def test_counts_duplicates(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        create_dive_log(db, diver, (1, 0), (2, 1), (2, 2), (2, 3))

        summary = await summarize_numbering(async_db, user_id=diver.id)

        # Two dives beyond the one that legitimately holds #2.
        assert summary.duplicate_count == 2
        assert summary.is_sequential is False

    @pytest.mark.asyncio
    async def test_counts_dives_numbered_out_of_date_order(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Numbers that go 1, 9, 4, 5: only #4 is lower than the dive before it."""
        create_dive_log(db, diver, (1, 0), (9, 1), (4, 2), (5, 3))

        summary = await summarize_numbering(async_db, user_id=diver.id)

        assert summary.out_of_date_order_count == 1

    @pytest.mark.asyncio
    async def test_repeated_number_counts_as_a_duplicate_not_as_out_of_order(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Reporting it under both headings would tell a diver two things are wrong with
        their log when one is."""
        create_dive_log(db, diver, (1, 0), (2, 1), (2, 2))

        summary = await summarize_numbering(async_db, user_id=diver.id)

        assert summary.duplicate_count == 1
        assert summary.out_of_date_order_count == 0

    @pytest.mark.asyncio
    async def test_ignores_deleted_dives_and_other_divers(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        create_dive_log(db, diver, (1, 0), (2, 1))
        create_dive_log(db, diver, (99, 2), is_deleted=True, deleted_at=datetime.now(UTC))
        create_dive_log(db, other_diver, (500, 0))

        summary = await summarize_numbering(async_db, user_id=diver.id)

        assert (summary.total_dives, summary.highest) == (2, 2)
        assert summary.is_sequential is True


class TestRenumberDives:
    @pytest.mark.asyncio
    async def test_dry_run_reports_the_changes_and_writes_nothing(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        create_dive_log(db, diver, (11, 0), (12, 1), (13, 2))

        result = await renumber_dives(async_db, user_id=diver.id, dry_run=True)

        assert result.dry_run is True
        assert result.dives_in_scope == 3
        assert [(c.dive_number, c.new_dive_number) for c in result.changes] == [(11, 1), (12, 2), (13, 3)]
        assert await _numbers_by_day(async_db, diver) == [11, 12, 13]

    @pytest.mark.asyncio
    async def test_renumbers_the_whole_log_from_one_in_date_order(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        # Deliberately scrambled: gaps, a duplicate, and numbers that don't follow dates.
        create_dive_log(db, diver, (7, 0), (3, 1), (3, 2), (99, 3))

        result = await renumber_dives(async_db, user_id=diver.id)

        assert result.dry_run is False
        assert await _numbers_by_day(async_db, diver) == [1, 2, 3, 4]
        # The third dive already held #3, so it isn't listed - the preview shows what
        # moves, not every dive in scope, which is `dives_in_scope`.
        assert [(c.dive_number, c.new_dive_number) for c in result.changes] == [(7, 1), (3, 2), (99, 4)]
        assert result.dives_in_scope == 4

    @pytest.mark.asyncio
    async def test_starts_the_count_where_asked(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The diver whose first 46 dives are on paper renumbers to #47 onwards."""
        create_dive_log(db, diver, (1, 0), (2, 1), (3, 2))

        await renumber_dives(async_db, user_id=diver.id, start_at=47)

        assert await _numbers_by_day(async_db, diver) == [47, 48, 49]

    @pytest.mark.asyncio
    async def test_scope_leaves_earlier_dives_untouched(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """Tidy the recent tail without rewriting the part that mirrors a paper logbook."""
        create_dive_log(db, diver, (100, 0), (101, 1), (9, 2), (4, 3))

        result = await renumber_dives(async_db, user_id=diver.id, start_at=102, from_start_time=log_day(2))

        assert result.dives_in_scope == 2
        assert await _numbers_by_day(async_db, diver) == [100, 101, 102, 103]

    @pytest.mark.asyncio
    async def test_scope_boundary_includes_a_dive_at_the_exact_instant(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        create_dive_log(db, diver, (1, 0), (2, 1))

        result = await renumber_dives(async_db, user_id=diver.id, start_at=50, from_start_time=log_day(1))

        assert result.dives_in_scope == 1
        assert await _numbers_by_day(async_db, diver) == [1, 50]

    @pytest.mark.asyncio
    async def test_already_clean_log_reports_no_changes(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        create_dive_log(db, diver, (1, 0), (2, 1), (3, 2))

        result = await renumber_dives(async_db, user_id=diver.id)

        assert result.changes == []
        assert result.dives_in_scope == 3

    @pytest.mark.asyncio
    async def test_shifting_a_run_down_by_one_does_not_collide(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The case a row-by-row renumber would break on: every dive takes the number the
        dive before it currently holds. One `UPDATE ... FROM` has no intermediate state to
        collide with."""
        create_dive_log(db, diver, (2, 0), (3, 1), (4, 2), (5, 3))

        await renumber_dives(async_db, user_id=diver.id)

        assert await _numbers_by_day(async_db, diver) == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_leaves_deleted_dives_and_other_divers_alone(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        create_dive_log(db, diver, (7, 0), (8, 1))
        deleted = create_dive_log(db, diver, (99, 2), is_deleted=True, deleted_at=datetime.now(UTC))[0]
        theirs = create_dive_log(db, other_diver, (500, 0))[0]

        await renumber_dives(async_db, user_id=diver.id)

        assert await _numbers_by_day(async_db, diver) == [1, 2]
        assert await async_db.scalar(select(Dive.dive_number).where(Dive.id == deleted.id)) == 99
        assert await async_db.scalar(select(Dive.dive_number).where(Dive.id == theirs.id)) == 500

    @pytest.mark.asyncio
    async def test_renumbered_log_summarizes_as_sequential(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The end state the whole feature exists to reach."""
        create_dive_log(db, diver, (7, 0), (3, 1), (3, 2), (99, 3))

        await renumber_dives(async_db, user_id=diver.id)

        summary = await summarize_numbering(async_db, user_id=diver.id)
        assert summary.is_sequential is True
        assert (summary.lowest, summary.highest) == (1, 4)
        assert summary.out_of_date_order_count == 0
