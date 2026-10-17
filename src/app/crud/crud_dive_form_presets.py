from fastcrud import FastCRUD
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


DIVE_FORM_PRESET_NAME_KEY = "ux_dive_form_preset_user_id_name_lower"


async def dive_form_preset_name_exists(
    db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None
) -> bool:
    """Asked on `ux_dive_form_preset_user_id_name_lower`'s own expressions."""
    return await unique_key_taken(
        db,
        DiveFormPreset,
        DIVE_FORM_PRESET_NAME_KEY,
        {"user_id": user_id, "name": name},
        exclude_id=exclude_id,
    )
