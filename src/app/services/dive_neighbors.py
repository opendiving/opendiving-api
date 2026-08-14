"""The two dives either side of one dive in its owner's log.

Its own module rather than a third role for `dive_numbering.py`: that one owns
`dive_number`, which is a label nothing orders by, while this is pure chronology. They
agree on how chronology is spelled, though - `(start_time, id)`, the same composite order
`_CHRONOLOGICAL` uses there, and for the same reason (see `_POSITION` below).

Deliberately not derived from `GET /dives`: a client showing prev/next on a dive page
would otherwise have to be holding the list page that dive sits on, which it isn't after
a deep link or a reload, and paging around a page boundary to find one neighbour is
several round trips for two rows.
"""

from datetime import datetime

from sqlalchemy import Select, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_start_time
from ..models.dive import Dive
from ..schemas.dive import DiveNeighbor, DiveNeighbors

# A dive's place in the log, as a value that can be compared whole. `start_time` alone
# isn't one: two dives can share it (a computer that records to the minute, a repetitive
# dive entered twice by hand), and with only that to go on, "the greatest start_time
# strictly earlier than mine" and "the smallest strictly later" would both skip past a
# tied dive - while the `<=`/`>=` that would catch it returns *this* dive as its own
# neighbour, which a client walking the chain follows in a circle. Ordering by `id` within
# a tie makes both queries answer about one total order, so every dive has exactly one
# predecessor and one successor and neither is itself.
_POSITION = tuple_(Dive.start_time, Dive.id)


def _neighbor_query(user_id: int, start_time: datetime, dive_id: int, *, later: bool) -> Select:
    """The dive immediately after (`later`) or before this one, as a one-row query.

    Postgres derives a `start_time` bound from the row comparison on its own, so this is
    an index scan over `ix_dive_user_id_start_time` that stops a row or two past the pivot
    rather than a scan of the log - on a real 506-dive log both directions read 2 rows.
    That's what makes two queries per request the cheap option, rather than one pass that
    fetches the log and picks the neighbours out of it in Python.
    """
    # Bound with the columns' own types rather than inferred from the Python values: an
    # aware `datetime` on its own binds as a plain `TIMESTAMP`, and comparing that against
    # a `timestamptz` column is a cast waiting to be got wrong.
    pivot = tuple_(literal(start_time, Dive.start_time.type), literal(dive_id, Dive.id.type))
    ordering = (Dive.start_time.asc(), Dive.id.asc()) if later else (Dive.start_time.desc(), Dive.id.desc())
    return (
        select(Dive.uuid, Dive.dive_number, Dive.start_time, Dive.utc_offset_minutes)
        .where(
            Dive.user_id == user_id,
            Dive.is_deleted.is_(False),
            _POSITION > pivot if later else _POSITION < pivot,
        )
        .order_by(*ordering)
        .limit(1)
    )


async def _adjacent_dive(
    db: AsyncSession, user_id: int, start_time: datetime, dive_id: int, *, later: bool
) -> DiveNeighbor | None:
    row = (await db.execute(_neighbor_query(user_id, start_time, dive_id, later=later))).first()
    if row is None:
        return None

    uuid, dive_number, neighbor_start_time, offset_minutes = row
    return DiveNeighbor(
        uuid=uuid,
        dive_number=dive_number,
        # Re-attached to the neighbour's *own* offset, exactly as `DiveRead` does it, so a
        # prev/next label reads in the timezone that dive was logged in rather than in
        # this one's.
        start_time=combine_start_time(neighbor_start_time, offset_minutes),
    )


async def find_dive_neighbors(db: AsyncSession, *, user_id: int, start_time: datetime, dive_id: int) -> DiveNeighbors:
    """The dives chronologically either side of the one at `(start_time, dive_id)`.

    `start_time` is the stored UTC instant and `dive_id`/`user_id` the internal integer
    ids: the caller has the dive row in hand by the time it gets here, and filtering by
    `user_id` is what keeps a neighbour from ever being someone else's dive.

    Either end is `None` at the ends of the log, and both are for a log of one.
    """
    return DiveNeighbors(
        previous=await _adjacent_dive(db, user_id, start_time, dive_id, later=False),
        next=await _adjacent_dive(db, user_id, start_time, dive_id, later=True),
    )
