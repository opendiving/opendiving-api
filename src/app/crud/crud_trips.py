import uuid as uuid_pkg
from typing import Any

from fastcrud import FastCRUD
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.search import LIKE_ESCAPE_CHAR, escape_like
from ..models.trip import Trip
from ..models.trip_part import TripPart
from ..schemas.trip import TripCreateInternal, TripReadInternal, TripUpdate, TripUpdateInternal

CRUDTrip = FastCRUD[Trip, TripCreateInternal, TripUpdate, TripUpdateInternal, TripUpdate, TripReadInternal]
crud_trips = CRUDTrip(Trip)

# A trip's span is not stored, so anything that orders trips by date is a correlated
# aggregate over the parts. `ix_trip_part_trip_id_position` leads with `trip_id`, so each
# trip costs an index lookup over a handful of rows - the development corpus's busiest
# account holds 25 trips. Public because the export loader orders by it too, ascending:
# two spellings of one derivation would be two places for it to drift.
EARLIEST_PART_START = (
    select(func.min(TripPart.start_date)).where(TripPart.trip_id == Trip.id).correlate(Trip).scalar_subquery()
)

# Most recent trip first, and a trip whose parts carry no dates last. `NULLS LAST` is the
# behaviour as much as a default to override: a trip with no dated part is a legal state
# from this revision on, and Postgres's default for `DESC` would read "no date" as
# "soonest" and float it to the top of every diver's list.
#
# `uuid` breaks ties: it is uuid7, so it orders by creation time, which keeps pagination
# stable across pages when several trips share an earliest start (or have none).
_LIST_ORDER = (EARLIEST_PART_START.desc().nulls_last(), Trip.uuid.desc())


def search_conditions(user_id: int, term: str) -> tuple[ColumnElement[bool], ...]:
    """The `WHERE` clauses matching a user's trips against a search term.

    A trip is as often remembered by where it went as by what it was called, and where it
    went is rows in `trip_part`. The EXISTS is what preserves that - typing "moalboal"
    finds the trip named "Cebu 2026" that went there.
    """
    pattern = f"%{escape_like(term)}%"
    return (
        Trip.user_id == user_id,
        or_(
            Trip.name.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
            select(TripPart.id)
            .where(
                TripPart.trip_id == Trip.id,
                or_(
                    TripPart.name.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
                    # The fuller name too, so "philippines" finds a trip whose parts are
                    # all named after towns and whose names stop at the country.
                    TripPart.full_name.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
                ),
            )
            .exists(),
        ),
    )


async def get_trips_page(
    db: AsyncSession, *, user_id: int, offset: int, limit: int, search: str | None = None
) -> dict[str, Any]:
    """One page of a diver's trips, most recent first, in the same
    `{"data": [...], "total_count": n}` shape `crud.get_multi` returns.

    Hand-written for `_LIST_ORDER`, as `get_courses_page` is - and, like that one, serving
    both branches rather than one. Neither path through the factory can express this
    ordering: `get_multi`'s `sort_orders` is `'asc'`/`'desc'` with no null placement, and
    `core/utils/search.py::search_multi` builds a bare `.desc()` from a single named sort
    column, which the earliest start is not - it is an aggregate over another table.

    Rows come back as plain dicts of every table column, matching `get_multi` called
    without a `schema_to_select`, so a caller can hand them to the same public-shape
    conversion either way - including the internal `id` the parts hang off.
    """
    # Replaced rather than appended to: `search_conditions` carries the ownership scope
    # itself, so that nothing can call it and get an unscoped clause.
    conditions: tuple[ColumnElement[bool], ...] = (Trip.user_id == user_id,)
    term = (search or "").strip()
    if term:
        conditions = search_conditions(user_id=user_id, term=term)

    total_count = await db.scalar(select(func.count()).select_from(Trip).where(*conditions))
    rows = (
        await db.execute(
            select(*Trip.__table__.columns).where(*conditions).order_by(*_LIST_ORDER).offset(offset).limit(limit)
        )
    ).mappings()

    return {"data": [dict(row) for row in rows], "total_count": total_count or 0}


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
