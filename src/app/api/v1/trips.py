import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, ForbiddenException, NotFoundException
from ...core.utils.cache import cache
from ...core.utils.owned_resource_cache import OwnedResourceCache
from ...crud.crud_trips import crud_trips, trip_name_exists
from ...schemas.trip import TripCreate, TripCreateInternal, TripRead, TripReadInternal, TripUpdate

router = APIRouter(tags=["trips"])


def _trip_owner_id(db_trip: Any) -> int:
    return cast(int, db_trip["user_id"] if isinstance(db_trip, dict) else db_trip.user_id)


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

# `GET /trips` feeds both the list page and the dive form's picker, so the page size is a
# client choice - but an unbounded one lets a single request pull the whole table.
MAX_TRIPS_PER_PAGE = 100


@router.post("/trip", response_model=TripRead, status_code=201)
async def write_trip(
    request: Request,
    trip: TripCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> TripRead:
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
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    return await _trip_cache.read_list(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=max(page, 1),
        items_per_page=min(max(items_per_page, 1), MAX_TRIPS_PER_PAGE),
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
    db_trip = await crud_trips.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=TripReadInternal, return_as_model=True
    )
    if db_trip is None:
        raise NotFoundException("Trip not found")

    db_trip = cast(TripReadInternal, db_trip)
    if db_trip.user_id != current_user["id"]:
        raise ForbiddenException()

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
    db_trip = await crud_trips.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=TripReadInternal, return_as_model=True
    )
    if db_trip is None:
        raise NotFoundException("Trip not found")

    db_trip = cast(TripReadInternal, db_trip)
    if db_trip.user_id != current_user["id"]:
        raise ForbiddenException()

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
    db_trip = await crud_trips.get(db=db, uuid=uuid, is_deleted=False, schema_to_select=TripReadInternal)
    if db_trip is None:
        raise NotFoundException("Trip not found")

    owner_id = _trip_owner_id(db_trip)
    if owner_id != current_user["id"]:
        raise ForbiddenException()

    await crud_trips.delete(db=db, uuid=uuid)
    await _trip_cache.invalidate_list(owner_id)

    return {"message": "Trip deleted"}
