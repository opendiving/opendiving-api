from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.gear_item import GearItem
from ..models.gear_set_item import GearSetItem
from ..schemas.gear_item import GearItemInfo
from .crud_gear_items import GEAR_ITEM_INFO_COLUMNS, gear_item_info_from_row


async def get_gear_items_for_set(db: AsyncSession, gear_set_id: int) -> list[GearItemInfo]:
    """Return the gear items in a set, in the order they were added.

    Nothing hides here: `erase_gear_item` is a real `DELETE` and `gear_set_item.gear_item_id`
    is `ON DELETE CASCADE`, so deleting one item unpicks it from every set the diver owns,
    at the source. That is the outcome the old read filter was chosen *over* - clearing the
    rows at delete time was rejected partly because it "silently rewrites every set the
    diver owns" - and the objection does not survive the row itself going: there is no
    membership left to rewrite, and no orphan left to hide.

    **Archived** items come through flagged, exactly as on the dive loaders: archiving
    retires kit from the picker without unpicking the sets it is in, and is now the only
    thing that does.
    """
    result = await db.execute(
        select(*GEAR_ITEM_INFO_COLUMNS)
        .join(GearSetItem, GearSetItem.gear_item_id == GearItem.id)
        .where(GearSetItem.gear_set_id == gear_set_id)
        .order_by(GearSetItem.position)
    )
    return [gear_item_info_from_row(row) for row in result]


async def get_gear_items_for_sets(db: AsyncSession, gear_set_ids: list[int]) -> dict[int, list[GearItemInfo]]:
    """Batched version of `get_gear_items_for_set`, for the paginated gear set listing.

    Pre-seeds the per-set lists empty so a set whose every item was deleted comes back with
    `[]` rather than dropping out of the mapping its caller indexes.
    """
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

    Safe to hand a full list: a deleted gear item takes its `gear_set_item` rows with it,
    so what `get_gear_items_for_set` hands out is the whole membership and echoing it back
    destroys nothing. `patch_gear_set` still reaches this only when the request actually
    carries `gear_item_uuids` - an absent list means *don't touch*, the same guard
    `patch_dive` puts on both of its list replacements.
    """
    unique_ids = list(dict.fromkeys(gear_item_ids))
    await db.execute(delete(GearSetItem).where(GearSetItem.gear_set_id == gear_set_id))
    for position, gear_item_id in enumerate(unique_ids):
        db.add(GearSetItem(gear_set_id=gear_set_id, gear_item_id=gear_item_id, position=position))
    if commit:
        await db.commit()
