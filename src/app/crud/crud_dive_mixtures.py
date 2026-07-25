from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive_mixture import DiveMixture
from ..schemas.dive_mixture import DiveMixtureCreate, DiveMixtureRead


async def get_mixtures_for_dive(db: AsyncSession, dive_id: int) -> list[DiveMixtureRead]:
    result = await db.execute(select(DiveMixture).where(DiveMixture.dive_id == dive_id))
    return [DiveMixtureRead.model_validate(row) for row in result.scalars().all()]


async def replace_mixtures_for_dive(
    db: AsyncSession, dive_id: int, mixtures: list[DiveMixtureCreate], commit: bool = True
) -> None:
    """Replace all gas mixtures for a dive with the given list."""
    await db.execute(delete(DiveMixture).where(DiveMixture.dive_id == dive_id))
    for mixture in mixtures:
        db.add(DiveMixture(dive_id=dive_id, **mixture.model_dump()))
    if commit:
        await db.commit()
