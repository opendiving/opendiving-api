"""Helpers for datetimes that must round-trip through the API with their original UTC
offset preserved (currently just `Dive.start_time`).

The problem: a Postgres `timestamptz` column only ever stores an absolute instant - it
has no memory of what UTC offset a value was originally expressed in. A dive logged at
09:00 in Bangkok (+07:00) and one logged at 09:00 in London (+00:00) are different
instants, but once either is *read back*, Postgres/SQLAlchemy hand back a datetime in
whatever timezone the session is configured with (UTC here), not the original offset.

So the offset a `start_time` was entered in is stored separately (`Dive.utc_offset_minutes`)
and re-attached on the way out. This module is the single place that conversion happens,
so `api/v1/dives.py` and `schemas/dive.py` stay in sync on exactly how it's done.

**There is a third state, and only the logbook importer can create it.** A NULL
`utc_offset_minutes` means "the wall clock was recorded and the instant is unknown" -
DiveJSON's local date-time (spec §5.2), which exists because real migration sources
destroy offsets and the only alternatives are fabricating one or dropping the dive. For
such a row the `start_time` column holds the wall clock *labelled* UTC, because a
`timestamptz` has nowhere else to put it, and `combine_start_time` hands it back naive -
the same wall clock, with no zone claim attached to it.

**Creating the state is import's alone; carrying it forward is not.** Manual entry and the
parse path both know an offset, so `DiveCreate` demands one and the state never comes into
existence through a human write. But `PATCH /dive/{uuid}` accepts an offsetless
`start_time` on a dive whose offset is *already* unknown, so an imported dive's wall clock
stays editable without a client having to invent an offset to fix a typo - the value this
app exports for such a dive is then a value its own write API accepts. The same body
against a dive that has an offset is refused. That asymmetry - preserve, never remove - is
`split_updated_start_time` below.

**A fourth state sits beside it on the same terms: the date alone.** `dive.start_date_only`
says the day was recorded and the time of day was not - DiveJSON's bare `full-date`
`started_at` (spec §5.2). The column pair then holds midnight of that day labelled UTC with a
NULL offset, since a day has no instant, and `combine_dive_start_time` hands back the bare
`date`, never the midnight. Only the importer begins it; an update may keep it by sending a
bare date back, and any date-time ends it.
"""

import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

# What a client is told when it tries to *remove* an offset. Named rather than inlined
# because two suites and the web app's edit form all depend on the exact sentence, and
# because it has to explain the one case that is allowed - a message reading only "include
# a UTC offset" would send a client looking for a bug in a dive it can legitimately save.
START_TIME_OFFSET_REQUIRED_MESSAGE = (
    "start_time must include a UTC offset, e.g. '2021-04-04T10:04:47.910+02:00'. Only a dive whose own UTC "
    "offset is already unknown may be updated without one."
)

# What a client is told when it sends a bare date for a dive that has a time of day - the same
# shape as the sentence above, naming the one case where a date alone is accepted.
START_TIME_TIME_OF_DAY_REQUIRED_MESSAGE = (
    "start_time must include a time of day, e.g. '2021-04-04T10:04:47.910+02:00'. Only a dive whose own time of "
    "day is already unknown may be updated with a date alone."
)


def require_utc_offset(value: datetime) -> datetime:
    """Pydantic `AfterValidator` rejecting naive datetimes.

    Callers (the web frontend, or any other API client) always know their own UTC
    offset - a browser can read it off `Date.getTimezoneOffset()`, a dive-computer file
    parser either finds an explicit offset in the file or the caller falls back to the
    browser's offset - so the API requires it up front rather than guessing.

    **A write-side rule, and no longer the whole of the write side.** It guards
    `DiveCreate`, `DiveRenumberRequest.from_start_time` and `GET /dives/next-number`'s
    query parameter, because every one of those callers knows an offset. It is
    deliberately *not* on `DiveUpdate`: whether an offsetless update is legal depends on
    the dive being updated, which a schema cannot see, so that half of the write side is
    `split_updated_start_time` instead. The read shapes serve whatever is stored,
    offset-less rows included - see the module docstring.
    """
    if value.utcoffset() is None:
        raise ValueError(
            "start_time must include a UTC offset, e.g. '2021-04-04T10:04:47.910+02:00' (not a naive datetime)"
        )
    return value


_FULL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def full_date_is_a_date(value: Any) -> Any:
    """Pydantic `BeforeValidator`: a bare `YYYY-MM-DD` reads as a `date`, never as midnight.

    Pydantic's `datetime` coerces one to 00:00, which is the fabrication a date-only start
    exists to avoid (spec §5.2): the day was recorded and the time of day was not. Ahead of a
    `datetime | date` union, which would otherwise take the string as its first member.
    """
    if isinstance(value, str) and _FULL_DATE.fullmatch(value):
        return date.fromisoformat(value)
    return value


def split_start_time(start_time: datetime) -> tuple[datetime, int]:
    """Decompose an offset-aware datetime into (its UTC instant, its UTC offset in minutes).

    The instant is what actually gets stored in the `start_time` column; the offset is
    what gets stored in `utc_offset_minutes` alongside it, so the original offset can be
    reconstructed later via `combine_start_time()`.
    """
    offset = start_time.utcoffset()
    if offset is None:
        raise ValueError("start_time must be timezone-aware")
    offset_minutes = int(offset.total_seconds() // 60)
    return start_time.astimezone(UTC), offset_minutes


def split_local_start_time(start_time: datetime) -> tuple[datetime, int | None]:
    """`split_start_time`, admitting the offset-less state the logbook importer can carry.

    A naive value is DiveJSON's local date-time (spec §5.2): the wall clock is recorded
    and the instant is not. It is stored as that wall clock labelled UTC, paired with a
    NULL offset - so `combine_start_time` reads back exactly the characters that arrived,
    and no reader can mistake the label for a claim about the instant. Deliberately a
    second function rather than a looser `split_start_time`: every existing caller of that
    one has an offset and must keep failing loudly if it ever stops having one.
    """
    if start_time.utcoffset() is None:
        return start_time.replace(tzinfo=UTC), None
    return split_start_time(start_time)


def split_dive_start_time(start_time: datetime | date) -> tuple[datetime, int | None, bool]:
    """`split_local_start_time`, admitting the date-only state too: `(start_time, offset, date_only)`.

    A bare `date` is stored as midnight of that day labelled UTC with a NULL offset and the
    flag set - the column pair's value for "no instant", and midnight rather than any other
    hour so that a sort on the column places the dive at the start of its day. The flag is
    what stops that midnight being read back as a time anybody recorded.
    """
    if isinstance(start_time, datetime):
        return (*split_local_start_time(start_time), False)
    return datetime.combine(start_time, time(), tzinfo=UTC), None, True


def split_updated_start_time(
    start_time: datetime | date, stored_offset_minutes: int | None, stored_date_only: bool
) -> tuple[datetime, int | None, bool]:
    """`split_dive_start_time`, narrowed to what an *update* of an existing dive may do.

    **Preserving is allowed; removing is not**, and that asymmetry is the whole of it. An
    offsetless `start_time` against a dive whose `utc_offset_minutes` is already NULL
    leaves it NULL: the wall clock of an imported dive stays editable, and the round trip
    closes, since the offsetless `started_at` this app exports for such a dive is then a
    value its own dive-write API accepts. The same body against a dive that *has* an offset
    is refused - otherwise editing would be a second way to bring the unknown state into
    existence, and import would stop being its only origin.

    A bare date is the same rule one level down: accepted against a dive whose time of day is
    already unknown, keeping it unknown, and refused against one that has a time. Any
    date-time ends the date-only state - a time typed is a fact gained - and is then judged
    by the offset rule above, which a date-only dive's NULL offset always admits.

    An offset-aware value is accepted either way: adopting a real offset is the diver
    deciding they know one, which is a fact gained rather than lost.

    A `ValueError` rather than an HTTP exception, so the rule stays testable without a
    request and the route decides the status code - the division of labour
    `validate_depth_pair` has.
    """
    if not isinstance(start_time, datetime):
        if not stored_date_only:
            raise ValueError(START_TIME_TIME_OF_DAY_REQUIRED_MESSAGE)
        return split_dive_start_time(start_time)
    if start_time.utcoffset() is None and stored_offset_minutes is not None:
        raise ValueError(START_TIME_OFFSET_REQUIRED_MESSAGE)
    return split_dive_start_time(start_time)


def combine_start_time(utc_instant: datetime, offset_minutes: int | None) -> datetime:
    """Inverse of `split_start_time()`: re-attach a stored UTC offset to the UTC instant
    read back from the DB, so it serializes with the dive's original offset rather than
    `+00:00`.

    `None` is the offset-unknown state (see the module docstring) and comes back **naive**:
    the recorded wall clock with no zone attached, which is what serializes as the
    offset-less `"2026-04-17T11:49:23"` the format defines and what every caller then
    carries onward without converting it. A dive's reader takes `combine_dive_start_time`
    below instead, which also knows the date-only state.
    """
    if offset_minutes is None:
        return utc_instant.astimezone(UTC).replace(tzinfo=None)
    return utc_instant.astimezone(timezone(timedelta(minutes=offset_minutes)))


def combine_dive_start_time(utc_instant: datetime, offset_minutes: int | None, date_only: bool) -> datetime | date:
    """`combine_start_time` for a dive's column triple: the bare `date` where only the day was
    recorded, which serializes as `"2002-06-18"` - DiveJSON's own spelling of that state.

    A recording's start never takes this path: its `started_at` is a date-time or absent.
    """
    combined = combine_start_time(utc_instant, offset_minutes)
    return combined.date() if date_only else combined
