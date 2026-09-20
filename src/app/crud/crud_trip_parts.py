from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.trip_part import TripPart
from ..schemas.trip import TripLocationRead, TripPartInput, TripPartRead

_LOCATION_COLUMNS = (
    TripPart.name,
    TripPart.display_name,
    TripPart.latitude,
    TripPart.longitude,
    TripPart.bbox_south,
    TripPart.bbox_north,
    TripPart.bbox_west,
    TripPart.bbox_east,
)

_READ_COLUMNS = (TripPart.start_date, TripPart.end_date, *_LOCATION_COLUMNS)


def _to_read(row: Any) -> TripPartRead:
    """A row into a part, with the place nested rather than flattened beside the dates.

    `name` is what says whether there is a place at all: it is the one column a location
    must have, so a row with none is a part the diver gave dates and no place.
    """
    location = (
        None
        if row.name is None
        else TripLocationRead(
            name=row.name,
            display_name=row.display_name,
            latitude=row.latitude,
            longitude=row.longitude,
            bbox_south=row.bbox_south,
            bbox_north=row.bbox_north,
            bbox_west=row.bbox_west,
            bbox_east=row.bbox_east,
        )
    )
    return TripPartRead(start_date=row.start_date, end_date=row.end_date, location=location)


async def get_parts_for_trip(db: AsyncSession, trip_id: int) -> list[TripPartRead]:
    """Return a trip's parts, in the order the diver arranged them."""
    result = await db.execute(select(*_READ_COLUMNS).where(TripPart.trip_id == trip_id).order_by(TripPart.position))
    return [_to_read(row) for row in result]


async def get_parts_for_trips(db: AsyncSession, trip_ids: list[int]) -> dict[int, list[TripPartRead]]:
    """Batched version of `get_parts_for_trip`, e.g. for a paginated trip listing.

    Pre-seeded with every requested id so a trip with no parts reads back as an empty list
    rather than a missing key.
    """
    parts_by_trip: dict[int, list[TripPartRead]] = {trip_id: [] for trip_id in trip_ids}
    if not trip_ids:
        return parts_by_trip

    result = await db.execute(
        select(TripPart.trip_id, *_READ_COLUMNS)
        .where(TripPart.trip_id.in_(set(trip_ids)))
        .order_by(TripPart.trip_id, TripPart.position)
    )
    for row in result:
        parts_by_trip[row.trip_id].append(_to_read(row))
    return parts_by_trip


async def replace_parts_for_trip(
    db: AsyncSession, trip_id: int, parts: list[TripPartInput], commit: bool = True
) -> None:
    """Replace all of a trip's parts with the given ordered list.

    Delete-then-insert rather than a diff: these are value objects with nothing stable to
    match old rows against (duplicate names are legal, and a part may have no name at
    all), and `position` is just the index in the list the client sent.
    """
    await db.execute(delete(TripPart).where(TripPart.trip_id == trip_id))
    for position, part in enumerate(parts):
        location = part.location
        db.add(
            TripPart(
                trip_id=trip_id,
                position=position,
                start_date=part.start_date,
                end_date=part.end_date,
                name=None if location is None else location.name,
                display_name=None if location is None else location.display_name,
                latitude=None if location is None else location.latitude,
                longitude=None if location is None else location.longitude,
                bbox_south=None if location is None else location.bbox_south,
                bbox_north=None if location is None else location.bbox_north,
                bbox_west=None if location is None else location.bbox_west,
                bbox_east=None if location is None else location.bbox_east,
            )
        )
    if commit:
        await db.commit()
