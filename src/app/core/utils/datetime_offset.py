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
"""

from datetime import UTC, datetime, timedelta, timezone


def require_utc_offset(value: datetime) -> datetime:
    """Pydantic `AfterValidator` rejecting naive datetimes.

    Callers (the web frontend, or any other API client) always know their own UTC
    offset - a browser can read it off `Date.getTimezoneOffset()`, a dive-computer file
    parser either finds an explicit offset in the file or the caller falls back to the
    browser's offset - so the API requires it up front rather than guessing.
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


def combine_start_time(utc_instant: datetime, offset_minutes: int) -> datetime:
    """Inverse of `split_start_time()`: re-attach a stored UTC offset to the UTC instant
    read back from the DB, so it serializes with the dive's original offset rather than
    `+00:00`.
    """
    return utc_instant.astimezone(timezone(timedelta(minutes=offset_minutes)))
