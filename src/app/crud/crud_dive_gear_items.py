from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive_gear_item import DiveGearItem
from ..models.gear_item import GearItem
from ..schemas.gear_item import GearItemInfo
from .crud_gear_items import GEAR_ITEM_INFO_COLUMNS, gear_item_info_from_row


async def get_gear_items_for_dive(db: AsyncSession, dive_id: int) -> list[GearItemInfo]:
    """Return the *live* gear items used on a dive, in the order they were listed.

    The same filter, for the same reasons, as `get_dive_sites_for_dive`: `erase_gear_item`
    flags the item and leaves the `dive_gear_item` rows alone, so without this a dive goes
    on listing kit that `GET /gear-item/{uuid}` answers 404 for, and that `PATCH /dive`
    refuses to accept back (`resolve_gear_item_ids_for_user` resolves only live items, so
    reading a dive's gear list and writing it back verbatim would 422). The links stay
    because export still wants them: `_owned` in `services/export/loader.py` reads
    deleted-but-referenced items back on purpose, flagged `is_deleted`, so UDDF's
    `xs:IDREF` references resolve.

    They stay only until that dive's next `PATCH`, though, and this filter is what makes
    that so: a client seeding an edit form from this list submits it back one entry short,
    and `replace_gear_items_for_dive` is a delete-and-reinsert. `recalculate_gear_dive_counts`
    then drops the item's `dive_count` to match, which is the one way the severance shows on
    the deleted item's own row. See "The links outlive the delete, but not the dive's next
    edit" in DECISIONS.md before writing anything that relies on the row being there.

    Only `is_deleted` hides. Archived items come through as they always have, flagged
    `is_archived` for the client to render: archiving retires kit from the dive form's
    picker precisely so the dives that used it keep showing it.
    """
    result = await db.execute(
        select(*GEAR_ITEM_INFO_COLUMNS)
        .join(DiveGearItem, DiveGearItem.gear_item_id == GearItem.id)
        .where(DiveGearItem.dive_id == dive_id, GearItem.is_deleted.is_(False))
        .order_by(DiveGearItem.position)
    )
    return [gear_item_info_from_row(row) for row in result]


async def get_gear_items_for_dives(db: AsyncSession, dive_ids: list[int]) -> dict[int, list[GearItemInfo]]:
    """Batched version of `get_gear_items_for_dive`, e.g. for a paginated dive listing.

    Filters deleted items for the same reasons - this is what `GET /dives` enriches its
    rows through, so a filter on the single-dive loader alone would leave the list page
    still showing them - and needs nothing extra to degrade well: the per-dive lists are
    pre-seeded empty, so a dive whose only item is gone comes back with `[]` rather than
    dropping out of the mapping.
    """
    items_by_dive: dict[int, list[GearItemInfo]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return items_by_dive

    result = await db.execute(
        select(DiveGearItem.dive_id, *GEAR_ITEM_INFO_COLUMNS)
        .join(GearItem, GearItem.id == DiveGearItem.gear_item_id)
        .where(DiveGearItem.dive_id.in_(dive_ids), GearItem.is_deleted.is_(False))
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

    **The wipe takes soft-deleted items with it, and that is a known accepted loss** - the
    same one `replace_dive_sites_for_dive` carries, for the same reason. Since
    `get_gear_items_for_dive` stopped returning deleted items, a client editing a dive's
    gear list submits back only what it was shown, so a dive holding a live item and a
    hidden one comes back without the hidden one and the delete below destroys its row. The
    diver never saw it and never asked to remove it, and no client can preserve a reference
    it was never handed.

    Declined rather than missed, though **not** for the reason the site half declined it: the
    position-contiguity cost that settled it there does not apply here, since gear has no
    `replace_gear_item_on_dives` to keep in step and `position` is a pure sort key. Filtering
    the delete would be about one subquery. It was declined to keep this function and
    `replace_dive_sites_for_dive` mirrors of each other - see "One narrower case
    `dirtyFields` cannot reach" in DECISIONS.md, which gives both halves of that.
    """
    unique_ids = list(dict.fromkeys(gear_item_ids))
    await db.execute(delete(DiveGearItem).where(DiveGearItem.dive_id == dive_id))
    for position, gear_item_id in enumerate(unique_ids):
        db.add(DiveGearItem(dive_id=dive_id, gear_item_id=gear_item_id, position=position))
    if commit:
        await db.commit()
