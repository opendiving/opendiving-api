from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, ForbiddenException, NotFoundException
from ...core.utils.cache import cache, delete_keys_by_pattern
from ...crud.crud_trips import crud_trips, trip_name_exists
from ...schemas.trip import TripCreate, TripCreateInternal, TripRead, TripUpdate

router = APIRouter(tags=["trips"])


def _trip_owner_id(db_trip: Any) -> int:
    return cast(int, db_trip["user_id"] if isinstance(db_trip, dict) else db_trip.user_id)


@router.post("/trip", response_model=TripRead, status_code=201)
async def write_trip(
    request: Request,
    trip: TripCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> TripRead:
    if current_user["id"] != trip.user_id:
        raise ForbiddenException()

    if await trip_name_exists(db=db, user_id=trip.user_id, name=trip.name):
        raise DuplicateValueException("A trip with this name already exists")

    trip_internal = TripCreateInternal(**trip.model_dump())
    created_trip = await crud_trips.create(db=db, object=trip_internal, schema_to_select=TripRead, return_as_model=True)
    await delete_keys_by_pattern(f"user_{trip.user_id}_trips:*")

    trip_read = await crud_trips.get(db=db, id=created_trip.id, schema_to_select=TripRead, return_as_model=True)
    if trip_read is None:
        raise NotFoundException("Created trip not found")

    return cast(TripRead, trip_read)


@cache(
    key_prefix="user_{user_id}_trips:page_{page}:items_per_page:{items_per_page}",
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_trips(
    request: Request,
    user_id: int,
    db: AsyncSession,
    page: int,
    items_per_page: int,
) -> dict:
    """Fetches (and caches) a user's paginated trip list.

    This is only ever called after the caller's authorization has already been checked by
    `read_trips` below - it must not be called directly from a route, since the `@cache`
    decorator serves cached responses without re-running any authorization logic.
    """
    trips_data = await crud_trips.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        user_id=user_id,
        is_deleted=False,
        sort_columns="start_date",
        sort_orders="desc",
    )

    response: dict[str, Any] = paginated_response(crud_data=trips_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/trips", response_model=PaginatedListResponse[TripRead])
async def read_trips(
    request: Request,
    user_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
) -> dict:
    if current_user["id"] != user_id:
        raise ForbiddenException()

    return await _cached_read_trips(request, user_id=user_id, db=db, page=page, items_per_page=items_per_page)


@cache(key_prefix="trip_cache", resource_id_name="id")
async def _cached_read_trip(request: Request, id: int, db: AsyncSession) -> TripRead:
    """Fetches (and caches) a single trip by id, regardless of owner.

    Like `_cached_read_trips`, this must only be called after authorization has already
    been checked, since `@cache` can serve a cached response without re-checking it.
    """
    db_trip = await crud_trips.get(db=db, id=id, is_deleted=False, schema_to_select=TripRead, return_as_model=True)
    if db_trip is None:
        raise NotFoundException("Trip not found")

    return cast(TripRead, db_trip)


@router.get("/trip/{id}", response_model=TripRead)
async def read_trip(
    request: Request,
    id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> TripRead:
    db_trip = await crud_trips.get(db=db, id=id, is_deleted=False, schema_to_select=TripRead, return_as_model=True)
    if db_trip is None:
        raise NotFoundException("Trip not found")

    db_trip = cast(TripRead, db_trip)
    if db_trip.user_id != current_user["id"]:
        raise ForbiddenException()

    return await _cached_read_trip(request, id=id, db=db)


@router.patch("/trip/{id}")
@cache("trip_cache", resource_id_name="id")
async def patch_trip(
    request: Request,
    id: int,
    values: TripUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_trip = await crud_trips.get(db=db, id=id, is_deleted=False, schema_to_select=TripRead, return_as_model=True)
    if db_trip is None:
        raise NotFoundException("Trip not found")

    db_trip = cast(TripRead, db_trip)
    if db_trip.user_id != current_user["id"]:
        raise ForbiddenException()

    if values.name is not None and await trip_name_exists(
        db=db, user_id=db_trip.user_id, name=values.name, exclude_id=id
    ):
        raise DuplicateValueException("A trip with this name already exists")

    update_data = values.model_dump(exclude_unset=True)
    if update_data:
        await crud_trips.update(db=db, object=update_data, id=id)
        await delete_keys_by_pattern(f"user_{db_trip.user_id}_trips:*")

    return {"message": "Trip updated"}


@router.delete("/trip/{id}")
@cache("trip_cache", resource_id_name="id")
async def erase_trip(
    request: Request,
    id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_trip = await crud_trips.get(db=db, id=id, is_deleted=False, schema_to_select=TripRead)
    if db_trip is None:
        raise NotFoundException("Trip not found")

    owner_id = _trip_owner_id(db_trip)
    if owner_id != current_user["id"]:
        raise ForbiddenException()

    await crud_trips.delete(db=db, id=id)
    await delete_keys_by_pattern(f"user_{owner_id}_trips:*")

    return {"message": "Trip deleted"}
