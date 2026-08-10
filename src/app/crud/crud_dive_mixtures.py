from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive_mixture import DiveMixture
from ..schemas.dive_mixture import DiveMixtureCreate, DiveMixtureRead


async def get_mixtures_for_dive(db: AsyncSession, dive_id: int) -> list[DiveMixtureRead]:
    result = await db.execute(select(DiveMixture).where(DiveMixture.dive_id == dive_id))
    return [DiveMixtureRead.model_validate(row) for row in result.scalars().all()]


async def get_mixtures_for_dives(db: AsyncSession, dive_ids: list[int]) -> dict[int, list[DiveMixtureRead]]:
    """Batched version of `get_mixtures_for_dive`, in the same shape as
    `get_dive_sites_for_dives`/`get_gear_items_for_dives`.

    Every requested id gets a key, so a dive with no mixtures at all reads as an empty
    list rather than a `KeyError` at the call site.
    """
    mixtures_by_dive: dict[int, list[DiveMixtureRead]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return mixtures_by_dive

    result = await db.execute(select(DiveMixture).where(DiveMixture.dive_id.in_(dive_ids)))
    for mixture in result.scalars().all():
        mixtures_by_dive[mixture.dive_id].append(DiveMixtureRead.model_validate(mixture))
    return mixtures_by_dive


async def replace_mixtures_for_dive(
    db: AsyncSession, dive_id: int, mixtures: list[DiveMixtureCreate], commit: bool = True
) -> None:
    """Replace all gas mixtures for a dive with the given list."""
    await db.execute(delete(DiveMixture).where(DiveMixture.dive_id == dive_id))
    for mixture in mixtures:
        db.add(DiveMixture(dive_id=dive_id, **mixture.model_dump()))
    if commit:
        await db.commit()
