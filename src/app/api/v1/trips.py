from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastcrud.paginated import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import ForbiddenException, NotFoundException
from ...crud.crud_trips import crud_trips
from ...crud.crud_users import crud_users
from ...schemas.trip import TripCreate, TripCreateInternal, TripRead, TripUpdate
from ...schemas.user import UserRead

router = APIRouter(tags=["trips"])


@router.post("/{username}/trip", response_model=TripRead, status_code=201)
async def write_trip(
        request: Request,
        username: str,
        trip: TripCreate,
        current_user: Annotated[dict, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(async_get_db)],
) -> TripRead:
    db_user = await crud_users.get(
        db=db, username=username, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if db_user is None:
        raise NotFoundException("User not found")

    db_user = cast(UserRead, db_user)
    if current_user["id"] != db_user.id:
        raise ForbiddenException()

    trip_internal = TripCreateInternal(name=trip.name, user_id=db_user.id)
    created_trip = await crud_trips.create(db=db, object=trip_internal)

    trip_read = await crud_trips.get(db=db, id=created_trip.id, schema_to_select=TripRead)
    if trip_read is None:
        raise NotFoundException("Created trip not found")

    return cast(TripRead, trip_read)


@router.get("/{username}/trips", response_model=PaginatedListResponse[TripRead])
async def read_trips(
        request: Request,
        username: str,
        db: Annotated[AsyncSession, Depends(async_get_db)],
        page: int = 1,
        items_per_page: int = 10,
) -> dict:
    db_user = await crud_users.get(
        db=db, username=username, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if not db_user:
        raise NotFoundException("User not found")

    db_user = cast(UserRead, db_user)
    trips_data = await crud_trips.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        user_id=db_user.id,
        is_deleted=False,
        sort_columns="name",
        sort_orders="asc",
    )

    response: dict[str, Any] = paginated_response(crud_data=trips_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/{username}/trip/{id}", response_model=TripRead)
async def read_trip(
        request: Request, username: str, id: int, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> TripRead:
    db_user = await crud_users.get(
        db=db, username=username, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if db_user is None:
        raise NotFoundException("User not found")

    db_user = cast(UserRead, db_user)
    db_trip = await crud_trips.get(
        db=db, id=id, user_id=db_user.id, is_deleted=False, schema_to_select=TripRead
    )
    if db_trip is None:
        raise NotFoundException("Trip not found")

    return cast(TripRead, db_trip)


@router.patch("/{username}/trip/{id}")
async def patch_trip(
        request: Request,
        username: str,
        id: int,
        values: TripUpdate,
        current_user: Annotated[dict, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_user = await crud_users.get(
        db=db, username=username, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if db_user is None:
        raise NotFoundException("User not found")

    db_user = cast(UserRead, db_user)
    if current_user["id"] != db_user.id:
        raise ForbiddenException()

    db_trip = await crud_trips.get(db=db, id=id, user_id=db_user.id, is_deleted=False, schema_to_select=TripRead)
    if db_trip is None:
        raise NotFoundException("Trip not found")

    update_data = values.model_dump(exclude_unset=True)
    if update_data:
        await crud_trips.update(db=db, object=update_data, id=id)

    return {"message": "Trip updated"}


@router.delete("/{username}/trip/{id}")
async def erase_trip(
        request: Request,
        username: str,
        id: int,
        current_user: Annotated[dict, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_user = await crud_users.get(
        db=db, username=username, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if db_user is None:
        raise NotFoundException("User not found")

    db_user = cast(UserRead, db_user)
    if current_user["id"] != db_user.id:
        raise ForbiddenException()

    db_trip = await crud_trips.get(db=db, id=id, user_id=db_user.id, is_deleted=False, schema_to_select=TripRead)
    if db_trip is None:
        raise NotFoundException("Trip not found")

    await crud_trips.delete(db=db, id=id)

    return {"message": "Trip deleted"}
