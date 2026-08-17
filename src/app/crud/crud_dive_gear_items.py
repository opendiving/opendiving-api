from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive_gear_item import DiveGearItem
from ..models.gear_item import GearItem
from ..schemas.gear_item import GearItemInfo
from .crud_gear_items import GEAR_ITEM_INFO_COLUMNS, gear_item_info_from_row


async def get_gear_items_for_dive(db: AsyncSession, dive_id: int) -> list[GearItemInfo]:
    """Return the gear items used on a dive, in the order they were listed.

    **Includes soft-deleted items, which is the same bug the dive-site loaders had and a
    deliberate deferral rather than an oversight.** A deleted gear item goes on being
    listed on the dives it was used on, though `GET /gear-item/{uuid}` 404s for it - see
    "No manual DDL, and one sibling left alone" in DECISIONS.md. Before adding the filter,
    check `erase_gear_item`: it has its own cache invalidation and `dive_count` bookkeeping
    to reason about, which is why this was not bundled into the sites-and-trips change.
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

    Carries the same deferred soft-delete bug, and has to be filtered in the same change:
    this is what `GET /dives` enriches its rows through.
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
    """
    unique_ids = list(dict.fromkeys(gear_item_ids))
    await db.execute(delete(DiveGearItem).where(DiveGearItem.dive_id == dive_id))
    for position, gear_item_id in enumerate(unique_ids):
        db.add(DiveGearItem(dive_id=dive_id, gear_item_id=gear_item_id, position=position))
    if commit:
        await db.commit()
