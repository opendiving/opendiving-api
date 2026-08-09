import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, ForbiddenException, NotFoundException
from ...core.utils.cache import cache
from ...core.utils.owned_resource_cache import OwnedResourceCache
from ...crud.crud_dive_sites import crud_dive_sites, dive_site_name_exists
from ...schemas.dive_site import (
    DiveSiteCreate,
    DiveSiteCreateInternal,
    DiveSiteRead,
    DiveSiteReadInternal,
    DiveSiteUpdate,
)
from ...services.cache_invalidation import invalidate_dive_caches

router = APIRouter(tags=["dive-sites"])


def _dive_site_owner_id(db_dive_site: Any) -> int:
    return cast(int, db_dive_site["user_id"] if isinstance(db_dive_site, dict) else db_dive_site.user_id)


def _to_public_dive_site(
    db_dive_site: DiveSiteReadInternal | dict[str, Any], *, user_uuid: uuid_pkg.UUID
) -> DiveSiteRead:
    """Convert an internal dive site representation (integer FKs) into its public shape
    (owning user referenced by `uuid`)."""
    data = db_dive_site if isinstance(db_dive_site, dict) else db_dive_site.model_dump()
    return DiveSiteRead(**{k: v for k, v in data.items() if k not in ("id", "user_id")}, user_uuid=user_uuid)


_dive_site_cache: OwnedResourceCache[DiveSiteReadInternal, DiveSiteRead] = OwnedResourceCache(
    resource_name="dive_sites",
    resource_label="Dive site",
    item_cache_prefix="dive_site_cache",
    crud=crud_dive_sites,
    schema_to_select=DiveSiteReadInternal,
    to_public=lambda db_dive_site, user_uuid: _to_public_dive_site(db_dive_site, user_uuid=user_uuid),
    sort_columns="name",
    sort_orders="asc",
)


@router.post("/dive-site", response_model=DiveSiteRead, status_code=201)
async def write_dive_site(
    request: Request,
    dive_site: DiveSiteCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteRead:
    if current_user["uuid"] != dive_site.user_uuid:
        raise ForbiddenException()

    if await dive_site_name_exists(db=db, user_id=current_user["id"], name=dive_site.name, location=dive_site.location):
        raise DuplicateValueException("A dive site with this name already exists at this location")

    dive_site_internal_dict = dive_site.model_dump(exclude={"user_uuid"})
    dive_site_internal = DiveSiteCreateInternal(**dive_site_internal_dict, user_id=current_user["id"])
    created_dive_site = await crud_dive_sites.create(
        db=db, object=dive_site_internal, schema_to_select=DiveSiteReadInternal, return_as_model=True
    )
    await _dive_site_cache.invalidate_list(current_user["id"])

    dive_site_read = await crud_dive_sites.get(
        db=db, id=created_dive_site.id, schema_to_select=DiveSiteReadInternal, return_as_model=True
    )
    if dive_site_read is None:
        raise NotFoundException("Created dive site not found")

    return _to_public_dive_site(cast(DiveSiteReadInternal, dive_site_read), user_uuid=current_user["uuid"])


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

    return await _dive_site_cache.read_list(
        request, user_id=current_user["id"], user_uuid=user_uuid, db=db, page=page, items_per_page=items_per_page
    )


@router.get("/dive-site/{uuid}", response_model=DiveSiteRead)
async def read_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteRead:
    db_dive_site = await crud_dive_sites.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=DiveSiteReadInternal, return_as_model=True
    )
    if db_dive_site is None:
        raise NotFoundException("Dive site not found")

    db_dive_site = cast(DiveSiteReadInternal, db_dive_site)
    if db_dive_site.user_id != current_user["id"]:
        raise ForbiddenException()

    return await _dive_site_cache.read_item(request, uuid=uuid, owner_uuid=current_user["uuid"], db=db)


@router.patch("/dive-site/{uuid}")
@cache("dive_site_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def patch_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: DiveSiteUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_dive_site = await crud_dive_sites.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=DiveSiteReadInternal, return_as_model=True
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
        await crud_dive_sites.update(db=db, object=update_data, uuid=uuid)
        await _dive_site_cache.invalidate_list(db_dive_site.user_id)
        # Dive reads embed this site's name/location, so a rename makes every cached
        # dive logged here stale - the bug that used to be documented as a known
        # limitation, fixable now that the single-dive cache key is user-scoped.
        await invalidate_dive_caches(db_dive_site.user_id)

    return {"message": "Dive site updated"}


@router.delete("/dive-site/{uuid}")
@cache("dive_site_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def erase_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_dive_site = await crud_dive_sites.get(db=db, uuid=uuid, schema_to_select=DiveSiteReadInternal)
    if db_dive_site is None:
        raise NotFoundException("Dive site not found")

    owner_id = _dive_site_owner_id(db_dive_site)
    if owner_id != current_user["id"]:
        raise ForbiddenException()

    await crud_dive_sites.delete(db=db, uuid=uuid)
    await _dive_site_cache.invalidate_list(owner_id)
    # Soft-deleted sites stay on the dives logged at them, so drop those reads too.
    await invalidate_dive_caches(owner_id)

    return {"message": "Dive site deleted"}
