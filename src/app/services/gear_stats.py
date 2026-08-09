from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive import Dive
from ..models.dive_gear_item import DiveGearItem
from ..models.gear_item import GearItem


async def recalculate_gear_dive_counts(db: AsyncSession, user_id: int, commit: bool = True) -> None:
    """Recompute `gear_item.dive_count` for every one of a user's gear items.

    Recomputing from scratch (rather than incrementally adjusting counters) keeps this
    immune to drift, exactly like `services.dive_stats.recalculate_dive_stats`: it's
    called after every dive create, update, and delete (soft or hard) for the affected
    user, and always reflects their current set of non-deleted dives.

    Only rows whose count actually changed are written (`dive_count IS DISTINCT FROM ...`),
    so the common case - a dive edit that doesn't touch its gear - doesn't rewrite the
    user's whole gear table and churn `updated_at`-adjacent tuple versions for nothing.
    """
    # Per-item dive counts for this user, as a correlated scalar subquery: only dives
    # that are still live (`is_deleted = false`) count, so soft-deleting a dive
    # decrements its gear's counts on the next recalculation.
    count_for_item = (
        select(func.count())
        .select_from(DiveGearItem)
        .join(Dive, Dive.id == DiveGearItem.dive_id)
        .where(
            DiveGearItem.gear_item_id == GearItem.id,
            Dive.user_id == user_id,
            Dive.is_deleted.is_(False),
        )
        .scalar_subquery()
    )

    await db.execute(
        update(GearItem)
        .where(GearItem.user_id == user_id, GearItem.dive_count.is_distinct_from(count_for_item))
        .values(dive_count=count_for_item)
    )

    if commit:
        await db.commit()
