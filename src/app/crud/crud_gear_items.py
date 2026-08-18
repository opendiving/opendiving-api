import uuid as uuid_pkg
from typing import Any

from fastcrud import FastCRUD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.gear_item import GearItem
from ..schemas.gear_item import (
    GearItemCreateInternal,
    GearItemInfo,
    GearItemReadInternal,
    GearItemUpdate,
    GearItemUpdateInternal,
)

# The `gear_item` columns making up a `GearItemInfo` (the summary shape embedded in a
# dive or a gear set), in the order `gear_item_info_from_row` unpacks them. Shared by
# `crud_dive_gear_items` and `crud_gear_set_items`, which select the same summary
# through their respective join tables.
GEAR_ITEM_INFO_COLUMNS = (
    GearItem.uuid,
    GearItem.name,
    GearItem.brand,
    GearItem.type,
    GearItem.rented,
    GearItem.is_archived,
)


def gear_item_info_from_row(row: Any) -> GearItemInfo:
    """Build a `GearItemInfo` from a result row selecting `GEAR_ITEM_INFO_COLUMNS`."""
    return GearItemInfo(
        uuid=row.uuid,
        name=row.name,
        brand=row.brand,
        type=row.type,
        rented=row.rented,
        is_archived=row.is_archived,
    )


CRUDGearItem = FastCRUD[
    GearItem, GearItemCreateInternal, GearItemUpdate, GearItemUpdateInternal, GearItemUpdate, GearItemReadInternal
]
crud_gear_items = CRUDGearItem(GearItem)


async def resolve_gear_item_ids_for_user(
    db: AsyncSession, gear_item_uuids: list[uuid_pkg.UUID], user_id: int
) -> dict[uuid_pkg.UUID, int] | None:
    """Resolve gear item public `uuid`s to their internal `id`s, scoped to gear items
    belonging to the given user.

    Archived items resolve normally: archiving only hides an item from the dive form's
    picker, it must not stop an existing dive/gear set that already references it from
    being saved again.

    Returns `None` if any given uuid doesn't resolve to a gear item owned by the user
    (used to prevent a user from attaching another user's gear to their own dive/set).
    """
    unique_uuids = set(gear_item_uuids)
    if not unique_uuids:
        return {}

    stmt = select(GearItem.uuid, GearItem.id).where(
        GearItem.uuid.in_(unique_uuids),
        GearItem.user_id == user_id,
    )
    result = await db.execute(stmt)
    mapping = {row.uuid: row.id for row in result}
    if mapping.keys() != unique_uuids:
        return None
    return mapping


async def get_gear_item_uuids_by_id(db: AsyncSession, gear_item_ids: list[int]) -> dict[int, uuid_pkg.UUID]:
    """Resolve internal gear item `id`s back to their public `uuid`s, in one query.

    The reverse of `resolve_gear_item_ids_for_user`, for read paths that hold internal
    FKs and need to emit the public shape - a service schedule/record row carries
    `gear_item_id`, but `GearServiceScheduleRead` exposes `gear_item_uuid`. Batched so a
    paginated listing resolves every row's item in one round trip instead of per row.

    Unlike its counterpart this does no ownership filtering: callers reach it only with
    ids taken from rows they have already authorized.

    **Every call site treats a miss as real**, and none of them index this mapping directly.
    `gear_item_id` is `NOT NULL` and `ON DELETE CASCADE` on both tables that carry it, so
    within any one consistent snapshot a schedule or record whose item is gone is gone
    itself - but the caller reads those rows in a *separate statement* from this one, and
    the session runs at READ COMMITTED, so a `DELETE /gear-item/{uuid}` committing in
    between leaves an id here that resolves to nothing. The list routes drop the row; the
    single-resource routes 404, the answer they would have given a moment later anyway.

    That window is new. Through the soft-delete era the `gear_item` row survived its own
    deletion, so this lookup could not miss whatever the timing, and indexing directly was
    safe for a reason that stopped holding when the delete became real. See "The service
    -record resolvers split, and only one of them was the same question" in DECISIONS.md
    for why the mapping is nonetheless still unfiltered.
    """
    if not gear_item_ids:
        return {}

    result = await db.execute(select(GearItem.id, GearItem.uuid).where(GearItem.id.in_(set(gear_item_ids))))
    return {row.id: row.uuid for row in result}


async def gear_item_name_exists(
    db: AsyncSession, user_id: int, name: str, brand: str | None = None, exclude_id: int | None = None
) -> bool:
    """Case-insensitive check for whether a gear item with the same (brand, name) already
    exists for the user.

    Mirrors the `ux_gear_item_user_id_brand_name_lower` unique index. Two items with NULL
    brand and the same name are treated as duplicates. Archived items count - archiving is
    not deleting, and the index does not look away from them either.
    """
    stmt = select(GearItem.id).where(
        GearItem.user_id == user_id,
        func.lower(GearItem.name) == name.strip().lower(),
    )
    if brand is None:
        stmt = stmt.where(GearItem.brand.is_(None))
    else:
        stmt = stmt.where(func.lower(GearItem.brand) == brand.strip().lower())
    if exclude_id is not None:
        stmt = stmt.where(GearItem.id != exclude_id)

    result = await db.execute(stmt.limit(1))
    return result.first() is not None
