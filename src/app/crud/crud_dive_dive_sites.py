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
    """Return the *live* dive sites visited during a dive, in the order they were visited.

    A soft-deleted site keeps its `dive_dive_site` rows - `erase_dive_site` flags the site
    and leaves the links alone - so without the filter a dive goes on rendering a site that
    `GET /dive-site/{uuid}` answers 404 for, and that `PATCH /dive` refuses to accept back
    (`resolve_dive_site_ids_for_user` resolves only live sites, so reading a dive's site
    list and writing it back verbatim would 422). The links stay because export still wants
    them: `_owned` in `services/export/loader.py` reads deleted-but-referenced sites back on
    purpose, flagged `is_deleted`, so the record survives where it belongs rather than here.

    They stay only until that dive's next `PATCH`, though, and this filter is what makes
    that so: a client seeding an edit form from this list submits it back one entry short,
    and `replace_dive_sites_for_dive` is a delete-and-reinsert, so the row is then gone for
    good. Accepted rather than worked around - see "The links outlive the delete, but not
    the dive's next edit" in DECISIONS.md before writing anything that relies on the row
    being there.

    Dropping a row promotes whatever follows it into the slot ahead - a dive logged at
    `[A, B]` whose A is deleted reads back as `[B]`, and B becomes the primary site every
    single-site surface shows. That is intended: `position` is a sort key, not an identity,
    and the alternative is a dive whose primary site does not exist.

    Takes a `dive_id` and no owner, unlike `get_trip_uuids_by_ids` alongside it, which was
    given a `user_id` scope as defence in depth. The asymmetry is intentional: a `dive_id`
    is not a client-supplied handle - every route resolves and ownership-checks the dive
    before reaching this - whereas the trip ids are read off dive rows in bulk, which is
    the shape a future caller could get wrong. A cross-user join row cannot be created
    through the API at all; see the note above `trip_for` in `services/export/loader.py`.
    """
    result = await db.execute(
        select(*DIVE_SITE_INFO_COLUMNS)
        .join(DiveDiveSite, DiveDiveSite.dive_site_id == DiveSite.id)
        .where(DiveDiveSite.dive_id == dive_id, DiveSite.is_deleted.is_(False))
        .order_by(DiveDiveSite.position)
    )
    return [dive_site_info_from_row(row) for row in result]


async def get_dive_sites_for_dives(db: AsyncSession, dive_ids: list[int]) -> dict[int, list[DiveSiteInfo]]:
    """Batched version of `get_dive_sites_for_dive`, e.g. for a paginated dive listing.

    Filters deleted sites for the same reasons, and needs nothing extra to degrade well:
    the per-dive lists are pre-seeded empty, so a dive whose only site is gone comes back
    with `[]` rather than dropping out of the mapping.
    """
    sites_by_dive: dict[int, list[DiveSiteInfo]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return sites_by_dive

    result = await db.execute(
        select(DiveDiveSite.dive_id, *DIVE_SITE_INFO_COLUMNS)
        .join(DiveSite, DiveSite.id == DiveDiveSite.dive_site_id)
        .where(DiveDiveSite.dive_id.in_(dive_ids), DiveSite.is_deleted.is_(False))
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

    **The wipe takes soft-deleted sites with it, and that is a known accepted loss.** Since
    `get_dive_sites_for_dive` stopped returning them, a client editing a dive's site list
    submits back only the sites it was shown - so a dive linked to a live A and a hidden B
    comes back as `["A", "C"]` when the diver adds C, and B's row is destroyed by the
    delete below. The diver never saw B and never asked to remove it, and no client can
    prevent this: it cannot preserve a reference it was never handed.

    Declined rather than missed - see "One narrower case `dirtyFields` cannot reach" in
    DECISIONS.md, which records the fix (delete only rows whose site is live, then renumber
    the survivors after the submitted list) and why the position-contiguity cost was judged
    too high for a path this narrow. Reconsider it if the balance changes - but on
    `replace_gear_items_for_set` rather than here: nothing reads `gear_set_item.position`
    as more than a sort key, whereas position 0 here *is* the primary site, so it is where
    the same fix is cheapest to try first.
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
    #
    # `doomed.id != already_there.id` is what keeps this statement correct on its own terms
    # rather than on a caller's. Without it, `from == to` makes every row join *itself*,
    # the `case` falls to `else_`, and the `DELETE` strips the site from every one of the
    # diver's dives while the `UPDATE` matches nothing - silent data loss reported as a
    # plausible count. `erase_dive_site` does reject that call with a 422, but a guard in
    # another module is not a precondition this one is entitled to assume, and the
    # comparison is free: the two aliases select different sites in every real call, so it
    # can only ever be true.
    doomed = aliased(DiveDiveSite)
    already_there = aliased(DiveDiveSite)
    loser_ids = (
        select(case((doomed.position > already_there.position, doomed.id), else_=already_there.id))
        .select_from(doomed)
        .join(already_there, already_there.dive_id == doomed.dive_id)
        .where(
            doomed.id != already_there.id,
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
