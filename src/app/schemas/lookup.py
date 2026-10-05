import uuid as uuid_pkg
from datetime import UTC, date, datetime, time
from typing import Annotated

from fastapi import Query

from ..core.utils.pagination import DEFAULT_MAX_ITEMS_PER_PAGE
from .dive import DiveLocalStartTime

# `DiveLocalStartTime` rather than `DiveStartTime`, because the hosts send what their form
# holds: a dive's start with an offset, without one or as a bare date, and every other host a
# plain date. The stricter type refuses all but the first.
LookupUntil = Annotated[
    DiveLocalStartTime | None,
    Query(
        description="The date of the record being edited: only dives at or before it rank an item. "
        "A date counts its whole day; a time without an offset reads as UTC. Omitted, every dive counts"
    ),
]

# Capped at the largest page, so every uuid one request names fits on one page of it.
LookupUuids = Annotated[
    list[uuid_pkg.UUID] | None,
    Query(
        max_length=DEFAULT_MAX_ITEMS_PER_PAGE,
        description="Only the rows with these uuids, repeated once per uuid: the ones a form holds without "
        "having searched for them. One that is not the caller's names nothing. The page still applies",
    ),
]


def lookup_bound(until: datetime | date | None) -> datetime | None:
    """The one instant a lookup compares `dive.start_time` against, and keys its cache on.

    A naive time is labelled UTC, as storage labels a dive whose offset is unknown; a date is
    the last microsecond of that day, so the date-only dive stored at its midnight and a timed
    dive later that day both count. One instant however it was spelled, so two spellings share
    a cache entry.
    """
    if until is None:
        return None
    if isinstance(until, datetime):
        return until.replace(tzinfo=UTC) if until.utcoffset() is None else until.astimezone(UTC)
    return datetime.combine(until, time.max, tzinfo=UTC)
