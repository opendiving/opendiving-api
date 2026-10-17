from fastcrud import FastCRUD
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive_form_preset import DiveFormPreset
from ..schemas.dive_form_preset import (
    DiveFormPresetCreateInternal,
    DiveFormPresetReadInternal,
    DiveFormPresetUpdate,
    DiveFormPresetUpdateInternal,
)
from .unique_keys import unique_key_taken

CRUDDiveFormPreset = FastCRUD[
    DiveFormPreset,
    DiveFormPresetCreateInternal,
    DiveFormPresetUpdate,
    DiveFormPresetUpdateInternal,
    DiveFormPresetUpdate,
    DiveFormPresetReadInternal,
]
crud_dive_form_presets = CRUDDiveFormPreset(DiveFormPreset)


async def dive_form_preset_name_exists(
    db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None
) -> bool:
    """Asked on `ux_dive_form_preset_user_id_name_lower`'s own expressions."""
    return await unique_key_taken(
        db,
        DiveFormPreset,
        "ux_dive_form_preset_user_id_name_lower",
        {"user_id": user_id, "name": name},
        exclude_id=exclude_id,
    )


async def dive_form_preset_names_for_user(db: AsyncSession, user_id: int) -> list[str]:
    """Every preset name this account holds, for deciding which defaults are missing.

    One query rather than a `dive_form_preset_name_exists` per default: restore asks about
    all three at once, and the comparison it makes is case-insensitive against names it
    already has in hand.
    """
    rows = await db.execute(select(DiveFormPreset.name).where(DiveFormPreset.user_id == user_id))
    return list(rows.scalars().all())
