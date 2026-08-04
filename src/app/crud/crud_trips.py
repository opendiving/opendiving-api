from fastcrud import FastCRUD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.trip import Trip
from ..schemas.trip import TripCreateInternal, TripDelete, TripRead, TripUpdate, TripUpdateInternal

CRUDTrip = FastCRUD[Trip, TripCreateInternal, TripUpdate, TripUpdateInternal, TripDelete, TripRead]
crud_trips = CRUDTrip(Trip)


async def trip_belongs_to_user(db: AsyncSession, trip_id: int, user_id: int) -> bool:
    """Check whether a non-deleted trip with this id belongs to the given user.

    Used to prevent a user from linking another user's trip to their own dive.
    """
    stmt = select(Trip.id).where(
        Trip.id == trip_id,
        Trip.user_id == user_id,
        Trip.is_deleted.is_(False),
    )
    result = await db.execute(stmt.limit(1))
    return result.first() is not None


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
