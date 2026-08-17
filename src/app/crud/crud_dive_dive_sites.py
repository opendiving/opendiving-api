from typing import Any

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_site import DiveSite
from ..schemas.dive import DiveSiteInfo

# The `dive_site` columns making up a `DiveSiteInfo` (the site summary embedded in a dive
# read), in the order `dive_site_info_from_row` unpacks them. Both loaders below select the
# same summary - one for a single dive, one batched - so the mapping lives here once
# instead of once per query. Same shape as `GEAR_ITEM_INFO_COLUMNS` in `crud_gear_items`,
# which is shared across modules; this pair has no caller outside this one.
DIVE_SITE_INFO_COLUMNS = (
    DiveSite.uuid,
    DiveSite.name,
    DiveSite.location,
    DiveSite.latitude,
    DiveSite.longitude,
)


def dive_site_info_from_row(row: Any) -> DiveSiteInfo:
    """Build a `DiveSiteInfo` from a result row selecting `DIVE_SITE_INFO_COLUMNS`.

    The one place a column is paired with a field, which matters most for the position:
    `latitude=row.longitude` is a valid float in a valid range, so a transposed pair would
    pass every schema check and place the pin in the wrong hemisphere.
    """
    return DiveSiteInfo(
        uuid=row.uuid,
        name=row.name,
        location=row.location,
        latitude=row.latitude,
        longitude=row.longitude,
    )


async def get_dive_sites_for_dive(db: AsyncSession, dive_id: int) -> list[DiveSiteInfo]:
    """Return the dive sites visited during a dive, in the order they were visited."""
    result = await db.execute(
        select(*DIVE_SITE_INFO_COLUMNS)
        .join(DiveDiveSite, DiveDiveSite.dive_site_id == DiveSite.id)
        .where(DiveDiveSite.dive_id == dive_id)
        .order_by(DiveDiveSite.position)
    )
    return [dive_site_info_from_row(row) for row in result]


async def get_dive_sites_for_dives(db: AsyncSession, dive_ids: list[int]) -> dict[int, list[DiveSiteInfo]]:
    """Batched version of `get_dive_sites_for_dive`, e.g. for a paginated dive listing."""
    sites_by_dive: dict[int, list[DiveSiteInfo]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return sites_by_dive

    result = await db.execute(
        select(DiveDiveSite.dive_id, *DIVE_SITE_INFO_COLUMNS)
        .join(DiveSite, DiveSite.id == DiveDiveSite.dive_site_id)
        .where(DiveDiveSite.dive_id.in_(dive_ids))
        .order_by(DiveDiveSite.dive_id, DiveDiveSite.position)
    )
    for row in result:
        sites_by_dive[row.dive_id].append(dive_site_info_from_row(row))
    return sites_by_dive


async def replace_dive_sites_for_dive(
    db: AsyncSession, dive_id: int, dive_site_ids: list[int], commit: bool = True
) -> None:
    """Replace all dive sites for a dive with the given ordered list.

    Duplicate ids are silently deduplicated (keeping each id's first occurrence,
    which determines its position) to avoid a unique-constraint violation.
    """
    unique_ids = list(dict.fromkeys(dive_site_ids))
    await db.execute(delete(DiveDiveSite).where(DiveDiveSite.dive_id == dive_id))
    for position, dive_site_id in enumerate(unique_ids):
        db.add(DiveDiveSite(dive_id=dive_id, dive_site_id=dive_site_id, position=position))
    if commit:
        await db.commit()


async def replace_dive_site_on_dives(
    db: AsyncSession, *, user_id: int, from_dive_site_id: int, to_dive_site_id: int
) -> int:
    """Swap one dive site for another across every live dive of a diver's that was logged
    at it, and return how many dives changed. Does not commit - the caller's delete does,
    so the two land together.

    Three statements rather than a read-modify-write per dive, because the whole point of
    the route this backs is that reassigning a liveaboard's forty dives should not be forty
    round trips in either direction.

    The rules it has to satisfy are the ones `replace_dive_sites_for_dive` already sets for
    a hand-edited site list, restated set-wise:

    - **In place.** A dive's sites are ordered, and position 0 is the primary site every
      single-site surface shows. So the replacement inherits the *doomed* site's slot: a
      dive logged at `[A, X]` whose A is replaced by B reads back as `[B, X]`, not
      `[X, B]`. Plain `UPDATE dive_dive_site SET dive_site_id` does this for free, since it
      never touches `position`.
    - **Deduped.** A dive already logged at both sites must end up holding the replacement
      once, not twice - which the `(dive_id, dive_site_id)` unique constraint would turn
      into a 500 rather than a duplicate. Hence the first statement, which drops whichever
      of the pair sits *later*, leaving the earlier slot for the survivor; that is the same
      "first occurrence wins, and its position is the one kept" rule
      `replace_dive_sites_for_dive` gets from `dict.fromkeys`.
    - **Contiguous.** Deleting that row can leave a hole (`[A, B, X]` -> positions 0 and 2),
      and every other writer numbers a dive's sites 0..n-1. The third statement renumbers
      the handful of dives a row was actually removed from. Nothing reads `position` as
      anything but a sort key today, so this buys consistency rather than a fixed bug - but
      an invariant that holds except down one path is not one.

    Soft-deleted dives are left alone, for the reasons in `reassign_dives_to_trip`, and the
    count covers every dive that referenced the doomed site - including one that only *lost*
    it because it already held the replacement.
    """
    live_dive_ids = select(Dive.id).where(Dive.user_id == user_id, Dive.is_deleted.is_(False))

    # The two rows of a dive that holds both sites, resolved to whichever one loses. Ties
    # on `position` (nothing forbids them) fall to the replacement's row, so the doomed row
    # is the one the `UPDATE` below rewrites - deterministic either way.
    doomed = aliased(DiveDiveSite)
    already_there = aliased(DiveDiveSite)
    loser_ids = (
        select(case((doomed.position > already_there.position, doomed.id), else_=already_there.id))
        .select_from(doomed)
        .join(already_there, already_there.dive_id == doomed.dive_id)
        .where(
            doomed.dive_site_id == from_dive_site_id,
            already_there.dive_site_id == to_dive_site_id,
            doomed.dive_id.in_(live_dive_ids),
        )
    )
    deduped = await db.execute(
        delete(DiveDiveSite).where(DiveDiveSite.id.in_(loser_ids)).returning(DiveDiveSite.dive_id)
    )
    deduped_dive_ids = {dive_id for (dive_id,) in deduped}

    swapped = await db.execute(
        update(DiveDiveSite)
        .where(DiveDiveSite.dive_site_id == from_dive_site_id, DiveDiveSite.dive_id.in_(live_dive_ids))
        .values(dive_site_id=to_dive_site_id)
        .returning(DiveDiveSite.dive_id)
    )
    swapped_dive_ids = {dive_id for (dive_id,) in swapped}

    if deduped_dive_ids:
        renumbered = (
            select(
                DiveDiveSite.id.label("id"),
                # `id` breaks a tie on `position` so the result does not depend on the
                # order Postgres happened to return the rows in.
                (
                    func.row_number().over(
                        partition_by=DiveDiveSite.dive_id, order_by=(DiveDiveSite.position, DiveDiveSite.id)
                    )
                    - 1
                ).label("position"),
            )
            .where(DiveDiveSite.dive_id.in_(deduped_dive_ids))
            .subquery()
        )
        await db.execute(
            update(DiveDiveSite)
            .where(DiveDiveSite.id == renumbered.c.id, DiveDiveSite.position != renumbered.c.position)
            .values(position=renumbered.c.position)
        )

    return len(deduped_dive_ids | swapped_dive_ids)
