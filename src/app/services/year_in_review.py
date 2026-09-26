"""A diver's calendar year in figures, for the January email.

Computed from that year's rows every time, while `user_dive_stats` stays all-time: a year is
at most a few hundred dives, which is a scan rather than a schema, and per-year columns
recomputed on every dive write would pay all year for a number read once.

The same split as `dive_activity.py`: `summarize_year` and the date helpers are pure, and
covered without a database (`tests/test_year_in_review.py`); `year_in_review` only fetches.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from operator import attrgetter
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_start_time
from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_site import DiveSite
from ..models.dive_species import DiveSpecies

# How many reviews one run sends. The flagship's relay allows 100 emails a day, and every
# other email of the day - the gear digest and the renewal reminders at the same hour, and
# each sign-in and invitation - has to fit beside these. A constant rather than a setting:
# only one instance's mail plan sets it, and a setting owes an installer a row of docs.
YEAR_IN_REVIEW_BATCH_SIZE = 50


def reviewed_year(today: date) -> int | None:
    """The year a run on `today` reviews - the previous one, and only while it is January.

    Any other month is `None`, so a diver who imports an old logbook in July is not sent a
    "your year" email in July. One still unsent when January ends gets none for that year.
    """
    return today.year - 1 if today.month == 1 else None


def local_day(start_time: datetime, utc_offset_minutes: int | None) -> date:
    """The calendar day a dive belongs to: its own local one, as `bucket_by_day` counts it
    for the dashboard's activity chart. A dive at 00:30 on 1 January in Bangkok is stored as
    31 December UTC, and is a dive of the new year."""
    return combine_start_time(start_time, utc_offset_minutes).date()


def year_window(year: int) -> tuple[datetime, datetime]:
    """Stored instants bracketing every dive whose local day falls in `year`.

    A day wider than the year at each end, because a stored offset is under a day either
    way. A pre-filter for the query only; `local_day` decides.
    """
    return datetime(year, 1, 1, tzinfo=UTC) - timedelta(days=1), datetime(year + 1, 1, 1, tzinfo=UTC) + timedelta(
        days=1
    )


@dataclass(frozen=True)
class ReviewedDive:
    id: int
    uuid: UUID
    day: date
    max_depth: float | None
    duration: int
    site_name: str | None = None


@dataclass(frozen=True)
class YearInReview:
    year: int
    dives: int
    seconds_underwater: int
    deepest: ReviewedDive | None
    longest: ReviewedDive | None
    dive_sites: int
    species: int
    first_species: int


def summarize_year(
    year: int,
    dives: Sequence[ReviewedDive],
    sites: Sequence[tuple[int, int, str]],
    sightings: Sequence[tuple[int, int]],
) -> YearInReview:
    """The figures, from the year's dives oldest first, their `(dive_id, dive_site_id,
    site_name)` rows in visiting order, and every `(species_id, local_year)` sighting up to
    the end of `year`.

    Oldest first is what settles a tie: the first dive to reach the deepest depth is the one
    the email names. A dive's site is its first one, as a dive page names it.

    A species is a first sighting when no earlier year of the log holds it - which is why the
    sightings reach back past `year` and this does not look at dives alone.
    """
    first_site: dict[int, str] = {}
    for dive_id, _, name in sites:
        first_site.setdefault(dive_id, name)

    def named(dive: ReviewedDive | None) -> ReviewedDive | None:
        return None if dive is None else replace(dive, site_name=first_site.get(dive.id))

    this_year = {species_id for species_id, seen_in in sightings if seen_in == year}
    before = {species_id for species_id, seen_in in sightings if seen_in < year}

    return YearInReview(
        year=year,
        dives=len(dives),
        seconds_underwater=sum(dive.duration for dive in dives),
        deepest=named(
            max((dive for dive in dives if dive.max_depth is not None), key=attrgetter("max_depth"), default=None)
        ),
        longest=named(max((dive for dive in dives if dive.duration > 0), key=attrgetter("duration"), default=None)),
        dive_sites=len({site_id for _, site_id, _ in sites}),
        species=len(this_year),
        first_species=len(this_year - before),
    )


async def year_in_review(db: AsyncSession, user_id: int, year: int) -> YearInReview | None:
    """`None` when none of the diver's live dives falls in `year` by its local day."""
    window_start, window_end = year_window(year)
    rows = (
        await db.execute(
            select(Dive.id, Dive.uuid, Dive.start_time, Dive.utc_offset_minutes, Dive.max_depth, Dive.duration)
            .where(
                Dive.user_id == user_id,
                Dive.is_deleted.is_(False),
                Dive.start_time >= window_start,
                Dive.start_time < window_end,
            )
            .order_by(Dive.start_time, Dive.id)
        )
    ).all()
    dives = [
        ReviewedDive(
            id=row.id,
            uuid=row.uuid,
            day=day,
            max_depth=row.max_depth,
            duration=row.duration,
        )
        for row in rows
        if (day := local_day(row.start_time, row.utc_offset_minutes)).year == year
    ]
    if not dives:
        return None

    sites = (
        await db.execute(
            select(DiveDiveSite.dive_id, DiveDiveSite.dive_site_id, DiveSite.name)
            .join(DiveSite, DiveSite.id == DiveDiveSite.dive_site_id)
            .where(DiveDiveSite.dive_id.in_([dive.id for dive in dives]))
            .order_by(DiveDiveSite.dive_id, DiveDiveSite.position)
        )
    ).all()
    sightings = (
        await db.execute(
            select(DiveSpecies.species_id, Dive.start_time, Dive.utc_offset_minutes)
            .join(Dive, Dive.id == DiveSpecies.dive_id)
            .where(Dive.user_id == user_id, Dive.is_deleted.is_(False), Dive.start_time < window_end)
        )
    ).all()

    return summarize_year(
        year,
        dives,
        [(row.dive_id, row.dive_site_id, row.name) for row in sites],
        [(row.species_id, local_day(row.start_time, row.utc_offset_minutes).year) for row in sightings],
    )
