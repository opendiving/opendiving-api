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
the same wall clock, with no zone claim attached to it. Every write path a human touches
still requires an offset: manual entry and the parse path both know one, so the state
enters through import alone.
"""

from datetime import UTC, datetime, timedelta, timezone


def require_utc_offset(value: datetime) -> datetime:
    """Pydantic `AfterValidator` rejecting naive datetimes.

    Callers (the web frontend, or any other API client) always know their own UTC
    offset - a browser can read it off `Date.getTimezoneOffset()`, a dive-computer file
    parser either finds an explicit offset in the file or the caller falls back to the
    browser's offset - so the API requires it up front rather than guessing.

    **A write-side rule, not a read-side one.** It stays on `DiveCreate`, `DiveUpdate`,
    `DiveRenumberRequest.from_start_time` and `GET /dives/next-number`'s query parameter,
    because every one of those callers knows an offset. The read shapes serve whatever is
    stored, offset-less rows included - see the module docstring.
    """
    if value.utcoffset() is None:
        raise ValueError(
            "start_time must include a UTC offset, e.g. '2021-04-04T10:04:47.910+02:00' (not a naive datetime)"
        )
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


def combine_start_time(utc_instant: datetime, offset_minutes: int | None) -> datetime:
    """Inverse of `split_start_time()`: re-attach a stored UTC offset to the UTC instant
    read back from the DB, so it serializes with the dive's original offset rather than
    `+00:00`.

    `None` is the offset-unknown state (see the module docstring) and comes back **naive**:
    the recorded wall clock with no zone attached, which is what serializes as the
    offset-less `"2026-04-17T11:49:23"` the format defines and what every caller here -
    the dive read shapes, the activity chart's day buckets, the CSV and UDDF writers, the
    DiveJSON writer - then carries onward without converting it.
    """
    if offset_minutes is None:
        return utc_instant.astimezone(UTC).replace(tzinfo=None)
    return utc_instant.astimezone(timezone(timedelta(minutes=offset_minutes)))
