from fastcrud import FastCRUD
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.gear_set import GearSet
from ..schemas.gear_set import (
    GearSetCreateInternal,
    GearSetReadInternal,
    GearSetUpdate,
    GearSetUpdateInternal,
)
from .unique_keys import unique_key_taken

CRUDGearSet = FastCRUD[
    GearSet, GearSetCreateInternal, GearSetUpdate, GearSetUpdateInternal, GearSetUpdate, GearSetReadInternal
]
crud_gear_sets = CRUDGearSet(GearSet)


async def gear_set_name_exists(db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None) -> bool:
    """Asked on `ux_gear_set_user_id_name_lower`'s own expressions."""
    return await unique_key_taken(
        db, GearSet, "ux_gear_set_user_id_name_lower", {"user_id": user_id, "name": name}, exclude_id=exclude_id
    )
