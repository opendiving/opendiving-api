from fastcrud import FastCRUD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.gear_set import GearSet
from ..schemas.gear_set import (
    GearSetCreateInternal,
    GearSetReadInternal,
    GearSetUpdate,
    GearSetUpdateInternal,
)

CRUDGearSet = FastCRUD[
    GearSet, GearSetCreateInternal, GearSetUpdate, GearSetUpdateInternal, GearSetUpdate, GearSetReadInternal
]
crud_gear_sets = CRUDGearSet(GearSet)


async def gear_set_name_exists(db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None) -> bool:
    """Case-insensitive check for whether a gear set with the same name already exists for
    the user. Mirrors the `ux_gear_set_user_id_name_lower` index.
    """
    stmt = select(GearSet.id).where(
        GearSet.user_id == user_id,
        func.lower(GearSet.name) == name.strip().lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(GearSet.id != exclude_id)

    result = await db.execute(stmt.limit(1))
    return result.first() is not None
