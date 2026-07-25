from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastcrud.paginated import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_superuser, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import ForbiddenException, NotFoundException
from ...core.utils.cache import cache
from ...crud.crud_dives import crud_dives
from ...crud.crud_users import crud_users
from ...schemas.dive import DiveCreate, DiveCreateInternal, DiveRead, DiveUpdate
from ...schemas.parsed_dive import ParsedDiveSchema
from ...schemas.user import UserRead
from ...services.dive_parsers import DiveParseError, UnsupportedDiveFileError, parse_dive_file

router = APIRouter(tags=["dives"])


@router.post("/dive/parse-xml", response_model=ParsedDiveSchema)
async def parse_dive_xml(
        file: Annotated[UploadFile, File(description="Dive-computer export file (e.g. Suunto XML)")],
) -> ParsedDiveSchema:
    """Upload a dive-computer export file and receive the parsed dive data as JSON."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    content = await file.read()
    try:
        return parse_dive_file(file.filename, content)
    except UnsupportedDiveFileError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except DiveParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{username}/dive", response_model=DiveRead, status_code=201)
async def write_dive(
        request: Request,
        username: str,
        dive: DiveCreate,
        current_user: Annotated[dict, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveRead:
    db_user = await crud_users.get(
        db=db, username=username, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if db_user is None:
        raise NotFoundException("User not found")

    db_user = cast(UserRead, db_user)
    if current_user["id"] != db_user.id:
        raise ForbiddenException()

    dive_internal_dict = dive.model_dump()
    dive_internal_dict["user_id"] = db_user.id

    dive_internal = DiveCreateInternal(**dive_internal_dict)
    created_dive = await crud_dives.create(db=db, object=dive_internal)

    dive_read = await crud_dives.get(db=db, id=created_dive.id, schema_to_select=DiveRead)
    if dive_read is None:
        raise NotFoundException("Created dive not found")

    return cast(DiveRead, dive_read)


@router.get("/{username}/dives", response_model=PaginatedListResponse[DiveRead])
@cache(
    key_prefix="{username}_dives:page_{page}:items_per_page:{items_per_page}",
    resource_id_name="username",
    expiration=60,
)
async def read_dives(
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
    dives_data = await crud_dives.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        user_id=db_user.id,
        is_deleted=False,
    )

    response: dict[str, Any] = paginated_response(crud_data=dives_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/{username}/dive/{id}", response_model=DiveRead)
@cache(key_prefix="{username}_dive_cache", resource_id_name="id")
async def read_dive(
        request: Request, username: str, id: int, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> DiveRead:
    db_user = await crud_users.get(
        db=db, username=username, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if db_user is None:
        raise NotFoundException("User not found")

    db_user = cast(UserRead, db_user)
    db_dive = await crud_dives.get(
        db=db, id=id, user_id=db_user.id, is_deleted=False, schema_to_select=DiveRead
    )
    if db_dive is None:
        raise NotFoundException("Dive not found")

    return cast(DiveRead, db_dive)


@router.patch("/{username}/dive/{id}")
@cache("{username}_dive_cache", resource_id_name="id", pattern_to_invalidate_extra=["{username}_dives:*"])
async def patch_dive(
        request: Request,
        username: str,
        id: int,
        values: DiveUpdate,
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

    db_dive = await crud_dives.get(db=db, id=id, is_deleted=False, schema_to_select=DiveRead)
    if db_dive is None:
        raise NotFoundException("Dive not found")

    await crud_dives.update(db=db, object=values, id=id)
    return {"message": "Dive updated"}


@router.delete("/{username}/dive/{id}")
@cache("{username}_dive_cache", resource_id_name="id", to_invalidate_extra={"{username}_dives": "{username}"})
async def erase_dive(
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

    db_dive = await crud_dives.get(db=db, id=id, is_deleted=False, schema_to_select=DiveRead)
    if db_dive is None:
        raise NotFoundException("Dive not found")

    await crud_dives.delete(db=db, id=id)

    return {"message": "Dive deleted"}


@router.delete("/{username}/dive/{id}", dependencies=[Depends(get_current_superuser)])
@cache("{username}_dive_cache", resource_id_name="id", to_invalidate_extra={"{username}_dives": "{username}"})
async def erase_db_dive(
        request: Request, username: str, id: int, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> dict[str, str]:
    db_user = await crud_users.get(
        db=db, username=username, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if db_user is None:
        raise NotFoundException("User not found")

    db_dive = await crud_dives.get(db=db, id=id, is_deleted=False, schema_to_select=DiveRead)
    if db_dive is None:
        raise NotFoundException("Dive not found")

    await crud_dives.db_delete(db=db, id=id)
    return {"message": "Dive deleted from the database"}
