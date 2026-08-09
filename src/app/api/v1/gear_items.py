import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, ForbiddenException, NotFoundException
from ...core.utils.cache import cache
from ...crud.crud_gear_items import crud_gear_items, gear_item_name_exists
from ...schemas.gear_item import (
    GearItemCreate,
    GearItemCreateInternal,
    GearItemRead,
    GearItemReadInternal,
    GearItemUpdate,
)
from ...services.cache_invalidation import invalidate_dive_caches, invalidate_gear_caches

router = APIRouter(tags=["gear"])


def _gear_item_owner_id(db_gear_item: Any) -> int:
    return cast(int, db_gear_item["user_id"] if isinstance(db_gear_item, dict) else db_gear_item.user_id)


def _to_public_gear_item(
    db_gear_item: GearItemReadInternal | dict[str, Any], *, user_uuid: uuid_pkg.UUID
) -> GearItemRead:
    """Convert an internal gear item representation (integer FKs) into its public shape
    (owning user referenced by `uuid`)."""
    data = db_gear_item if isinstance(db_gear_item, dict) else db_gear_item.model_dump()
    return GearItemRead(**{k: v for k, v in data.items() if k not in ("id", "user_id")}, user_uuid=user_uuid)


@router.post("/gear-item", response_model=GearItemRead, status_code=201)
async def write_gear_item(
    request: Request,
    gear_item: GearItemCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> GearItemRead:
    if current_user["uuid"] != gear_item.user_uuid:
        raise ForbiddenException()

    if await gear_item_name_exists(db=db, user_id=current_user["id"], name=gear_item.name, brand=gear_item.brand):
        raise DuplicateValueException("A gear item with this brand and name already exists")

    gear_item_internal = GearItemCreateInternal(
        **gear_item.model_dump(exclude={"user_uuid"}), user_id=current_user["id"]
    )
    created_gear_item = await crud_gear_items.create(
        db=db, object=gear_item_internal, schema_to_select=GearItemReadInternal, return_as_model=True
    )
    await invalidate_gear_caches(current_user["id"])

    gear_item_read = await crud_gear_items.get(
        db=db, id=created_gear_item.id, schema_to_select=GearItemReadInternal, return_as_model=True
    )
    if gear_item_read is None:
        raise NotFoundException("Created gear item not found")

    return _to_public_gear_item(cast(GearItemReadInternal, gear_item_read), user_uuid=current_user["uuid"])


@cache(
    key_prefix=("user_{user_id}_gear_items:page_{page}:items_per_page:{items_per_page}:archived_{include_archived}"),
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_gear_items(
    request: Request,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    db: AsyncSession,
    page: int,
    items_per_page: int,
    include_archived: bool,
) -> dict:
    """Fetches (and caches) a user's paginated gear item list.

    Like the other cached read helpers, this must only ever be called after the caller's
    authorization has been checked by the route - `@cache` serves cached responses
    without re-running any authorization logic. `include_archived` is part of the cache
    key so the picker's (non-archived) view and the management page's (full) view can't
    serve each other's results.
    """
    filters: dict[str, Any] = {"user_id": user_id, "is_deleted": False}
    if not include_archived:
        filters["is_archived"] = False

    data = await crud_gear_items.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        sort_columns="name",
        sort_orders="asc",
        **filters,
    )
    data["data"] = [_to_public_gear_item(item, user_uuid=user_uuid).model_dump() for item in data["data"]]

    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get("/gear-items", response_model=PaginatedListResponse[GearItemRead])
async def read_gear_items(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    include_archived: bool = False,
) -> dict:
    """List a user's gear. Archived items are excluded unless `include_archived=true`,
    so the dive form's picker only ever offers gear that's still in service.
    """
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    return await _cached_read_gear_items(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=page,
        items_per_page=items_per_page,
        include_archived=include_archived,
    )


@cache(key_prefix="user_{user_id}_gear_item", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_gear_item(
    request: Request, user_id: int, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> GearItemRead:
    """Fetches (and caches) a single gear item by uuid. Authorization is checked by the
    route before this is ever reached - see `_cached_read_gear_items`.
    """
    db_gear_item = await crud_gear_items.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=GearItemReadInternal, return_as_model=True
    )
    if db_gear_item is None:
        raise NotFoundException("Gear item not found")

    return _to_public_gear_item(cast(GearItemReadInternal, db_gear_item), user_uuid=owner_uuid)


@router.get("/gear-item/{uuid}", response_model=GearItemRead)
async def read_gear_item(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> GearItemRead:
    db_gear_item = await crud_gear_items.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=GearItemReadInternal, return_as_model=True
    )
    if db_gear_item is None:
        raise NotFoundException("Gear item not found")

    db_gear_item = cast(GearItemReadInternal, db_gear_item)
    if db_gear_item.user_id != current_user["id"]:
        raise ForbiddenException()

    return await _cached_read_gear_item(
        request, user_id=current_user["id"], uuid=uuid, owner_uuid=current_user["uuid"], db=db
    )


@router.patch("/gear-item/{uuid}")
async def patch_gear_item(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: GearItemUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partial update, including archiving/unarchiving via `is_archived` - `archived_at`
    is derived here rather than accepted from the caller.
    """
    db_gear_item = await crud_gear_items.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=GearItemReadInternal, return_as_model=True
    )
    if db_gear_item is None:
        raise NotFoundException("Gear item not found")

    db_gear_item = cast(GearItemReadInternal, db_gear_item)
    if db_gear_item.user_id != current_user["id"]:
        raise ForbiddenException()

    effective_name = values.name if values.name is not None else db_gear_item.name
    effective_brand = values.brand if "brand" in values.model_fields_set else db_gear_item.brand

    if (values.name is not None or "brand" in values.model_fields_set) and await gear_item_name_exists(
        db=db,
        user_id=db_gear_item.user_id,
        name=effective_name,
        brand=effective_brand,
        exclude_id=db_gear_item.id,
    ):
        raise DuplicateValueException("A gear item with this brand and name already exists")

    update_data = values.model_dump(exclude_unset=True)
    if values.is_archived is not None and values.is_archived != db_gear_item.is_archived:
        update_data["archived_at"] = datetime.now(UTC) if values.is_archived else None

    if update_data:
        await crud_gear_items.update(db=db, object=update_data, uuid=uuid)
        await invalidate_gear_caches(db_gear_item.user_id)
        # Dive reads embed this item's name/brand/type/rented/is_archived, so a rename
        # (or an archive) makes every cached dive that uses it stale.
        await invalidate_dive_caches(db_gear_item.user_id)

    return {"message": "Gear item updated"}


@router.delete("/gear-item/{uuid}")
async def erase_gear_item(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Soft-deletes a gear item. Dives and gear sets that already reference it keep their
    join rows (and so keep showing it), matching how a soft-deleted dive site behaves -
    archiving is the non-destructive way to retire gear you still want in your log.
    """
    db_gear_item = await crud_gear_items.get(db=db, uuid=uuid, schema_to_select=GearItemReadInternal)
    if db_gear_item is None:
        raise NotFoundException("Gear item not found")

    owner_id = _gear_item_owner_id(db_gear_item)
    if owner_id != current_user["id"]:
        raise ForbiddenException()

    await crud_gear_items.delete(db=db, uuid=uuid)
    await invalidate_gear_caches(owner_id)
    # Soft-deleted gear stays on the dives that used it, so their cached reads still
    # reference it - drop them rather than reasoning about which fields "look" deleted.
    await invalidate_dive_caches(owner_id)

    return {"message": "Gear item deleted"}
