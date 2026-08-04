import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, ForbiddenException, NotFoundException
from ...core.utils.cache import cache, delete_keys_by_pattern
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
    await delete_keys_by_pattern(f"user_{current_user['id']}_trips:*")

    trip_read = await crud_trips.get(db=db, id=created_trip.id, schema_to_select=TripReadInternal, return_as_model=True)
    if trip_read is None:
        raise NotFoundException("Created trip not found")

    return _to_public_trip(cast(TripReadInternal, trip_read), user_uuid=current_user["uuid"])


@cache(
    key_prefix="user_{user_id}_trips:page_{page}:items_per_page:{items_per_page}",
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
) -> dict:
    """Fetches (and caches) a user's paginated trip list.

    This is only ever called after the caller's authorization has already been checked by
    `read_trips` below - it must not be called directly from a route, since the `@cache`
    decorator serves cached responses without re-running any authorization logic.

    Keyed and filtered by the internal integer `user_id` (rather than the caller-supplied
    `user_uuid`) since that's already known to be the current user's id at this point.
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
    trips_data["data"] = [
        _to_public_trip(trip, user_uuid=user_uuid).model_dump() for trip in trips_data["data"]
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
) -> dict:
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    return await _cached_read_trips(
        request, user_id=current_user["id"], user_uuid=user_uuid, db=db, page=page, items_per_page=items_per_page
    )


@cache(key_prefix="trip_cache", resource_id_name="trip_uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_trip(
    request: Request, trip_uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> TripRead:
    """Fetches (and caches) a single trip by uuid, regardless of owner.

    Like `_cached_read_trips`, this must only be called after authorization has already
    been checked, since `@cache` can serve a cached response without re-checking it.
    """
    db_trip = await crud_trips.get(
        db=db, uuid=trip_uuid, is_deleted=False, schema_to_select=TripReadInternal, return_as_model=True
    )
    if db_trip is None:
        raise NotFoundException("Trip not found")

    return _to_public_trip(cast(TripReadInternal, db_trip), user_uuid=owner_uuid)


@router.get("/trip/{trip_uuid}", response_model=TripRead)
async def read_trip(
    request: Request,
    trip_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> TripRead:
    db_trip = await crud_trips.get(
        db=db, uuid=trip_uuid, is_deleted=False, schema_to_select=TripReadInternal, return_as_model=True
    )
    if db_trip is None:
        raise NotFoundException("Trip not found")

    db_trip = cast(TripReadInternal, db_trip)
    if db_trip.user_id != current_user["id"]:
        raise ForbiddenException()

    return await _cached_read_trip(request, trip_uuid=trip_uuid, owner_uuid=current_user["uuid"], db=db)


@router.patch("/trip/{trip_uuid}")
@cache("trip_cache", resource_id_name="trip_uuid", resource_id_type=uuid_pkg.UUID)
async def patch_trip(
    request: Request,
    trip_uuid: uuid_pkg.UUID,
    values: TripUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_trip = await crud_trips.get(
        db=db, uuid=trip_uuid, is_deleted=False, schema_to_select=TripReadInternal, return_as_model=True
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
        await crud_trips.update(db=db, object=update_data, uuid=trip_uuid)
        await delete_keys_by_pattern(f"user_{db_trip.user_id}_trips:*")

    return {"message": "Trip updated"}


@router.delete("/trip/{trip_uuid}")
@cache("trip_cache", resource_id_name="trip_uuid", resource_id_type=uuid_pkg.UUID)
async def erase_trip(
    request: Request,
    trip_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_trip = await crud_trips.get(db=db, uuid=trip_uuid, is_deleted=False, schema_to_select=TripReadInternal)
    if db_trip is None:
        raise NotFoundException("Trip not found")

    owner_id = _trip_owner_id(db_trip)
    if owner_id != current_user["id"]:
        raise ForbiddenException()

    await crud_trips.delete(db=db, uuid=trip_uuid)
    await delete_keys_by_pattern(f"user_{owner_id}_trips:*")

    return {"message": "Trip deleted"}
