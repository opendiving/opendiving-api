from sqlalchemy import func, or_, select, union, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive import Dive
from ..models.dive_gear_item import DiveGearItem
from ..models.dive_mixture import DiveMixture
from ..models.gear_item import GearItem


async def recalculate_gear_dive_counts(db: AsyncSession, user_id: int, commit: bool = True) -> None:
    """Recompute `gear_item.dive_count` and `gear_item.dive_time` for every one of a user's
    gear items.

    Recomputing from scratch (rather than incrementally adjusting counters) keeps this
    immune to drift, exactly like `services.dive_stats.recalculate_dive_stats`: it's
    called after every dive create, update, and delete (soft or hard) for the affected
    user, and always reflects their current set of non-deleted dives.

    A dive uses an item when the item is in its gear list or is one of its cylinders'
    vessel, and counts once however many ways it names the item: the `UNION` is over
    `(dive, item)` pairs, so a vessel listed in the gear as well is one dive, not two. The
    time is the summed `submerged_time` of the same dives.

    Only rows whose figures actually changed are written (`IS DISTINCT FROM`), so the
    common case - a dive edit that doesn't touch its gear - doesn't rewrite the user's
    whole gear table and churn `updated_at`-adjacent tuple versions for nothing.
    """
    # Only dives that are still live (`is_deleted = false`) count, so soft-deleting a dive
    # takes it off its gear's figures on the next recalculation.
    live = (Dive.user_id == user_id, Dive.is_deleted.is_(False))
    uses = union(
        select(DiveGearItem.dive_id, DiveGearItem.gear_item_id, Dive.submerged_time)
        .join(Dive, Dive.id == DiveGearItem.dive_id)
        .where(*live),
        select(DiveMixture.dive_id, DiveMixture.gear_item_id, Dive.submerged_time)
        .join(Dive, Dive.id == DiveMixture.dive_id)
        .where(*live, DiveMixture.gear_item_id.is_not(None)),
    ).cte("uses")

    count_for_item = select(func.count()).where(uses.c.gear_item_id == GearItem.id).scalar_subquery()
    time_for_item = (
        select(func.coalesce(func.sum(uses.c.submerged_time), 0))
        .where(uses.c.gear_item_id == GearItem.id)
        .scalar_subquery()
    )

    await db.execute(
        update(GearItem)
        .where(
            GearItem.user_id == user_id,
            or_(
                GearItem.dive_count.is_distinct_from(count_for_item), GearItem.dive_time.is_distinct_from(time_for_item)
            ),
        )
        .values(dive_count=count_for_item, dive_time=time_for_item)
    )

    if commit:
        await db.commit()
