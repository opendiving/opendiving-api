import uuid as uuid_pkg

from fastcrud import FastCRUD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.trip import Trip
from ..schemas.trip import TripCreateInternal, TripDelete, TripReadInternal, TripUpdate, TripUpdateInternal

CRUDTrip = FastCRUD[Trip, TripCreateInternal, TripUpdate, TripUpdateInternal, TripDelete, TripReadInternal]
crud_trips = CRUDTrip(Trip)


async def resolve_trip_id_for_user(db: AsyncSession, trip_uuid: uuid_pkg.UUID, user_id: int) -> int | None:
    """Resolve a trip's public `uuid` to its internal `id`, scoped to a non-deleted trip
    belonging to the given user.

    Used to translate a client-supplied trip reference into the internal id needed for
    FK storage/joins, while also preventing a user from linking another user's trip to
    their own dive.
    """
    stmt = select(Trip.id).where(
        Trip.uuid == trip_uuid,
        Trip.user_id == user_id,
        Trip.is_deleted.is_(False),
    )
    result = await db.execute(stmt.limit(1))
    row = result.first()
    return row[0] if row is not None else None


async def get_trip_uuids_by_ids(db: AsyncSession, trip_ids: list[int]) -> dict[int, uuid_pkg.UUID]:
    """Batched lookup of trip `id` -> `uuid`, e.g. for enriching a paginated dive listing."""
    if not trip_ids:
        return {}

    result = await db.execute(select(Trip.id, Trip.uuid).where(Trip.id.in_(set(trip_ids))))
    return {row.id: row.uuid for row in result}


async def trip_name_exists(db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None) -> bool:
    """Case-insensitive check for whether a non-deleted trip with this name already exists for the user.

    Mirrors the `ux_trip_user_id_name_lower` partial unique index, which enforces the same rule
    (case-insensitively, ignoring soft-deleted trips) at the database level as a safety net.
    """
    stmt = select(Trip.id).where(
        Trip.user_id == user_id,
        Trip.is_deleted.is_(False),
        func.lower(Trip.name) == name.strip().lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(Trip.id != exclude_id)

    result = await db.execute(stmt.limit(1))
    return result.first() is not None
