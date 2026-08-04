import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, ForbiddenException, NotFoundException
from ...core.utils.cache import cache, delete_keys_by_pattern
from ...crud.crud_dive_sites import crud_dive_sites, dive_site_name_exists
from ...schemas.dive_site import (
    DiveSiteCreate,
    DiveSiteCreateInternal,
    DiveSiteRead,
    DiveSiteReadInternal,
    DiveSiteUpdate,
)

router = APIRouter(tags=["dive_sites"])


def _dive_site_owner_id(db_dive_site: Any) -> int:
    return cast(int, db_dive_site["user_id"] if isinstance(db_dive_site, dict) else db_dive_site.user_id)


def _to_public_dive_site(
    db_dive_site: DiveSiteReadInternal | dict[str, Any], *, user_uuid: uuid_pkg.UUID
) -> DiveSiteRead:
    """Convert an internal dive site representation (integer FKs) into its public shape
    (owning user referenced by `uuid`)."""
    data = db_dive_site if isinstance(db_dive_site, dict) else db_dive_site.model_dump()
    return DiveSiteRead(**{k: v for k, v in data.items() if k not in ("id", "user_id")}, user_uuid=user_uuid)


@router.post("/dive-site", response_model=DiveSiteRead, status_code=201)
async def write_dive_site(
    request: Request,
    dive_site: DiveSiteCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteRead:
    if current_user["uuid"] != dive_site.user_uuid:
        raise ForbiddenException()

    if await dive_site_name_exists(
        db=db, user_id=current_user["id"], name=dive_site.name, location=dive_site.location
    ):
        raise DuplicateValueException("A dive site with this name already exists at this location")

    dive_site_internal_dict = dive_site.model_dump(exclude={"user_uuid"})
    dive_site_internal = DiveSiteCreateInternal(**dive_site_internal_dict, user_id=current_user["id"])
    created_dive_site = await crud_dive_sites.create(
        db=db, object=dive_site_internal, schema_to_select=DiveSiteReadInternal, return_as_model=True
    )
    await delete_keys_by_pattern(f"user_{current_user['id']}_dive_sites:*")

    dive_site_read = await crud_dive_sites.get(
        db=db, id=created_dive_site.id, schema_to_select=DiveSiteReadInternal, return_as_model=True
    )
    if dive_site_read is None:
        raise NotFoundException("Created dive site not found")

    return _to_public_dive_site(cast(DiveSiteReadInternal, dive_site_read), user_uuid=current_user["uuid"])


@cache(
    key_prefix="user_{user_id}_dive_sites:page_{page}:items_per_page:{items_per_page}",
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_dive_sites(
    request: Request,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    db: AsyncSession,
    page: int,
    items_per_page: int,
) -> dict:
    """Fetches (and caches) a user's paginated dive site list.

    This is only ever called after the caller's authorization has already been checked by
    `read_dive_sites` below - it must not be called directly from a route, since the `@cache`
    decorator serves cached responses without re-running any authorization logic.

    Keyed and filtered by the internal integer `user_id` (rather than the caller-supplied
    `user_uuid`) since that's already known to be the current user's id at this point.
    """
    dive_sites_data = await crud_dive_sites.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        user_id=user_id,
        is_deleted=False,
        sort_columns="name",
        sort_orders="asc",
    )
    dive_sites_data["data"] = [
        _to_public_dive_site(site, user_uuid=user_uuid).model_dump() for site in dive_sites_data["data"]
    ]

    response: dict[str, Any] = paginated_response(crud_data=dive_sites_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/dive-sites", response_model=PaginatedListResponse[DiveSiteRead])
async def read_dive_sites(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
) -> dict:
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    return await _cached_read_dive_sites(
        request, user_id=current_user["id"], user_uuid=user_uuid, db=db, page=page, items_per_page=items_per_page
    )


@cache(key_prefix="dive_site_cache", resource_id_name="dive_site_uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_dive_site(
    request: Request, dive_site_uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> DiveSiteRead:
    """Fetches (and caches) a single dive site by uuid, regardless of owner.

    Like `_cached_read_dive_sites`, this must only be called after authorization has already
    been checked, since `@cache` can serve a cached response without re-checking it.
    """
    db_dive_site = await crud_dive_sites.get(
        db=db, uuid=dive_site_uuid, is_deleted=False, schema_to_select=DiveSiteReadInternal, return_as_model=True
    )
    if db_dive_site is None:
        raise NotFoundException("Dive site not found")

    return _to_public_dive_site(cast(DiveSiteReadInternal, db_dive_site), user_uuid=owner_uuid)


@router.get("/dive-site/{dive_site_uuid}", response_model=DiveSiteRead)
async def read_dive_site(
    request: Request,
    dive_site_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteRead:
    db_dive_site = await crud_dive_sites.get(
        db=db, uuid=dive_site_uuid, is_deleted=False, schema_to_select=DiveSiteReadInternal, return_as_model=True
    )
    if db_dive_site is None:
        raise NotFoundException("Dive site not found")

    db_dive_site = cast(DiveSiteReadInternal, db_dive_site)
    if db_dive_site.user_id != current_user["id"]:
        raise ForbiddenException()

    return await _cached_read_dive_site(request, dive_site_uuid=dive_site_uuid, owner_uuid=current_user["uuid"], db=db)


@router.patch("/dive-site/{dive_site_uuid}")
@cache("dive_site_cache", resource_id_name="dive_site_uuid", resource_id_type=uuid_pkg.UUID)
async def patch_dive_site(
    request: Request,
    dive_site_uuid: uuid_pkg.UUID,
    values: DiveSiteUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_dive_site = await crud_dive_sites.get(
        db=db, uuid=dive_site_uuid, is_deleted=False, schema_to_select=DiveSiteReadInternal, return_as_model=True
    )
    if db_dive_site is None:
        raise NotFoundException("Dive site not found")

    db_dive_site = cast(DiveSiteReadInternal, db_dive_site)
    if db_dive_site.user_id != current_user["id"]:
        raise ForbiddenException()

    effective_name = values.name if values.name is not None else db_dive_site.name
    effective_location = values.location if "location" in values.model_fields_set else db_dive_site.location

    if (values.name is not None or "location" in values.model_fields_set) and await dive_site_name_exists(
        db=db,
        user_id=db_dive_site.user_id,
        name=effective_name,
        location=effective_location,
        exclude_id=db_dive_site.id,
    ):
        raise DuplicateValueException("A dive site with this name already exists at this location")

    update_data = values.model_dump(exclude_unset=True)
    if update_data:
        await crud_dive_sites.update(db=db, object=update_data, uuid=dive_site_uuid)
        await delete_keys_by_pattern(f"user_{db_dive_site.user_id}_dive_sites:*")

    return {"message": "Dive site updated"}


@router.delete("/dive-site/{dive_site_uuid}")
@cache("dive_site_cache", resource_id_name="dive_site_uuid", resource_id_type=uuid_pkg.UUID)
async def erase_dive_site(
    request: Request,
    dive_site_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_dive_site = await crud_dive_sites.get(db=db, uuid=dive_site_uuid, schema_to_select=DiveSiteReadInternal)
    if db_dive_site is None:
        raise NotFoundException("Dive site not found")

    owner_id = _dive_site_owner_id(db_dive_site)
    if owner_id != current_user["id"]:
        raise ForbiddenException()

    await crud_dive_sites.delete(db=db, uuid=dive_site_uuid)
    await delete_keys_by_pattern(f"user_{owner_id}_dive_sites:*")

    return {"message": "Dive site deleted"}
