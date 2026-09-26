"""Unit tests for the year in review's figures (`services/year_in_review.py`).

The pure half only - which year a run reviews, which year a dive belongs to, and what the
figures come to. The queries and the job are in `test_worker.py`, against Postgres.
"""

from datetime import UTC, date, datetime, timedelta, timezone

from uuid6 import uuid7

from src.app.services.year_in_review import (
    ReviewedDive,
    local_day,
    reviewed_year,
    summarize_year,
    year_window,
)


def _dive(dive_id: int, *, max_depth: float | None = 20.0, duration: int = 2400) -> ReviewedDive:
    return ReviewedDive(id=dive_id, uuid=uuid7(), day=date(2025, 3, dive_id), max_depth=max_depth, duration=duration)


class TestReviewedYear:
    def test_january_reviews_the_year_before(self) -> None:
        assert reviewed_year(date(2027, 1, 1)) == 2026
        assert reviewed_year(date(2027, 1, 31)) == 2026

    def test_no_other_month_reviews_anything(self) -> None:
        """An old logbook imported in July must not produce a "your year" email in July."""
        assert reviewed_year(date(2027, 2, 1)) is None
        assert reviewed_year(date(2026, 12, 31)) is None
        assert reviewed_year(date(2026, 7, 15)) is None


class TestTheDivesOwnLocalDay:
    def test_a_new_years_dive_in_bangkok_belongs_to_the_new_year(self) -> None:
        """Stored as 31 December UTC, dived on 1 January."""
        local = datetime(2027, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=7)))
        stored = local.astimezone(UTC)
        assert stored.year == 2026
        assert local_day(stored, 7 * 60) == date(2027, 1, 1)

    def test_an_unknown_offset_keeps_the_recorded_wall_clock(self) -> None:
        assert local_day(datetime(2026, 12, 31, 23, 50, tzinfo=UTC), None) == date(2026, 12, 31)

    def test_the_window_reaches_every_stored_instant_of_a_local_year(self) -> None:
        start, end = year_window(2026)
        # The first minute of 2026 at +14:00 and the last at -12:00, the widest offsets in use.
        first = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=14))).astimezone(UTC)
        last = datetime(2026, 12, 31, 23, 59, tzinfo=timezone(timedelta(hours=-12))).astimezone(UTC)
        assert start <= first < end
        assert start <= last < end


class TestSummarizeYear:
    def test_counts_and_time_underwater(self) -> None:
        review = summarize_year(2025, [_dive(1, duration=1800), _dive(2, duration=3000)], [], [])

        assert review.dives == 2
        assert review.seconds_underwater == 4800

    def test_the_first_dive_to_reach_the_deepest_depth_is_the_one_named(self) -> None:
        first, second = _dive(1, max_depth=30.5), _dive(2, max_depth=30.5)

        review = summarize_year(2025, [first, _dive(3, max_depth=12.0), second], [], [])

        assert review.deepest is not None and review.deepest.uuid == first.uuid

    def test_no_depth_recorded_all_year_names_no_deepest_dive(self) -> None:
        review = summarize_year(2025, [_dive(1, max_depth=None)], [], [])

        assert review.deepest is None
        assert review.longest is not None

    def test_the_longest_dive_and_its_first_site(self) -> None:
        short, long = _dive(1, duration=1800), _dive(2, duration=4200)
        sites = [(2, 10, "Blue Hole"), (2, 11, "The Arch"), (1, 12, "Lighthouse")]

        review = summarize_year(2025, [short, long], sites, [])

        assert review.longest is not None
        assert review.longest.uuid == long.uuid
        assert review.longest.site_name == "Blue Hole"

    def test_distinct_sites(self) -> None:
        sites = [(1, 10, "Blue Hole"), (2, 10, "Blue Hole"), (2, 11, "The Arch")]

        assert summarize_year(2025, [_dive(1), _dive(2)], sites, []).dive_sites == 2

    def test_a_species_is_a_first_sighting_only_when_no_earlier_year_holds_it(self) -> None:
        sightings = [
            (1, 2025),  # turtle, first seen this year
            (1, 2025),
            (2, 2019),  # moray, seen before
            (2, 2025),
            (3, 2025),  # nudibranch, first seen this year
            (4, 2019),  # a species not seen this year counts for nothing
        ]

        review = summarize_year(2025, [_dive(1)], [], sightings)

        assert review.species == 3
        assert review.first_species == 2
