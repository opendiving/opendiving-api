"""How many dives a diver logged on each calendar day.

Separate from `dive_stats.py`, which owns the four all-time counters stored on
`user_dive_stats` and recomputed on every write: those are scalars that a dashboard tile
reads back in one row, this is a series nothing stores, derived on read and cached.

Same split as `dive_gas.py`: `bucket_by_day` is pure - no session, no ORM, no clock - so
the bucketing rules are covered exhaustively without a database
(`tests/test_dive_activity.py`).
"""

from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_start_time
from ..models.dive import Dive
from ..schemas.dive import DiveActivityPoint


def bucket_by_day(dives: Sequence[tuple[datetime, int]]) -> list[DiveActivityPoint]:
    """Count `(start_time, utc_offset_minutes)` pairs into calendar days, oldest first.

    **The day is the dive's own local one**, reconstructed with `combine_start_time`
    exactly as `DiveRead` does it. A dive that began at 00:30 on the 1st of May in
    Bangkok (+07:00) is stored as 17:30 on the 30th of April UTC, and counting the stored
    instant would file it under April - and, worse, under a *different* day for a diver
    whose next trip was in a different timezone. That is the same rule as "a dive displays
    in the timezone it was logged in", extended from formatting to bucketing.

    Doing it here rather than as a `date_trunc` over `start_time + utc_offset_minutes` in
    SQL is deliberate. `core/utils/datetime_offset.py` is documented as the single place
    that conversion happens, and the failure mode of a second copy of it in another
    language is a chart that quietly disagrees with the dive pages it was built from.
    The cost is two small columns per dive on a cached endpoint - the same trade
    `gas_use_history` already makes, for the same reason.

    Days are the finest bucket the client's three windows need, and the only one that can
    serve all of them: it sums days into months and months into years itself. The series
    is still bounded by the diving rather than by the calendar - one row per day dived,
    never one per day - so it stays strictly smaller than the gas series the same
    dashboard already fetches, which carries a whole object per dive.

    Days with no diving are absent rather than zero-filled: the caller plots a fixed grid
    (a month's days, twelve months, or every year between the first and the last) and has
    to fill the gaps in it regardless, so sending empty buckets would be padding one shape
    into a different one.

    Sorted on the buckets themselves rather than inherited from the input's order, which
    is chronological by *instant* and so can hand back the 1st before the 30th: a dive
    logged at 00:30 on the 1st of May in Bangkok is an earlier instant than one at 20:00
    on the 30th of April in London, and they belong to different days.
    """
    counts = Counter((local.year, local.month, local.day) for local in (combine_start_time(*dive) for dive in dives))

    return [
        DiveActivityPoint(year=year, month=month, day=day, dives=count)
        for (year, month, day), count in sorted(counts.items())
    ]


async def dive_activity(db: AsyncSession, user_id: int) -> list[DiveActivityPoint]:
    """Every day of a user's diving that contains at least one dive, oldest first.

    Two columns and no joins: this counts dives, so nothing about any individual one is
    needed. `ix_dive_user_id_start_time` serves the filter and the sort together.
    """
    result = await db.execute(
        select(Dive.start_time, Dive.utc_offset_minutes)
        .where(Dive.user_id == user_id, Dive.is_deleted.is_(False))
        .order_by(Dive.start_time)
    )

    return bucket_by_day([(start_time, offset_minutes) for start_time, offset_minutes in result])
