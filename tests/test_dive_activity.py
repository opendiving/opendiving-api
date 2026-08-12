"""Unit tests for dives-per-month bucketing (`services/dive_activity.py`).

Same convention as `test_dive_gas.py`: `bucket_by_month` is pure, so the whole rule -
which month a dive lands in, and what the series looks like around the edges - is covered
here without a database.
"""

from datetime import UTC, datetime, timedelta, timezone

from src.app.services.dive_activity import bucket_by_month

BANGKOK = 7 * 60
LONDON = 0


def _dive(iso: str, offset_minutes: int = LONDON) -> tuple[datetime, int]:
    """A dive as it comes back from the database: the *UTC instant* it started at, plus
    the offset it was originally logged in. Both halves matter - see the timezone tests.
    """
    return datetime.fromisoformat(iso).replace(tzinfo=UTC), offset_minutes


def _local(iso: str, offset_minutes: int) -> tuple[datetime, int]:
    """The same thing stated the other way round: a dive's own local wall-clock time,
    converted back to the instant the database would hold. This is how a diver would
    describe when they dived, which makes the timezone cases readable.
    """
    aware = datetime.fromisoformat(iso).replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
    return aware.astimezone(UTC), offset_minutes


class TestBucketByMonth:
    def test_counts_dives_into_their_months(self) -> None:
        points = bucket_by_month(
            [
                _dive("2026-04-02T09:00:00"),
                _dive("2026-04-03T09:00:00"),
                _dive("2026-06-11T09:00:00"),
            ]
        )

        assert [(point.year, point.month, point.dives) for point in points] == [(2026, 4, 2), (2026, 6, 1)]

    def test_leaves_out_months_with_no_diving(self) -> None:
        """May is missing rather than present with a zero - the client draws the grid."""
        points = bucket_by_month([_dive("2026-04-02T09:00:00"), _dive("2026-06-11T09:00:00")])

        assert [point.month for point in points] == [4, 6]

    def test_has_nothing_to_say_about_an_empty_logbook(self) -> None:
        assert bucket_by_month([]) == []

    def test_buckets_by_the_dives_own_local_month(self) -> None:
        """A dive just after midnight on the 1st in Bangkok is still the previous month in
        UTC. It belongs to May, which is the month the diver did it in and the month their
        dive page shows.
        """
        points = bucket_by_month([_local("2026-05-01T00:30:00", BANGKOK)])

        assert [(point.year, point.month) for point in points] == [(2026, 5)]

    def test_buckets_by_the_dives_own_local_year(self) -> None:
        """The same trap one boundary up, where it is a whole year out."""
        points = bucket_by_month([_local("2026-01-01T00:30:00", BANGKOK)])

        assert [(point.year, point.month) for point in points] == [(2026, 1)]

    def test_orders_months_by_the_calendar_not_by_the_instant(self) -> None:
        """These two arrive in the order the query returns them - the Bangkok dive is the
        earlier instant - but belong to April and May respectively.
        """
        points = bucket_by_month(
            [
                _local("2026-05-01T00:30:00", BANGKOK),
                _local("2026-04-30T20:00:00", LONDON),
            ]
        )

        assert [point.month for point in points] == [4, 5]

    def test_keeps_years_apart(self) -> None:
        """The same month in two years is two buckets, not one."""
        points = bucket_by_month([_dive("2025-04-02T09:00:00"), _dive("2026-04-02T09:00:00")])

        assert [(point.year, point.dives) for point in points] == [(2025, 1), (2026, 1)]
