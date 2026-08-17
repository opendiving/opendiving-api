import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    DuplicateValueException,
    ForbiddenException,
    NotFoundException,
    UnprocessableEntityException,
)
from ...core.schemas import DeletedWithMovedDives
from ...core.utils.cache import cache
from ...core.utils.owned_resource_cache import OwnedResourceCache
from ...core.utils.pagination import clamp_pagination
from ...core.utils.search import LIKE_ESCAPE_CHAR, escape_like, search_multi
from ...crud.crud_dives import reassign_dives_to_trip
from ...crud.crud_trip_locations import (
    get_locations_for_trip,
    get_locations_for_trips,
    replace_locations_for_trip,
)
from ...crud.crud_trips import crud_trips, resolve_trip_id_for_user, trip_name_exists
from ...models.trip import Trip
from ...models.trip_location import TripLocation
from ...schemas.trip import (
    TripCreate,
    TripCreateInternal,
    TripLocationRead,
    TripRead,
    TripReadInternal,
    TripUpdateRequest,
)
from ...services.cache_invalidation import invalidate_dive_caches

router = APIRouter(tags=["trips"])

# The only integrity failure the location writes below can hit is the trip row vanishing
# between the write and the insert (a concurrent hard delete): lengths and ranges are
# already bounded by `TripLocationInput`, and there is no unique constraint to violate.
# 422 rather than a raw 500, matching how `patch_dive` treats its child-row writes.
#
# Both routes raise before invalidating anything, so this path knowingly leaves the trip's
# caches as they were - including any column update `patch_trip` already committed. Same
# as `patch_dive`, and the trade is deliberate: the trigger needs a hard delete, which no
# route offers, and invalidating on the way out of a failed write would mean doing it in
# two places for a case that cannot currently happen.
_LOCATION_ERROR_DETAIL = "Trip locations could not be saved."

# The list is ordered by most recent start date, in one place: `_trip_cache` no longer
# reads it (its `read_list` is unused), but it still takes it, and `_cached_read_trips`
# has two branches of its own. Three copies means changing the one that does nothing and
# seeing no change.
_SORT_COLUMN = "start_date"
_SORT_ORDER = "desc"


async def _get_owned_trip(
    db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict, *, include_deleted: bool = False
) -> TripReadInternal:
    """Fetch a trip by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for why someone else's row reads
    as a 404 and, in particular, why this must run before any `@cache`-wrapped read
    helper.
    """
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_trips,
        uuid=uuid,
        current_user=current_user,
        schema=TripReadInternal,
        not_found_message="Trip not found",
        include_deleted=include_deleted,
    )


def _to_public_trip(
    db_trip: TripReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    locations: list[TripLocationRead] | None = None,
) -> TripRead:
    """Convert an internal trip representation (integer FKs) into its public shape
    (owning user referenced by `uuid`, places embedded as read from the child table)."""
    data = db_trip if isinstance(db_trip, dict) else db_trip.model_dump()
    return TripRead(
        **{k: v for k, v in data.items() if k not in ("id", "user_id")},
        user_uuid=user_uuid,
        locations=locations or [],
    )


# Kept for its `list_cache_key_prefix` and `invalidate_list` only - `read_list`/`read_item`
# are no longer called. A trip read now embeds its locations, which is a second query
# zipped back into the page, and that is precisely the step the factory has no room for
# (see its docstring, which lists `dives.py` and the other opt-outs). The hand-rolled
# helpers below reproduce its key shapes exactly, so invalidation is unaffected.
#
# `search_columns` still has to be non-empty: it is what keeps the `:search:{search}`
# segment in the key. Only `name` remains a column - a trip's places moved to
# `trip_location`, and `_search_conditions` below is what searches them.
_trip_cache: OwnedResourceCache[TripReadInternal, TripRead] = OwnedResourceCache(
    resource_name="trips",
    resource_label="Trip",
    item_cache_prefix="trip_cache",
    crud=crud_trips,
    schema_to_select=TripReadInternal,
    to_public=lambda db_trip, user_uuid: _to_public_trip(db_trip, user_uuid=user_uuid),
    sort_columns=_SORT_COLUMN,
    sort_orders=_SORT_ORDER,
    search_columns=("name",),
)


def _search_conditions(user_id: int, term: str) -> tuple[ColumnElement[bool], ...]:
    """The `WHERE` clauses matching a user's non-deleted trips against a search term.

    Hand-written rather than `OwnedResourceCache.search_conditions`, which can only OR
    columns of one table: a trip is as often remembered by where it went as by what it
    was called, and where it went is now rows in `trip_location`. The EXISTS is what
    preserves that - typing "moalboal" finds the trip named "Cebu 2026" that went there.
    """
    pattern = f"%{escape_like(term)}%"
    return (
        Trip.user_id == user_id,
        Trip.is_deleted.is_(False),
        or_(
            Trip.name.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
            select(TripLocation.id)
            .where(
                TripLocation.trip_id == Trip.id,
                or_(
                    TripLocation.name.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
                    # The display name too, so "philippines" finds a trip whose locations
                    # are all named after towns.
                    TripLocation.display_name.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
                ),
            )
            .exists(),
        ),
    )


@router.post("/trip", response_model=TripRead, status_code=201)
async def write_trip(
    request: Request,
    trip: TripCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> TripRead:
    """Create a trip for the authenticated user, together with the places it went to.

    `user_uuid` in the body must be the caller's own: a mismatch is a 403 rather than a
    silent reassignment to the caller. Trip names are unique per user, so reusing one
    that already exists is a 422. `locations` keep the order given; index 0 is the one
    shown wherever only a single place fits.

    The trip row commits before its locations do, so a failure inserting them leaves the
    trip behind without them - the same accepted semantics as `POST /dive` and its
    mixtures.
    """
    if current_user["uuid"] != trip.user_uuid:
        raise ForbiddenException()

    if await trip_name_exists(db=db, user_id=current_user["id"], name=trip.name):
        raise DuplicateValueException("A trip with this name already exists")

    # `locations` is excluded rather than filtered downstream: `TripCreateInternal` is
    # `extra="forbid"`, and locations are rows in another table, not a trip column.
    trip_internal_dict = trip.model_dump(exclude={"user_uuid", "locations"})
    trip_internal = TripCreateInternal(**trip_internal_dict, user_id=current_user["id"])
    created_trip = await crud_trips.create(
        db=db, object=trip_internal, schema_to_select=TripReadInternal, return_as_model=True
    )

    try:
        await replace_locations_for_trip(db=db, trip_id=created_trip.id, locations=trip.locations)
    except IntegrityError as e:
        await db.rollback()
        raise UnprocessableEntityException(_LOCATION_ERROR_DETAIL) from e

    await _trip_cache.invalidate_list(current_user["id"])

    trip_read = await crud_trips.get(db=db, id=created_trip.id, schema_to_select=TripReadInternal, return_as_model=True)
    if trip_read is None:
        raise NotFoundException("Created trip not found")

    locations = await get_locations_for_trip(db=db, trip_id=created_trip.id)
    return _to_public_trip(cast(TripReadInternal, trip_read), user_uuid=current_user["uuid"], locations=locations)


@cache(
    key_prefix=_trip_cache.list_cache_key_prefix,
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_trips(
    request: Request,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    db: AsyncSession,
    page: int,
    items_per_page: int,
    search: str | None,
) -> dict:
    """Fetches (and caches) a user's paginated trip list, each trip with its locations.

    Only ever called after `read_trips` below has checked the caller's authorization - a
    `@cache` hit skips this body entirely, authorization logic included.

    The kwarg names are load-bearing: `user_id`, `page`, `items_per_page` and `search`
    fill the placeholders in the key prefix this borrows from `_trip_cache`, which is
    what keeps the keys byte-identical to the ones `invalidate_list` sweeps.
    """
    offset = compute_offset(page, items_per_page)
    term = (search or "").strip()

    trips_data: dict[str, Any]
    if term:
        trips_data = await search_multi(
            db=db,
            model=Trip,
            conditions=_search_conditions(user_id=user_id, term=term),
            sort_column=_SORT_COLUMN,
            sort_order=_SORT_ORDER,
            offset=offset,
            limit=items_per_page,
        )
    else:
        trips_data = cast(
            dict[str, Any],
            await crud_trips.get_multi(
                db=db,
                offset=offset,
                limit=items_per_page,
                user_id=user_id,
                is_deleted=False,
                sort_columns=_SORT_COLUMN,
                sort_orders=_SORT_ORDER,
            ),
        )

    # One batched query for the page rather than one per trip. Both branches above return
    # full-column dicts (`get_multi` without a `schema_to_select`, and `search_multi` by
    # construction), so the internal `id` the child rows hang off is there to read.
    locations_by_trip = await get_locations_for_trips(db=db, trip_ids=[trip["id"] for trip in trips_data["data"]])

    trips_data["data"] = [
        _to_public_trip(trip, user_uuid=user_uuid, locations=locations_by_trip.get(trip["id"], [])).model_dump()
        for trip in trips_data["data"]
    ]

    response: dict[str, Any] = paginated_response(crud_data=trips_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/trips", response_model=PaginatedListResponse[TripRead])
async def read_trips(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive substring match on the trip's name or its locations"),
    ] = None,
) -> dict:
    """List the caller's trips, most recent start date first, each with its locations.

    `user_uuid` must be the caller's own (403 otherwise) - this endpoint cannot be used
    to read another user's trips. `search` matches a case-insensitive substring against
    the trip's name and the names of the places it went to, so a trip is findable by
    either. Out-of-range pagination is clamped rather than rejected, so `items_per_page`
    above the ceiling returns the ceiling instead of a 422.
    """
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    page, items_per_page = clamp_pagination(page, items_per_page)

    return await _cached_read_trips(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=page,
        items_per_page=items_per_page,
        # Normalized here rather than in the cache layer so that " Dahab " and "dahab"
        # share one cache entry instead of two identical ones under different keys.
        search=(search or "").strip().lower() or None,
    )


# The same `trip_cache:{uuid}` key the PATCH and DELETE decorators below already delete,
# so moving off `OwnedResourceCache.read_item` changed nothing about invalidation.
@cache(key_prefix="trip_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_trip(
    request: Request, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> TripRead:
    """Fetches (and caches) a single trip by uuid, with its locations attached.

    Like `_cached_read_trips`, this must only be called once the route has established
    that the caller owns the trip: `@cache` can serve a hit without re-checking it.
    """
    db_trip = await crud_trips.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=TripReadInternal, return_as_model=True
    )
    if db_trip is None:
        raise NotFoundException("Trip not found")
    db_trip = cast(TripReadInternal, db_trip)

    locations = await get_locations_for_trip(db=db, trip_id=db_trip.id)
    return _to_public_trip(db_trip, user_uuid=owner_uuid, locations=locations)


@router.get("/trip/{uuid}", response_model=TripRead)
async def read_trip(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> TripRead:
    """Return a single trip by its public uuid, with the places it went to.

    404 when no such trip exists - and the same 404 when it belongs to another user, so
    someone else's uuid stays unprobeable.
    """
    # Authorize before the cached read: `@cache` replays a hit without re-checking.
    await _get_owned_trip(db, uuid, current_user)

    return await _cached_read_trip(request, uuid=uuid, owner_uuid=current_user["uuid"], db=db)


@router.patch("/trip/{uuid}")
@cache("trip_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def patch_trip(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: TripUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update a trip; omitted fields are left untouched.

    404 unless the caller owns it, exactly as for a trip that doesn't exist. Renaming to
    a name the caller already has on another trip is a 422. `locations` is replaced
    wholesale when present rather than merged, so sending a shorter list removes the
    difference and an empty list clears them; omitting the key leaves them alone.
    """
    db_trip = await _get_owned_trip(db, uuid, current_user)

    if values.name is not None and await trip_name_exists(
        db=db, user_id=db_trip.user_id, name=values.name, exclude_id=db_trip.id
    ):
        raise DuplicateValueException("A trip with this name already exists")

    update_data = values.model_dump(exclude={"locations"}, exclude_unset=True)
    if update_data:
        await crud_trips.update(db=db, object=update_data, uuid=uuid)

    if values.locations is not None:
        try:
            await replace_locations_for_trip(db=db, trip_id=db_trip.id, locations=values.locations)
        except IntegrityError as e:
            await db.rollback()
            raise UnprocessableEntityException(_LOCATION_ERROR_DETAIL) from e

    # A locations-only edit has an empty `update_data` but still changes what the list
    # pages say: the decorator on this route only drops `trip_cache:{uuid}`.
    if update_data or values.locations is not None:
        await _trip_cache.invalidate_list(db_trip.user_id)

    return {"message": "Trip updated"}


@router.delete("/trip/{uuid}")
@cache("trip_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def erase_trip(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    move_dives_to: Annotated[
        uuid_pkg.UUID | None,
        Query(description="Move this trip's dives onto the trip with this uuid before deleting it"),
    ] = None,
) -> DeletedWithMovedDives:
    """Soft-delete a trip, optionally moving its dives onto another trip first.

    404 unless the caller owns it, exactly as for a trip that doesn't exist. The row is
    flagged rather than removed, so dives that referenced this trip keep their `trip_id` -
    the trip simply stops appearing in reads.

    Pass `move_dives_to` and every one of the caller's live dives on this trip is
    re-pointed at that one first, in the same transaction as the delete: either the diver's
    log ends up entirely on the replacement trip with this one gone, or nothing happened.
    A replacement that isn't the caller's own live trip, or that is this trip, is a 422 -
    the same answer `PATCH /dive` gives for a `trip_uuid` it can't resolve, which is the
    per-dive call this parameter exists to replace.

    `moved_dives` counts what was re-pointed, for the "12 dives moved to Cebu 2026" the web
    app says afterwards. It is present either way, and 0 when the parameter was omitted.
    """
    db_trip = await _get_owned_trip(db, uuid, current_user)
    owner_id = db_trip.user_id

    moved_dives = 0
    if move_dives_to is not None:
        if move_dives_to == uuid:
            raise UnprocessableEntityException("A trip cannot be moved onto itself.")
        replacement_id = await resolve_trip_id_for_user(db=db, trip_uuid=move_dives_to, user_id=owner_id)
        if replacement_id is None:
            raise UnprocessableEntityException("Trip not found.")
        moved_dives = await reassign_dives_to_trip(
            db=db, user_id=owner_id, from_trip_id=db_trip.id, to_trip_id=replacement_id
        )

    # Commits the reassignment above along with the delete - `crud_trips.delete` is the
    # only writer here that commits, and both wrote through this one session.
    await crud_trips.delete(db=db, uuid=uuid)
    await _trip_cache.invalidate_list(owner_id)
    # Only when dives actually moved. A plain delete leaves every `dive.trip_id` where it
    # was, so nothing a cached dive read says about its trip has changed; a move changes
    # the `trip_uuid` each of those dives reports.
    if moved_dives:
        await invalidate_dive_caches(owner_id)

    return DeletedWithMovedDives(message="Trip deleted", moved_dives=moved_dives)
