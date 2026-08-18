import uuid as uuid_pkg

from fastcrud import FastCRUD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.trip import Trip
from ..schemas.trip import TripCreateInternal, TripReadInternal, TripUpdate, TripUpdateInternal

CRUDTrip = FastCRUD[Trip, TripCreateInternal, TripUpdate, TripUpdateInternal, TripUpdate, TripReadInternal]
crud_trips = CRUDTrip(Trip)


async def resolve_trip_id_for_user(db: AsyncSession, trip_uuid: uuid_pkg.UUID, user_id: int) -> int | None:
    """Resolve a trip's public `uuid` to its internal `id`, scoped to a trip belonging to
    the given user.

    Used to translate a client-supplied trip reference into the internal id needed for
    FK storage/joins, while also preventing a user from linking another user's trip to
    their own dive.
    """
    stmt = select(Trip.id).where(
        Trip.uuid == trip_uuid,
        Trip.user_id == user_id,
    )
    result = await db.execute(stmt.limit(1))
    row = result.first()
    return row[0] if row is not None else None


async def get_trip_uuids_by_ids(db: AsyncSession, trip_ids: list[int], user_id: int) -> dict[int, uuid_pkg.UUID]:
    """Batched lookup of trip `id` -> `uuid`, e.g. for enriching a paginated dive listing.

    A miss is no longer an expected outcome. Trips are hard-deleted and `dive.trip_id` is
    `ON DELETE SET NULL`, so deleting a trip clears the column on every dive that pointed
    at it rather than leaving an id behind for this lookup to decline - the callers' `None`
    now comes from the row itself, and their `if trip_id is not None` guard is what
    produces it. This used to filter `is_deleted` and turn a hidden trip into the same
    null; there is no hidden trip to filter for.

    The `user_id` scope is defence in depth rather than a fix: today every caller passes
    ids taken from the caller's own dives, so a cross-user id cannot arrive. Scoping it
    here means a future caller that sources ids some other way cannot leak a uuid, and
    matches `resolve_trip_id_for_user` above.
    """
    if not trip_ids:
        return {}

    result = await db.execute(
        select(Trip.id, Trip.uuid).where(
            Trip.id.in_(set(trip_ids)),
            Trip.user_id == user_id,
        )
    )
    return {row.id: row.uuid for row in result}


async def trip_name_exists(db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None) -> bool:
    """Case-insensitive check for whether a trip with this name already exists for the user.

    Mirrors the `ux_trip_user_id_name_lower` unique index, which enforces the same rule
    case-insensitively at the database level as a safety net.
    """
    stmt = select(Trip.id).where(
        Trip.user_id == user_id,
        func.lower(Trip.name) == name.strip().lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(Trip.id != exclude_id)

    result = await db.execute(stmt.limit(1))
    return result.first() is not None
