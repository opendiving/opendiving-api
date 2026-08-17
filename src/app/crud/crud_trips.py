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


async def get_trip_uuids_by_ids(db: AsyncSession, trip_ids: list[int], user_id: int) -> dict[int, uuid_pkg.UUID]:
    """Batched lookup of trip `id` -> `uuid`, e.g. for enriching a paginated dive listing.

    Resolves only a *live* trip of this user's, which makes an unresolvable `trip_id` an
    expected outcome rather than a missing row: a dive keeps its `trip_id` when its trip is
    soft-deleted, and callers turn the resulting miss into `trip_uuid: null`. That is what
    stops a dive reporting a trip `GET /trip/{uuid}` answers 404 for, and it matches the
    write side - `resolve_trip_id_for_user` refuses a deleted trip, so a `trip_uuid` this
    returned would be one `PATCH /dive` then rejected.

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
            Trip.is_deleted.is_(False),
        )
    )
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
