from collections.abc import Iterable
from datetime import date
from typing import Protocol


class _Dated(Protocol):
    """Anything shaped like a trip part - the ORM row and `TripPartRead` both are."""

    @property
    def start_date(self) -> date | None: ...

    @property
    def end_date(self) -> date | None: ...


def trip_span(parts: Iterable[_Dated]) -> tuple[date | None, date | None]:
    """A trip's span: the earliest `start_date` and the latest `end_date` across its parts.

    The one place this is derived in Python, so that a second caller cannot drift into a
    second spelling of it. `trips.csv` is that caller today, and the only one: neither the
    UDDF writer nor the DiveJSON one wants a span, each giving a part its own dates
    instead. Each pair is taken independently: a trip whose only dated part carries an end
    and no start has an end and no start, which is what the data says rather than an
    invented range.

    `(None, None)` for a trip with no parts or no dates on any of them. That is a state
    the app has never had before, and every caller has to answer for it rather than
    formatting an absence.

    `GET /trips` orders by the same earliest start, but in SQL rather than through here -
    a page of trips cannot be sorted by a value Python computes after the LIMIT.
    """
    starts = [part.start_date for part in parts if part.start_date is not None]
    ends = [part.end_date for part in parts if part.end_date is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)
