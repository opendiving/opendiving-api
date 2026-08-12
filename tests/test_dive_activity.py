"""Unit tests for dives-per-day bucketing (`services/dive_activity.py`).

Same convention as `test_dive_gas.py`: `bucket_by_day` is pure, so the whole rule - which
day a dive lands in, and what the series looks like around the edges - is covered here
without a database.
"""

from datetime import UTC, datetime, timedelta, timezone

from src.app.services.dive_activity import bucket_by_day

BANGKOK = 7 * 60
LONDON = 0
# The mirror of Bangkok, and Honolulu rather than a mainland US offset on purpose: it
# has never observed DST, so -10:00 is honest on an April date and a December one alike.
# A "New York" fixture would have to be -04:00 in one test and -05:00 in the other.
HONOLULU = -10 * 60


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


class TestBucketByDay:
    def test_counts_dives_into_their_days(self) -> None:
        """Two dives on one day is one bucket of two - a repetitive pair, which is what the
        day scope exists to show.
        """
        points = bucket_by_day(
            [
                _dive("2026-04-02T09:00:00"),
                _dive("2026-04-02T13:30:00"),
                _dive("2026-04-03T09:00:00"),
            ]
        )

        assert [(point.year, point.month, point.day, point.dives) for point in points] == [
            (2026, 4, 2, 2),
            (2026, 4, 3, 1),
        ]

    def test_leaves_out_days_with_no_diving(self) -> None:
        """The 3rd is missing rather than present with a zero - the client draws the grid."""
        points = bucket_by_day([_dive("2026-04-02T09:00:00"), _dive("2026-04-04T09:00:00")])

        assert [point.day for point in points] == [2, 4]

    def test_has_nothing_to_say_about_an_empty_logbook(self) -> None:
        assert bucket_by_day([]) == []

    def test_buckets_by_the_dives_own_local_day(self) -> None:
        """A dive just after midnight on the 1st in Bangkok is still the previous day - and
        the previous month - in UTC. It belongs to the 1st of May, which is the day the
        diver did it on and the day their dive page shows.
        """
        points = bucket_by_day([_local("2026-05-01T00:30:00", BANGKOK)])

        assert [(point.year, point.month, point.day) for point in points] == [(2026, 5, 1)]

    def test_buckets_by_the_dives_own_local_year(self) -> None:
        """The same trap one boundary up, where it is a whole year out."""
        points = bucket_by_day([_local("2026-01-01T00:30:00", BANGKOK)])

        assert [(point.year, point.month, point.day) for point in points] == [(2026, 1, 1)]

    def test_buckets_a_dive_west_of_utc_by_its_own_local_day(self) -> None:
        """The mirror of the Bangkok case, which the two above only cover from the east.

        An evening dive in Honolulu on the last day of April is already the 1st of May in
        UTC, so counting the stored instant files it a day - and a month - *late*, where
        the Bangkok trap files it early. Both directions are the same off-by-one, and a
        fixture set that only ever runs ahead of UTC can't tell a correct conversion from
        one that adds the offset where it should subtract it.
        """
        points = bucket_by_day([_local("2026-04-30T22:00:00", HONOLULU)])

        assert [(point.year, point.month, point.day) for point in points] == [(2026, 4, 30)]

    def test_buckets_a_dive_west_of_utc_by_its_own_local_year(self) -> None:
        """The westward trap one boundary up: a New Year's Eve dive in Honolulu is the
        1st of January in UTC, and belongs to the year the diver did it in.
        """
        points = bucket_by_day([_local("2025-12-31T22:00:00", HONOLULU)])

        assert [(point.year, point.month, point.day) for point in points] == [(2025, 12, 31)]

    def test_orders_days_by_the_calendar_not_by_the_instant(self) -> None:
        """These two arrive in the order the query returns them - the Bangkok dive is the
        earlier instant - but belong to the 1st of May and the 30th of April respectively.
        """
        points = bucket_by_day(
            [
                _local("2026-05-01T00:30:00", BANGKOK),
                _local("2026-04-30T20:00:00", LONDON),
            ]
        )

        assert [(point.month, point.day) for point in points] == [(4, 30), (5, 1)]

    def test_keeps_months_apart(self) -> None:
        """The same day-of-month in two months is two buckets, not one."""
        points = bucket_by_day([_dive("2026-04-02T09:00:00"), _dive("2026-06-02T09:00:00")])

        assert [(point.month, point.dives) for point in points] == [(4, 1), (6, 1)]

    def test_keeps_years_apart(self) -> None:
        """The same day in two years is two buckets, not one."""
        points = bucket_by_day([_dive("2025-04-02T09:00:00"), _dive("2026-04-02T09:00:00")])

        assert [(point.year, point.dives) for point in points] == [(2025, 1), (2026, 1)]
