from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.gear_item import GearItem
from ..models.gear_set_item import GearSetItem
from ..schemas.gear_item import GearItemInfo
from .crud_gear_items import GEAR_ITEM_INFO_COLUMNS, gear_item_info_from_row


async def get_gear_items_for_set(db: AsyncSession, gear_set_id: int) -> list[GearItemInfo]:
    """Return the gear items in a set, in the order they were added."""
    result = await db.execute(
        select(*GEAR_ITEM_INFO_COLUMNS)
        .join(GearSetItem, GearSetItem.gear_item_id == GearItem.id)
        .where(GearSetItem.gear_set_id == gear_set_id)
        .order_by(GearSetItem.position)
    )
    return [gear_item_info_from_row(row) for row in result]


async def get_gear_items_for_sets(db: AsyncSession, gear_set_ids: list[int]) -> dict[int, list[GearItemInfo]]:
    """Batched version of `get_gear_items_for_set`, for the paginated gear set listing."""
    items_by_set: dict[int, list[GearItemInfo]] = {gear_set_id: [] for gear_set_id in gear_set_ids}
    if not gear_set_ids:
        return items_by_set

    result = await db.execute(
        select(GearSetItem.gear_set_id, *GEAR_ITEM_INFO_COLUMNS)
        .join(GearItem, GearItem.id == GearSetItem.gear_item_id)
        .where(GearSetItem.gear_set_id.in_(gear_set_ids))
        .order_by(GearSetItem.gear_set_id, GearSetItem.position)
    )
    for row in result:
        items_by_set[row.gear_set_id].append(gear_item_info_from_row(row))
    return items_by_set


async def replace_gear_items_for_set(
    db: AsyncSession, gear_set_id: int, gear_item_ids: list[int], commit: bool = True
) -> None:
    """Replace all items in a gear set with the given ordered list.

    Duplicate ids are silently deduplicated (keeping each id's first occurrence, which
    determines its position) to avoid a unique-constraint violation.
    """
    unique_ids = list(dict.fromkeys(gear_item_ids))
    await db.execute(delete(GearSetItem).where(GearSetItem.gear_set_id == gear_set_id))
    for position, gear_item_id in enumerate(unique_ids):
        db.add(GearSetItem(gear_set_id=gear_set_id, gear_item_id=gear_item_id, position=position))
    if commit:
        await db.commit()
