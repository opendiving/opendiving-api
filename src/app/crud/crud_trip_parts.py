import uuid as uuid_pkg
from collections.abc import Mapping
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.contact import Contact
from ..models.trip import Trip
from ..models.trip_part import TripPart
from ..schemas.location import LOCATION_FIELDS, location_columns, location_from_row
from ..schemas.trip import TripPartInput, TripPartRead

# The place's columns, bare: a part has no position of its own for the locality's to be
# confused with, which is why these carry none of the `location_` prefix a dive site's do.
_LOCATION_COLUMNS = tuple(getattr(TripPart, field) for field in LOCATION_FIELDS)

# The accommodation comes back as the contact's public uuid, joined in rather than looked
# up per part: a part is read only ever with the rest of its trip, so the join rides the
# same query.
_READ_COLUMNS = (
    TripPart.start_date,
    TripPart.end_date,
    *_LOCATION_COLUMNS,
    Contact.uuid.label("accommodation_uuid"),
)


def _select_parts(*extra: Any) -> Any:
    return select(*extra, *_READ_COLUMNS).outerjoin(Contact, Contact.id == TripPart.accommodation_contact_id)


def _to_read(row: Any) -> TripPartRead:
    """A row into a part, with the place nested rather than flattened beside the dates."""
    return TripPartRead(
        start_date=row.start_date,
        end_date=row.end_date,
        location=location_from_row(row),
        accommodation_uuid=row.accommodation_uuid,
    )


async def get_parts_for_trip(db: AsyncSession, trip_id: int) -> list[TripPartRead]:
    """Return a trip's parts, in the order the diver arranged them."""
    result = await db.execute(_select_parts().where(TripPart.trip_id == trip_id).order_by(TripPart.position))
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
        _select_parts(TripPart.trip_id)
        .where(TripPart.trip_id.in_(set(trip_ids)))
        .order_by(TripPart.trip_id, TripPart.position)
    )
    for row in result:
        parts_by_trip[row.trip_id].append(_to_read(row))
    return parts_by_trip


async def replace_parts_for_trip(
    db: AsyncSession,
    trip_id: int,
    parts: list[TripPartInput],
    commit: bool = True,
    accommodation_ids: Mapping[uuid_pkg.UUID, int] | None = None,
) -> None:
    """Replace all of a trip's parts with the given ordered list.

    Delete-then-insert rather than a diff: these are value objects with nothing stable to
    match old rows against (duplicate names are legal, and a part may have no name at
    all), and `position` is just the index in the list the client sent.

    `accommodation_ids` maps each part's `accommodation_uuid` to the contact's id, already
    resolved against the trip's owner by `resolve_contact_ids_for_user` - the route does
    that before it writes anything, so a foreign uuid is a 422 rather than half a trip.
    """
    await db.execute(delete(TripPart).where(TripPart.trip_id == trip_id))
    for position, part in enumerate(parts):
        db.add(
            TripPart(
                trip_id=trip_id,
                position=position,
                start_date=part.start_date,
                end_date=part.end_date,
                **location_columns(part.location),
                accommodation_contact_id=(
                    None if part.accommodation_uuid is None else (accommodation_ids or {})[part.accommodation_uuid]
                ),
            )
        )
    if commit:
        await db.commit()


async def get_trip_uuids_staying_at(db: AsyncSession, contact_id: int) -> list[uuid_pkg.UUID]:
    """The trips with a part whose accommodation is this contact - what deleting the
    contact has to drop from the single-trip cache, whose key names no user."""
    result = await db.execute(
        select(Trip.uuid)
        .join(TripPart, TripPart.trip_id == Trip.id)
        .where(TripPart.accommodation_contact_id == contact_id)
        .distinct()
    )
    return list(result.scalars())
