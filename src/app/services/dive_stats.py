from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive import Dive
from ..models.dive_species import DiveSpecies
from ..models.user_dive_stats import UserDiveStats


async def recalculate_dive_stats(db: AsyncSession, user_id: int, commit: bool = True) -> UserDiveStats:
    """Recompute a user's aggregate dive stats from their current dives.

    Recomputing from scratch (rather than incrementally adjusting counters) keeps
    this immune to drift - it's called after every dive create, update, and delete
    (soft or hard) for the affected user, and always reflects their current set of
    non-deleted dives.

    `species_seen` is the number of *distinct* species across those dives, not the number
    of sightings: a diver who saw a turtle on ten dives has seen one species. It is derived
    here rather than anywhere else because this function already runs on every path that
    can change the answer - every dive create, update and delete - so counting it costs no
    new invalidation surface. It was a hardcoded 0 for as long as there was nothing to
    count, which is why the dashboard tile that read it was removed; this is the revisit.

    The main aggregate query is backed by a covering index (`ix_dive_user_id_stats`) so it
    runs as an index-only scan rather than one heap fetch per dive - see DECISIONS.md. The
    species count is a second query rather than a fourth column on that one: it joins a
    different table, and folding it in would turn the covering scan into a join and cost
    every dive write the very thing that index exists to avoid.
    """
    result = await db.execute(
        select(
            # `func.count()` (`COUNT(*)`) rather than `func.count(Dive.id)`: `id` isn't part
            # of `ix_dive_user_id_stats`, so counting it would force a heap fetch per row
            # (defeating the point of the covering index) even though `id` is never null and
            # the two forms are equivalent here.
            func.count(),
            func.coalesce(func.max(Dive.max_depth), 0),
            func.coalesce(func.sum(Dive.duration), 0),
        ).where(Dive.user_id == user_id, Dive.is_deleted.is_(False))
    )
    total_dives, max_depth, total_time = result.one()

    species_seen = await db.scalar(
        select(func.count(func.distinct(DiveSpecies.species_id)))
        .join(Dive, Dive.id == DiveSpecies.dive_id)
        .where(Dive.user_id == user_id, Dive.is_deleted.is_(False))
    )

    stats_result = await db.execute(select(UserDiveStats).where(UserDiveStats.user_id == user_id))
    stats = stats_result.scalar_one_or_none()

    if stats is None:
        stats = UserDiveStats(
            user_id=user_id,
            total_dives=total_dives,
            max_depth=float(max_depth),
            total_time=int(total_time),
            species_seen=int(species_seen or 0),
        )
        db.add(stats)
    else:
        stats.total_dives = total_dives
        stats.max_depth = float(max_depth)
        stats.total_time = int(total_time)
        # Set on this branch too, which is the one nearly every real write takes - the
        # insert above only ever runs once per account. Updating only the insert would
        # leave every diver who already has a stats row at whatever `species_seen` was
        # when they got it, which for every existing account is 0, for good.
        stats.species_seen = int(species_seen or 0)
        stats.updated_at = datetime.now(UTC)

    if commit:
        await db.commit()
        await db.refresh(stats)

    return stats
