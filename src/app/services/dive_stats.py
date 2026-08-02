from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive import Dive
from ..models.user_dive_stats import UserDiveStats


async def recalculate_dive_stats(db: AsyncSession, user_id: int, commit: bool = True) -> UserDiveStats:
    """Recompute a user's aggregate dive stats from their current dives.

    Recomputing from scratch (rather than incrementally adjusting counters) keeps
    this immune to drift - it's called after every dive create, update, and delete
    (soft or hard) for the affected user, and always reflects their current set of
    non-deleted dives. `species_seen` is not derived from dives yet, so existing
    values are preserved (defaulting to 0 for a brand-new record).
    """
    result = await db.execute(
        select(
            func.count(Dive.id),
            func.coalesce(func.max(Dive.max_depth), 0),
            func.coalesce(func.sum(Dive.duration), 0),
        ).where(Dive.user_id == user_id, Dive.is_deleted.is_(False))
    )
    total_dives, max_depth, total_time = result.one()

    stats_result = await db.execute(select(UserDiveStats).where(UserDiveStats.user_id == user_id))
    stats = stats_result.scalar_one_or_none()

    if stats is None:
        stats = UserDiveStats(
            user_id=user_id,
            total_dives=total_dives,
            max_depth=float(max_depth),
            total_time=int(total_time),
            species_seen=0,
        )
        db.add(stats)
    else:
        stats.total_dives = total_dives
        stats.max_depth = float(max_depth)
        stats.total_time = int(total_time)
        stats.updated_at = datetime.now(UTC)

    if commit:
        await db.commit()
        await db.refresh(stats)

    return stats
