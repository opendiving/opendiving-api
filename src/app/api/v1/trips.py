import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, ForbiddenException, NotFoundException
from ...core.utils.cache import cache
from ...core.utils.owned_resource_cache import OwnedResourceCache
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_trips import crud_trips, trip_name_exists
from ...schemas.trip import TripCreate, TripCreateInternal, TripRead, TripReadInternal, TripUpdate

router = APIRouter(tags=["trips"])


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


def _to_public_trip(db_trip: TripReadInternal | dict[str, Any], *, user_uuid: uuid_pkg.UUID) -> TripRead:
    """Convert an internal trip representation (integer FKs) into its public shape
    (owning user referenced by `uuid`)."""
    data = db_trip if isinstance(db_trip, dict) else db_trip.model_dump()
    return TripRead(**{k: v for k, v in data.items() if k not in ("id", "user_id")}, user_uuid=user_uuid)


_trip_cache: OwnedResourceCache[TripReadInternal, TripRead] = OwnedResourceCache(
    resource_name="trips",
    resource_label="Trip",
    item_cache_prefix="trip_cache",
    crud=crud_trips,
    schema_to_select=TripReadInternal,
    to_public=lambda db_trip, user_uuid: _to_public_trip(db_trip, user_uuid=user_uuid),
    sort_columns="start_date",
    sort_orders="desc",
    # Same reason as dive sites: the dive form's picker narrows server-side as you type
    # rather than shipping the user's whole trip list to the browser. Location is searched
    # alongside the name because a trip is as often remembered by where it went as by what
    # it was called - see DECISIONS.md.
    search_columns=("name", "location"),
)


@router.post("/trip", response_model=TripRead, status_code=201)
async def write_trip(
    request: Request,
    trip: TripCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> TripRead:
    """Create a trip for the authenticated user.

    `user_uuid` in the body must be the caller's own: a mismatch is a 403 rather than a
    silent reassignment to the caller. Trip names are unique per user, so reusing one
    that already exists is a 422.
    """
    if current_user["uuid"] != trip.user_uuid:
        raise ForbiddenException()

    if await trip_name_exists(db=db, user_id=current_user["id"], name=trip.name):
        raise DuplicateValueException("A trip with this name already exists")

    trip_internal_dict = trip.model_dump(exclude={"user_uuid"})
    trip_internal = TripCreateInternal(**trip_internal_dict, user_id=current_user["id"])
    created_trip = await crud_trips.create(
        db=db, object=trip_internal, schema_to_select=TripReadInternal, return_as_model=True
    )
    await _trip_cache.invalidate_list(current_user["id"])

    trip_read = await crud_trips.get(db=db, id=created_trip.id, schema_to_select=TripReadInternal, return_as_model=True)
    if trip_read is None:
        raise NotFoundException("Created trip not found")

    return _to_public_trip(cast(TripReadInternal, trip_read), user_uuid=current_user["uuid"])


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
        Query(max_length=255, description="Case-insensitive substring match on name or location"),
    ] = None,
) -> dict:
    """List the caller's trips, most recent start date first.

    `user_uuid` must be the caller's own (403 otherwise) - this endpoint cannot be used
    to read another user's trips. `search` matches a case-insensitive substring against
    name and location. Out-of-range pagination is clamped rather than rejected, so
    `items_per_page` above the ceiling returns the ceiling instead of a 422.
    """
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    page, items_per_page = clamp_pagination(page, items_per_page)

    return await _trip_cache.read_list(
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


@router.get("/trip/{uuid}", response_model=TripRead)
async def read_trip(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> TripRead:
    """Return a single trip by its public uuid.

    404 when no such trip exists - and the same 404 when it belongs to another user, so
    someone else's uuid stays unprobeable.
    """
    # Authorize before the cached read: `@cache` replays a hit without re-checking.
    await _get_owned_trip(db, uuid, current_user)

    return await _trip_cache.read_item(request, uuid=uuid, owner_uuid=current_user["uuid"], db=db)


@router.patch("/trip/{uuid}")
@cache("trip_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def patch_trip(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: TripUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update a trip; omitted fields are left untouched.

    404 unless the caller owns it, exactly as for a trip that doesn't exist. Renaming to
    a name the caller already has on another trip is a 422.
    """
    db_trip = await _get_owned_trip(db, uuid, current_user)

    if values.name is not None and await trip_name_exists(
        db=db, user_id=db_trip.user_id, name=values.name, exclude_id=db_trip.id
    ):
        raise DuplicateValueException("A trip with this name already exists")

    update_data = values.model_dump(exclude_unset=True)
    if update_data:
        await crud_trips.update(db=db, object=update_data, uuid=uuid)
        await _trip_cache.invalidate_list(db_trip.user_id)

    return {"message": "Trip updated"}


@router.delete("/trip/{uuid}")
@cache("trip_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def erase_trip(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Soft-delete a trip.

    404 unless the caller owns it, exactly as for a trip that doesn't exist. The row is
    flagged rather than removed, so dives that referenced this trip keep their `trip_id` -
    the trip simply stops appearing in reads.
    """
    owner_id = (await _get_owned_trip(db, uuid, current_user)).user_id

    await crud_trips.delete(db=db, uuid=uuid)
    await _trip_cache.invalidate_list(owner_id)

    return {"message": "Trip deleted"}
