import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, ForbiddenException, NotFoundException
from ...core.security import blacklist_token, get_password_hash, oauth2_scheme
from ...crud.crud_users import crud_users
from ...schemas.user import UserCreate, UserCreateInternal, UserRead, UserReadInternal, UserUpdate

router = APIRouter(tags=["users"])


@router.post("/user", response_model=UserRead, status_code=201)
async def write_user(
    request: Request, user: UserCreate, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> UserRead:
    email_row = await crud_users.exists(db=db, email=user.email)
    if email_row:
        raise DuplicateValueException("Email is already registered")

    username_row = await crud_users.exists(db=db, username=user.username)
    if username_row:
        raise DuplicateValueException("Username not available")

    user_internal_dict = user.model_dump()
    user_internal_dict["hashed_password"] = get_password_hash(password=user_internal_dict["password"])
    del user_internal_dict["password"]

    user_internal = UserCreateInternal(**user_internal_dict)
    created_user = await crud_users.create(
        db=db, object=user_internal, schema_to_select=UserReadInternal, return_as_model=True
    )

    user_read = await crud_users.get(db=db, id=created_user.id, schema_to_select=UserRead, return_as_model=True)
    if user_read is None:
        raise NotFoundException("Created user not found")

    return cast(UserRead, user_read)


@router.get("/users", response_model=PaginatedListResponse[UserRead], dependencies=[Depends(get_current_user)])
async def read_users(
    request: Request, db: Annotated[AsyncSession, Depends(async_get_db)], page: int = 1, items_per_page: int = 10
) -> dict:
    users_data = await crud_users.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        is_deleted=False,
    )

    response: dict[str, Any] = paginated_response(crud_data=users_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/user/me", response_model=UserRead)
async def read_users_me(request: Request, current_user: Annotated[dict, Depends(get_current_user)]) -> dict:
    return current_user


@router.get("/user/{user_uuid}", response_model=UserRead, dependencies=[Depends(get_current_user)])
async def read_user(
    request: Request, user_uuid: uuid_pkg.UUID, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> UserRead:
    db_user = await crud_users.get(
        db=db, uuid=user_uuid, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if db_user is None:
        raise NotFoundException("User not found")

    return cast(UserRead, db_user)


@router.patch("/user/{user_uuid}")
async def patch_user(
    request: Request,
    values: UserUpdate,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_user = await crud_users.get(db=db, uuid=user_uuid)
    if db_user is None:
        raise NotFoundException("User not found")

    if isinstance(db_user, dict):
        db_username = db_user["username"]
        db_email = db_user["email"]
    else:
        db_username = db_user.username
        db_email = db_user.email

    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    if values.email is not None and values.email != db_email:
        if await crud_users.exists(db=db, email=values.email):
            raise DuplicateValueException("Email is already registered")

    if values.username is not None and values.username != db_username:
        if await crud_users.exists(db=db, username=values.username):
            raise DuplicateValueException("Username not available")

    await crud_users.update(db=db, object=values, uuid=user_uuid)
    return {"message": "User updated"}


@router.delete("/user/{user_uuid}")
async def erase_user(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    token: str = Depends(oauth2_scheme),
) -> dict[str, str]:
    db_user = await crud_users.get(db=db, uuid=user_uuid, schema_to_select=UserReadInternal)
    if not db_user:
        raise NotFoundException("User not found")

    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    await crud_users.delete(db=db, uuid=user_uuid)
    await blacklist_token(token=token, db=db)
    return {"message": "User deleted"}
