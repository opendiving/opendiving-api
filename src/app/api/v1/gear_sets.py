import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    DuplicateValueException,
    ForbiddenException,
    NotFoundException,
    UnprocessableEntityException,
)
from ...core.utils.cache import cache
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_gear_items import resolve_gear_item_ids_for_user
from ...crud.crud_gear_set_items import (
    get_gear_items_for_set,
    get_gear_items_for_sets,
    replace_gear_items_for_set,
)
from ...crud.crud_gear_sets import crud_gear_sets, gear_set_name_exists
from ...schemas.gear_item import GearItemInfo
from ...schemas.gear_set import (
    GearSetCreateInternal,
    GearSetCreateRequest,
    GearSetRead,
    GearSetReadInternal,
    GearSetUpdateRequest,
)
from ...services.cache_invalidation import invalidate_gear_caches

router = APIRouter(tags=["gear"])


async def _get_owned_gear_set(
    db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict, *, include_deleted: bool = False
) -> GearSetReadInternal:
    """Fetch a gear set by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for the 404/403 split and, in
    particular, why this must run before any `@cache`-wrapped read helper.
    """
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_gear_sets,
        uuid=uuid,
        current_user=current_user,
        schema=GearSetReadInternal,
        not_found_message="Gear set not found",
        include_deleted=include_deleted,
    )


def _to_public_gear_set(
    db_gear_set: GearSetReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    gear_items: list[GearItemInfo],
) -> GearSetRead:
    """Convert an internal gear set representation (integer FKs) into its public shape
    (owning user referenced by `uuid`, member items embedded)."""
    data = db_gear_set if isinstance(db_gear_set, dict) else db_gear_set.model_dump()
    return GearSetRead(
        **{k: v for k, v in data.items() if k not in ("id", "user_id")},
        user_uuid=user_uuid,
        gear_items=gear_items,
    )


async def _resolve_item_ids(db: AsyncSession, gear_item_uuids: list[uuid_pkg.UUID], user_id: int) -> list[int]:
    """Resolve the request's gear item uuids to internal ids, preserving their order.
    Raises 422 if any uuid isn't one of this user's gear items.
    """
    id_by_uuid = await resolve_gear_item_ids_for_user(db=db, gear_item_uuids=gear_item_uuids, user_id=user_id)
    if id_by_uuid is None:
        raise UnprocessableEntityException("Gear item not found.")
    return [id_by_uuid[u] for u in gear_item_uuids]


@router.post("/gear-set", response_model=GearSetRead, status_code=201)
async def write_gear_set(
    request: Request,
    gear_set: GearSetCreateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> GearSetRead:
    if current_user["uuid"] != gear_set.user_uuid:
        raise ForbiddenException()

    if await gear_set_name_exists(db=db, user_id=current_user["id"], name=gear_set.name):
        raise DuplicateValueException("A gear set with this name already exists")

    gear_item_ids = await _resolve_item_ids(db, gear_set.gear_item_uuids, current_user["id"])

    gear_set_internal = GearSetCreateInternal(name=gear_set.name, weight=gear_set.weight, user_id=current_user["id"])
    created_gear_set = await crud_gear_sets.create(
        db=db, object=gear_set_internal, schema_to_select=GearSetReadInternal, return_as_model=True
    )
    await replace_gear_items_for_set(db=db, gear_set_id=created_gear_set.id, gear_item_ids=gear_item_ids)
    await invalidate_gear_caches(current_user["id"])

    gear_set_read = await crud_gear_sets.get(
        db=db, id=created_gear_set.id, schema_to_select=GearSetReadInternal, return_as_model=True
    )
    if gear_set_read is None:
        raise NotFoundException("Created gear set not found")

    gear_items = await get_gear_items_for_set(db=db, gear_set_id=created_gear_set.id)
    return _to_public_gear_set(
        cast(GearSetReadInternal, gear_set_read), user_uuid=current_user["uuid"], gear_items=gear_items
    )


@cache(
    key_prefix="user_{user_id}_gear_sets:page_{page}:items_per_page:{items_per_page}",
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_gear_sets(
    request: Request,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    db: AsyncSession,
    page: int,
    items_per_page: int,
) -> dict:
    """Fetches (and caches) a user's paginated gear set list, with each set's items
    attached via one batched lookup. Authorization is checked by the route before this
    is ever reached - `@cache` serves cached responses without re-checking it.
    """
    data = await crud_gear_sets.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        user_id=user_id,
        is_deleted=False,
        sort_columns="name",
        sort_orders="asc",
    )

    set_ids = [s["id"] for s in data["data"]]
    items_by_set = await get_gear_items_for_sets(db=db, gear_set_ids=set_ids)
    data["data"] = [
        _to_public_gear_set(s, user_uuid=user_uuid, gear_items=items_by_set.get(s["id"], [])).model_dump()
        for s in data["data"]
    ]

    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get("/gear-sets", response_model=PaginatedListResponse[GearSetRead])
async def read_gear_sets(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
) -> dict:
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    page, items_per_page = clamp_pagination(page, items_per_page)

    return await _cached_read_gear_sets(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=page,
        items_per_page=items_per_page,
    )


@cache(key_prefix="user_{user_id}_gear_set", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_gear_set(
    request: Request, user_id: int, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> GearSetRead:
    db_gear_set = await crud_gear_sets.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=GearSetReadInternal, return_as_model=True
    )
    if db_gear_set is None:
        raise NotFoundException("Gear set not found")

    db_gear_set = cast(GearSetReadInternal, db_gear_set)
    gear_items = await get_gear_items_for_set(db=db, gear_set_id=db_gear_set.id)
    return _to_public_gear_set(db_gear_set, user_uuid=owner_uuid, gear_items=gear_items)


@router.get("/gear-set/{uuid}", response_model=GearSetRead)
async def read_gear_set(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> GearSetRead:
    # Authorize before the cached read: `@cache` replays a hit without re-checking.
    await _get_owned_gear_set(db, uuid, current_user)

    return await _cached_read_gear_set(
        request, user_id=current_user["id"], uuid=uuid, owner_uuid=current_user["uuid"], db=db
    )


@router.patch("/gear-set/{uuid}")
async def patch_gear_set(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: GearSetUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partial update. Passing `gear_item_uuids` replaces the set's members wholesale -
    this is what the dive form's "Save as set" does when saving onto an existing set.
    """
    db_gear_set = await _get_owned_gear_set(db, uuid, current_user)

    if values.name is not None and await gear_set_name_exists(
        db=db, user_id=db_gear_set.user_id, name=values.name, exclude_id=db_gear_set.id
    ):
        raise DuplicateValueException("A gear set with this name already exists")

    gear_item_ids: list[int] | None = None
    if values.gear_item_uuids is not None:
        gear_item_ids = await _resolve_item_ids(db, values.gear_item_uuids, db_gear_set.user_id)

    update_data = values.model_dump(exclude={"gear_item_uuids"}, exclude_unset=True)
    if update_data:
        await crud_gear_sets.update(db=db, object=update_data, uuid=uuid)

    if gear_item_ids is not None:
        await replace_gear_items_for_set(db=db, gear_set_id=db_gear_set.id, gear_item_ids=gear_item_ids)

    if update_data or gear_item_ids is not None:
        await invalidate_gear_caches(db_gear_set.user_id)

    return {"message": "Gear set updated"}


@router.delete("/gear-set/{uuid}")
async def erase_gear_set(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Soft-deletes a gear set. Sets are purely a convenience shortcut, so deleting one
    never touches the gear items in it, nor any dive those items were logged on.
    """
    # `include_deleted`: deleting an already-soft-deleted gear set is a no-op, not a 404.
    owner_id = (await _get_owned_gear_set(db, uuid, current_user, include_deleted=True)).user_id

    await crud_gear_sets.delete(db=db, uuid=uuid)
    await invalidate_gear_caches(owner_id)

    return {"message": "Gear set deleted"}
