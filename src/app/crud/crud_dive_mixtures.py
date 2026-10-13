import uuid as uuid_pkg
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive import Dive
from ..models.dive_mixture import DiveMixture
from ..models.gear_item import GearItem
from ..schemas.dive_mixture import DiveMixtureCreate, DiveMixtureRead
from .crud_gear_items import GEAR_ITEM_INFO_COLUMNS, gear_item_info_from_row


def _with_vessel() -> Select[*tuple[Any, ...]]:
    """Every mixture column plus its linked vessel's summary, in one statement - the embed a
    read carries, never a query per row."""
    return select(DiveMixture, GearItem.id.label("vessel_id"), *GEAR_ITEM_INFO_COLUMNS).outerjoin(
        GearItem, GearItem.id == DiveMixture.gear_item_id
    )


def _read(row: Any) -> DiveMixtureRead:
    mixture = DiveMixtureRead.model_validate(row.DiveMixture)
    if row.vessel_id is not None:
        mixture.gear_item = gear_item_info_from_row(row)
    return mixture


async def get_mixtures_for_dive(db: AsyncSession, dive_id: int) -> list[DiveMixtureRead]:
    """A dive's cylinders, in the order they were saved.

    The `ORDER BY` is load-bearing, not cosmetic. `replace_mixtures_for_dive` adds rows in
    list order, so ascending `id` *is* the order the diver arranged their cylinders in -
    but without an explicit sort Postgres is free to return heap order, which stops
    matching insertion order as soon as any row is updated in place. `backfill_tech_fields`
    issues exactly such an `UPDATE`, so an unordered read here would come back in a
    different order on the run after a backfill than it did on the run before it.

    `merge_mixture_fields` joins parsed cylinders to stored ones **by position**, and
    `pair_cylinders` falls back to position wherever no mix pairs them; they are the callers
    that would silently corrupt data without this - see the first one's docstring.
    """
    result = await db.execute(_with_vessel().where(DiveMixture.dive_id == dive_id).order_by(DiveMixture.id))
    return [_read(row) for row in result]


async def get_mixtures_for_dives(db: AsyncSession, dive_ids: list[int]) -> dict[int, list[DiveMixtureRead]]:
    """Batched version of `get_mixtures_for_dive`, in the same shape as
    `get_dive_sites_for_dives`/`get_gear_items_for_dives`.

    Every requested id gets a key, so a dive with no mixtures at all reads as an empty
    list rather than a `KeyError` at the call site.

    Ordered by `id` for the same reason as `get_mixtures_for_dive`, so a dive's cylinders
    come back in the same order whether they were fetched one dive at a time or in a batch.
    Without it the list and detail endpoints could disagree about which tank is first.
    """
    mixtures_by_dive: dict[int, list[DiveMixtureRead]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return mixtures_by_dive

    result = await db.execute(_with_vessel().where(DiveMixture.dive_id.in_(dive_ids)).order_by(DiveMixture.id))
    for row in result:
        mixtures_by_dive[row.DiveMixture.dive_id].append(_read(row))
    return mixtures_by_dive


async def replace_mixtures_for_dive(
    db: AsyncSession,
    dive_id: int,
    mixtures: list[DiveMixtureCreate],
    commit: bool = True,
    *,
    gear_item_ids: Mapping[uuid_pkg.UUID, int] | None = None,
) -> None:
    """Replace all gas mixtures for a dive with the given list.

    `gear_item_ids` maps each linked vessel's public uuid to its id, as the caller resolved
    it. A link it does not map is written unlinked: the routes refuse an unresolved uuid
    before they get here, and the internal rebuilds resolve the owner's own items, so a miss
    is an item deleted in between, which unlinks the cylinder exactly as the delete's
    `SET NULL` would have.
    """
    ids = gear_item_ids or {}
    await db.execute(delete(DiveMixture).where(DiveMixture.dive_id == dive_id))
    for mixture in mixtures:
        gear_item_id = ids.get(mixture.gear_item_uuid) if mixture.gear_item_uuid is not None else None
        db.add(
            DiveMixture(dive_id=dive_id, gear_item_id=gear_item_id, **mixture.model_dump(exclude={"gear_item_uuid"}))
        )
    if commit:
        await db.commit()


async def vessel_ids_for_dive(
    db: AsyncSession, mixtures: Iterable[DiveMixtureCreate], *, dive_id: int
) -> dict[uuid_pkg.UUID, int]:
    """The ids of the dive owner's items the given cylinders link, for the internal rebuilds -
    a merge, a recording attach, an import's attach - whose links come off stored rows or a
    resolved document and so name the owner's own items. An item gone in between is simply
    absent (see `replace_mixtures_for_dive`)."""
    uuids = {mixture.gear_item_uuid for mixture in mixtures if mixture.gear_item_uuid is not None}
    if not uuids:
        return {}
    owner = select(Dive.user_id).where(Dive.id == dive_id).scalar_subquery()
    result = await db.execute(
        select(GearItem.uuid, GearItem.id).where(GearItem.uuid.in_(uuids), GearItem.user_id == owner)
    )
    return {row.uuid: row.id for row in result}


async def get_recent_volumes(db: AsyncSession, *, user_id: int, bound: datetime | None, limit: int) -> list[float]:
    """The distinct cylinder volumes on the caller's live dives at or before `bound`, the one a
    dive used most recently first, then the smaller.

    Compared as stored, with no rounding of its own: the readers already keep two places
    (`dive_reader._mixture`), and a volume typed in is the volume the diver means.
    """
    statement = (
        select(DiveMixture.volume)
        .join(Dive, Dive.id == DiveMixture.dive_id)
        .where(Dive.user_id == user_id, Dive.is_deleted.is_(False), DiveMixture.volume.is_not(None))
        .group_by(DiveMixture.volume)
        .order_by(func.max(Dive.start_time).desc(), DiveMixture.volume.asc())
        .limit(limit)
    )
    if bound is not None:
        statement = statement.where(Dive.start_time <= bound)
    return cast(list[float], list((await db.scalars(statement)).all()))


async def get_linked_gear_item_ids(db: AsyncSession, dive_id: int) -> set[int]:
    """The items a dive's stored cylinders link, on whichever tank."""
    result = await db.scalars(
        select(DiveMixture.gear_item_id).where(DiveMixture.dive_id == dive_id, DiveMixture.gear_item_id.is_not(None))
    )
    return {gear_item_id for gear_item_id in result.all() if gear_item_id is not None}


async def set_volume_of_linked_cylinders(db: AsyncSession, *, gear_item_id: int, volume: float) -> None:
    """Write a vessel's corrected capacity to every cylinder linked to it, uncommitted - the
    caller's gear-item update commits both together. Touches nothing but `volume`."""
    await db.execute(update(DiveMixture).where(DiveMixture.gear_item_id == gear_item_id).values(volume=volume))
