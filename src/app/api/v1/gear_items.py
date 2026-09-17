import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, NotFoundException
from ...core.utils.cache import cache
from ...core.utils.pagination import clamp_pagination
from ...core.utils.search import search_clause, search_multi
from ...crud.crud_gear_items import crud_gear_items, gear_item_name_exists
from ...crud.crud_gear_service_schedules import get_schedules_for_gear_item, get_schedules_for_gear_items
from ...models.gear_item import GearItem
from ...schemas.gear_item import (
    GearItemCreate,
    GearItemCreateInternal,
    GearItemRead,
    GearItemReadInternal,
    GearItemUpdate,
)
from ...schemas.gear_service import GearServiceScheduleInfo
from ...services.cache_invalidation import invalidate_dive_caches, invalidate_gear_caches

router = APIRouter(tags=["gear"])

# Brand rather than type: divers name their kit inconsistently ("MK25", "my reg"), but
# reach for the brand when they can't recall what they called it. Type is already a
# closed vocabulary with its own filter surface.
GEAR_ITEM_SEARCH_COLUMNS = ("name", "brand")


async def _get_owned_gear_item(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> GearItemReadInternal:
    """Fetch a gear item by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for why someone else's row reads
    as a 404 and, in particular, why this must run before any `@cache`-wrapped read
    helper.
    """
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_gear_items,
        uuid=uuid,
        current_user=current_user,
        schema=GearItemReadInternal,
        not_found_message="Gear item not found",
    )


def _to_public_gear_item(
    db_gear_item: GearItemReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    service: list[GearServiceScheduleInfo] | None = None,
) -> GearItemRead:
    """Convert an internal gear item representation (integer FKs) into its public shape
    (owning user referenced by `uuid`, service schedules embedded).

    `service` defaults to an empty list rather than being fetched here, so the read
    paths can resolve a whole page's schedules in one batched query - and so
    `write_gear_item` can skip the lookup entirely, a brand-new item provably having none.
    """
    data = db_gear_item if isinstance(db_gear_item, dict) else db_gear_item.model_dump()
    return GearItemRead(
        **{k: v for k, v in data.items() if k not in ("id", "user_id")},
        user_uuid=user_uuid,
        service=service or [],
    )


@router.post("/gear-item", response_model=GearItemRead, status_code=201)
async def write_gear_item(
    request: Request,
    gear_item: GearItemCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> GearItemRead:
    """Create a gear item for the authenticated user.

    Uniqueness is on brand *and* name together, so the same model from two brands is
    fine; a genuine repeat is a 422.
    """
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
    key_prefix=(
        "user_{user_id}_gear_items:page_{page}:items_per_page:{items_per_page}"
        ":archived_{include_archived}:search_{search}"
    ),
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
    search: str | None = None,
) -> dict:
    """Fetches (and caches) a user's paginated gear item list.

    Like the other cached read helpers, this must only ever be called after the caller's
    authorization has been checked by the route - `@cache` serves cached responses
    without re-running any authorization logic. `include_archived` is part of the cache
    key so the picker's (non-archived) view and the management page's (full) view can't
    serve each other's results, and `search` for the same reason between two queries.

    Hand-written rather than built with `OwnedResourceCache` (as trips, dive sites and
    gear sets are) because of the two things that factory has no room for: the extra
    `include_archived` filter dimension, and the batched service-schedule lookup below.
    See that class's docstring for why the duplication is preferred over a hook.
    """
    offset = compute_offset(page, items_per_page)
    term = (search or "").strip()

    if term:
        conditions = [
            GearItem.user_id == user_id,
            search_clause(GearItem, GEAR_ITEM_SEARCH_COLUMNS, term),
        ]
        if not include_archived:
            conditions.append(GearItem.is_archived.is_(False))

        data = await search_multi(
            db=db,
            model=GearItem,
            conditions=tuple(conditions),
            sort_column="name",
            sort_order="asc",
            offset=offset,
            limit=items_per_page,
        )
    else:
        filters: dict[str, Any] = {"user_id": user_id}
        if not include_archived:
            filters["is_archived"] = False

        data = await crud_gear_items.get_multi(
            db=db,
            offset=offset,
            limit=items_per_page,
            sort_columns="name",
            sort_orders="asc",
            **filters,
        )
    # One batched query for the whole page's service schedules rather than one per row -
    # every gear row shows a service badge, so an N+1 here would be on the hot path.
    # `get_multi` passes no `schema_to_select`, so each row still carries its internal `id`.
    schedules_by_item = await get_schedules_for_gear_items(db=db, gear_item_ids=[item["id"] for item in data["data"]])
    data["data"] = [
        _to_public_gear_item(item, user_uuid=user_uuid, service=schedules_by_item[item["id"]]).model_dump()
        for item in data["data"]
    ]

    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get("/gear-items", response_model=PaginatedListResponse[GearItemRead])
async def read_gear_items(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    include_archived: bool = False,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive substring match on name or brand"),
    ] = None,
) -> dict:
    """List the caller's gear. Archived items are excluded unless `include_archived=true`,
    so the dive form's picker only ever offers gear that's still in service.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    return await _cached_read_gear_items(
        request,
        user_id=current_user["id"],
        user_uuid=current_user["uuid"],
        db=db,
        page=page,
        items_per_page=items_per_page,
        include_archived=include_archived,
        # Normalized here rather than in the cache layer so that " MK25 " and "mk25"
        # share one cache entry instead of two identical ones under different keys.
        search=(search or "").strip().lower() or None,
    )


@cache(key_prefix="user_{user_id}_gear_item", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_gear_item(
    request: Request, user_id: int, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> GearItemRead:
    """Fetches (and caches) a single gear item by uuid. Authorization is checked by the
    route before this is ever reached - see `_cached_read_gear_items`.
    """
    db_gear_item = await crud_gear_items.get(
        db=db, uuid=uuid, schema_to_select=GearItemReadInternal, return_as_model=True
    )
    if db_gear_item is None:
        raise NotFoundException("Gear item not found")

    db_gear_item = cast(GearItemReadInternal, db_gear_item)
    service = await get_schedules_for_gear_item(db=db, gear_item_id=db_gear_item.id)
    return _to_public_gear_item(db_gear_item, user_uuid=owner_uuid, service=service)


@router.get("/gear-item/{uuid}", response_model=GearItemRead)
async def read_gear_item(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> GearItemRead:
    """Return a single gear item, with its service schedules attached.

    404 when no such item exists - and the same 404 when it belongs to another user, so
    someone else's uuid stays unprobeable.
    """
    # Authorize before the cached read: `@cache` replays a hit without re-checking.
    await _get_owned_gear_item(db, uuid, current_user)

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
    db_gear_item = await _get_owned_gear_item(db, uuid, current_user)

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
    """Delete a gear item, and everything hanging off it.

    404 unless the caller owns it, and a second `DELETE` on the same uuid is a 404 too.
    Four `ON DELETE CASCADE`s do the work, all of them already declared on their FKs:
    `dive_gear_item` and `gear_set_item` rows go, so the item drops off the dives it was
    used on and out of the sets it was in; `gear_service_schedule` rows go, so the digest
    stops emailing about kit the diver no longer has; and `gear_service_record` rows go
    with them, so the item's service history is destroyed rather than kept invisibly.

    That last one is the only behaviour a diver could notice as a loss, and the delete
    dialog already promises it: **archiving** is the non-destructive way to retire gear
    you still want in your log *and its service history*. See "A deleted gear item's
    service history has no view, and archiving is the surface that does" in DECISIONS.md.
    """
    db_gear_item = await _get_owned_gear_item(db, uuid, current_user)
    owner_id = db_gear_item.user_id

    await crud_gear_items.delete(db=db, uuid=uuid)
    # Load-bearing twice over: this drops the gear *set* caches too (one `user_{id}_gear_*`
    # pattern covers all four key shapes), without which a cached set read would go on
    # listing the deleted item as a member for the rest of the hour - stale in its
    # membership, not just in its fields.
    await invalidate_gear_caches(owner_id)
    # Unconditional, and load-bearing: a fresh dive read now omits this item, so every
    # cached read of a dive that used it would go on listing kit the diver has deleted for
    # the rest of the hour.
    await invalidate_dive_caches(owner_id)

    return {"message": "Gear item deleted"}
