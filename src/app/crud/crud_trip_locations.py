from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.trip_location import TripLocation
from ..schemas.trip import TripLocationInput, TripLocationRead

_READ_COLUMNS = (
    TripLocation.name,
    TripLocation.display_name,
    TripLocation.latitude,
    TripLocation.longitude,
    TripLocation.bbox_south,
    TripLocation.bbox_north,
    TripLocation.bbox_west,
    TripLocation.bbox_east,
)


def _to_read(row: Any) -> TripLocationRead:
    return TripLocationRead(
        name=row.name,
        display_name=row.display_name,
        latitude=row.latitude,
        longitude=row.longitude,
        bbox_south=row.bbox_south,
        bbox_north=row.bbox_north,
        bbox_west=row.bbox_west,
        bbox_east=row.bbox_east,
    )


async def get_locations_for_trip(db: AsyncSession, trip_id: int) -> list[TripLocationRead]:
    """Return a trip's locations, in the order they were listed."""
    result = await db.execute(
        select(*_READ_COLUMNS).where(TripLocation.trip_id == trip_id).order_by(TripLocation.position)
    )
    return [_to_read(row) for row in result]


async def get_locations_for_trips(db: AsyncSession, trip_ids: list[int]) -> dict[int, list[TripLocationRead]]:
    """Batched version of `get_locations_for_trip`, e.g. for a paginated trip listing.

    Pre-seeded with every requested id so a trip with no locations reads back as an empty
    list rather than a missing key.
    """
    locations_by_trip: dict[int, list[TripLocationRead]] = {trip_id: [] for trip_id in trip_ids}
    if not trip_ids:
        return locations_by_trip

    result = await db.execute(
        select(TripLocation.trip_id, *_READ_COLUMNS)
        .where(TripLocation.trip_id.in_(set(trip_ids)))
        .order_by(TripLocation.trip_id, TripLocation.position)
    )
    for row in result:
        locations_by_trip[row.trip_id].append(_to_read(row))
    return locations_by_trip


async def replace_locations_for_trip(
    db: AsyncSession, trip_id: int, locations: list[TripLocationInput], commit: bool = True
) -> None:
    """Replace all of a trip's locations with the given ordered list.

    Delete-then-insert rather than a diff: these are value objects with nothing stable to
    match old rows against (duplicate names are legal), and `position` is just the index
    in the list the client sent.
    """
    await db.execute(delete(TripLocation).where(TripLocation.trip_id == trip_id))
    for position, location in enumerate(locations):
        db.add(
            TripLocation(
                trip_id=trip_id,
                name=location.name,
                position=position,
                display_name=location.display_name,
                latitude=location.latitude,
                longitude=location.longitude,
                bbox_south=location.bbox_south,
                bbox_north=location.bbox_north,
                bbox_west=location.bbox_west,
                bbox_east=location.bbox_east,
            )
        )
    if commit:
        await db.commit()
