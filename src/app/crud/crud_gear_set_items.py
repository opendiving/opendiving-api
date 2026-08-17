from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.gear_item import GearItem
from ..models.gear_set_item import GearSetItem
from ..schemas.gear_item import GearItemInfo
from .crud_gear_items import GEAR_ITEM_INFO_COLUMNS, gear_item_info_from_row


async def get_gear_items_for_set(db: AsyncSession, gear_set_id: int) -> list[GearItemInfo]:
    """Return the *live* gear items in a set, in the order they were added.

    The symptom is the one `get_gear_items_for_dive` has: `erase_gear_item` flags the item
    and leaves the `gear_set_item` rows alone, so without this a set goes on listing kit
    that `GET /gear-item/{uuid}` answers 404 for and that `PATCH /gear-set` refuses back -
    `resolve_gear_item_ids_for_user` resolves only live items, so reading a set's item list
    and writing it verbatim would 422.

    The *answer* was a real choice here rather than the obvious one, because a gear set is
    a template the diver curates and not a record of anything that happened, so clearing
    the rows in `erase_gear_item` was defensible in a way it is not for a dive. Hiding won
    on two things a delete-time clear cannot do: it fixes the orphans already in the
    database, and it keeps deleting one item from silently rewriting every set the diver
    owns. See DECISIONS.md before changing it back.

    The links survive the delete, and no further than the set's next membership edit:
    `patch_gear_set` runs `replace_gear_items_for_set` - a delete-and-reinsert - whenever
    the request carries `gear_item_uuids`, so a client submitting back the shortened list
    it was handed drops the row for good. A rename or a weight change does not, which is
    the one way this is gentler than the dive half.

    Only `is_deleted` hides. Archived items come through flagged, exactly as on the dive
    loaders: archiving retires kit from the picker without unpicking the sets it is in.
    """
    result = await db.execute(
        select(*GEAR_ITEM_INFO_COLUMNS)
        .join(GearSetItem, GearSetItem.gear_item_id == GearItem.id)
        .where(GearSetItem.gear_set_id == gear_set_id, GearItem.is_deleted.is_(False))
        .order_by(GearSetItem.position)
    )
    return [gear_item_info_from_row(row) for row in result]


async def get_gear_items_for_sets(db: AsyncSession, gear_set_ids: list[int]) -> dict[int, list[GearItemInfo]]:
    """Batched version of `get_gear_items_for_set`, for the paginated gear set listing.

    Filters deleted items for the same reasons - this is what `GET /gear-sets` builds its
    page from, so a filter on the single-set loader alone would leave the list still
    showing them - and degrades the same way: the per-set lists are pre-seeded empty, so a
    set whose every item is gone comes back with `[]` rather than dropping out of the
    mapping its caller indexes.
    """
    items_by_set: dict[int, list[GearItemInfo]] = {gear_set_id: [] for gear_set_id in gear_set_ids}
    if not gear_set_ids:
        return items_by_set

    result = await db.execute(
        select(GearSetItem.gear_set_id, *GEAR_ITEM_INFO_COLUMNS)
        .join(GearItem, GearItem.id == GearSetItem.gear_item_id)
        .where(GearSetItem.gear_set_id.in_(gear_set_ids), GearItem.is_deleted.is_(False))
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

    **The wipe takes soft-deleted items with it, and that is a known accepted loss** - the
    third of the three `replace_*` helpers carrying it, after `replace_dive_sites_for_dive`
    and `replace_gear_items_for_dive`, and for the same reason. Since `get_gear_items_for_set`
    stopped returning deleted items, a client editing a set's membership submits back only
    what it was shown, so a set holding a live item and a hidden one comes back without the
    hidden one and the delete below destroys its row. No client can prevent it: it cannot
    preserve a reference it was never handed.

    One thing is milder here than on the dive helpers. `patch_gear_set` reaches this only
    when the request actually carries `gear_item_uuids`, so renaming a set or changing its
    `weight` severs nothing - there is no equivalent of the trip half's indistinguishable
    "the diver cleared it" versus "the client echoed back a null", because an absent list
    means *don't touch*. The narrow case survives regardless: adding one item resubmits the
    whole list, and the hidden row goes with it.

    Declined rather than missed - see "One narrower case `dirtyFields` cannot reach" in
    DECISIONS.md for the fix and why its cost was judged too high for a path this narrow.
    """
    unique_ids = list(dict.fromkeys(gear_item_ids))
    await db.execute(delete(GearSetItem).where(GearSetItem.gear_set_id == gear_set_id))
    for position, gear_item_id in enumerate(unique_ids):
        db.add(GearSetItem(gear_set_id=gear_set_id, gear_item_id=gear_item_id, position=position))
    if commit:
        await db.commit()
