from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive_gear_item import DiveGearItem
from ..models.gear_item import GearItem
from ..schemas.gear_item import GearItemInfo
from .crud_gear_items import GEAR_ITEM_INFO_COLUMNS, gear_item_info_from_row


async def get_gear_items_for_dive(db: AsyncSession, dive_id: int) -> list[GearItemInfo]:
    """Return the gear items used on a dive, in the order they were listed.

    Nothing hides here, and nothing needs to: `erase_gear_item` is a real `DELETE` and
    `dive_gear_item.gear_item_id` is `ON DELETE CASCADE`, so a join row cannot outlive the
    item it points at. Same shape as `get_dive_sites_for_dive`.

    **Archived** items come through as they always have, flagged `is_archived` for the
    client to render: archiving retires kit from the dive form's picker precisely so the
    dives that used it keep showing it. That is now the only way for an item to leave the
    picker without leaving the dives.
    """
    result = await db.execute(
        select(*GEAR_ITEM_INFO_COLUMNS)
        .join(DiveGearItem, DiveGearItem.gear_item_id == GearItem.id)
        .where(DiveGearItem.dive_id == dive_id)
        .order_by(DiveGearItem.position)
    )
    return [gear_item_info_from_row(row) for row in result]


async def get_gear_items_for_dives(db: AsyncSession, dive_ids: list[int]) -> dict[int, list[GearItemInfo]]:
    """Batched version of `get_gear_items_for_dive`, e.g. for a paginated dive listing.

    Pre-seeds the per-dive lists empty so a dive whose only item was deleted comes back
    with `[]` rather than dropping out of the mapping.
    """
    items_by_dive: dict[int, list[GearItemInfo]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return items_by_dive

    result = await db.execute(
        select(DiveGearItem.dive_id, *GEAR_ITEM_INFO_COLUMNS)
        .join(GearItem, GearItem.id == DiveGearItem.gear_item_id)
        .where(DiveGearItem.dive_id.in_(dive_ids))
        .order_by(DiveGearItem.dive_id, DiveGearItem.position)
    )
    for row in result:
        items_by_dive[row.dive_id].append(gear_item_info_from_row(row))
    return items_by_dive


async def replace_gear_items_for_dive(
    db: AsyncSession, dive_id: int, gear_item_ids: list[int], commit: bool = True
) -> None:
    """Replace all gear items for a dive with the given ordered list.

    Duplicate ids are silently deduplicated (keeping each id's first occurrence, which
    determines its position) to avoid a unique-constraint violation - same
    delete-and-reinsert approach as `replace_dive_sites_for_dive`/`replace_mixtures_for_dive`.

    Safe to hand a full list, for the reason `replace_dive_sites_for_dive` gives: a deleted
    gear item takes its `dive_gear_item` rows with it, so what `get_gear_items_for_dive`
    hands out is the whole truth and echoing it back destroys nothing.
    """
    unique_ids = list(dict.fromkeys(gear_item_ids))
    await db.execute(delete(DiveGearItem).where(DiveGearItem.dive_id == dive_id))
    for position, gear_item_id in enumerate(unique_ids):
        db.add(DiveGearItem(dive_id=dive_id, gear_item_id=gear_item_id, position=position))
    if commit:
        await db.commit()
