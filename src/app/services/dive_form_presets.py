from collections.abc import Iterable
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.crud_dive_form_presets import crud_dive_form_presets, dive_form_preset_names_for_user
from ..schemas.dive_form_preset import (
    DiveFormField,
    DiveFormPresetCreateInternal,
    DiveFormPresetReadInternal,
)


class DefaultPreset(NamedTuple):
    """One of the three presets every account is seeded with."""

    name: str
    hidden_fields: tuple[DiveFormField, ...]


# The three defaults, and the only definition of them: the registration seed, the restore
# route and the revision that backfilled the accounts predating this feature all describe
# the same three sets, so "default" cannot come to mean two things.
#
# They are seeded as **ordinary rows**. Nothing downstream special-cases them - a diver
# edits, renames or deletes any of the three exactly as they would one they saved
# themselves, and the only thing that remembers these names is `missing_default_presets`
# below, which adds back whichever is absent.
#
# Each set is written in `DiveFormField` declaration order, which is what
# `canonical_hidden_fields` would produce; `tests/test_dive_form_presets.py` asserts that
# rather than trusting the typing here.
DEFAULT_PRESETS: tuple[DefaultPreset, ...] = (
    DefaultPreset(
        # Keeps the required three plus trip, dive site, maximum depth, bottom temperature
        # and notes: a holiday diver's whole logbook entry. The six per-cylinder keys only
        # matter once Gas Mixtures is shown again, and they are hidden so that showing it
        # gives a plain tank card rather than a technical one - helium among them, since a
        # holiday diver's cylinder holds air or nitrox and the answer is always zero.
        name="Basic",
        hidden_fields=(
            DiveFormField.COURSE_UUID,
            DiveFormField.AVG_DEPTH,
            DiveFormField.VISIBILITY,
            DiveFormField.WATER_TYPE,
            DiveFormField.ALTITUDE,
            DiveFormField.MIXTURES,
            DiveFormField.GEAR_ITEM_UUIDS,
            DiveFormField.WEIGHT,
            DiveFormField.SPECIES_UUIDS,
            DiveFormField.MIXTURE_PO2_LIMIT,
            DiveFormField.MIXTURE_HELIUM,
            DiveFormField.MIXTURE_START_PRESSURE,
            DiveFormField.MIXTURE_END_PRESSURE,
            DiveFormField.MIXTURE_ROLE,
            DiveFormField.MIXTURE_USAGE,
        ),
    ),
    DefaultPreset(
        # Gas is on screen with its pressures - a recreational diver logs a cylinder and
        # what it read - but not the planning fields a ppO2 limit and a gas role are, not
        # helium, which is nitrox and air's constant zero, and not altitude, which almost
        # nobody dives at.
        name="Recreational",
        hidden_fields=(
            DiveFormField.ALTITUDE,
            DiveFormField.MIXTURE_PO2_LIMIT,
            DiveFormField.MIXTURE_HELIUM,
            DiveFormField.MIXTURE_ROLE,
            DiveFormField.MIXTURE_USAGE,
        ),
    ),
    # The empty set, deliberately - what is stored is the hidden set, so "everything" is
    # "nothing hidden" and a field the form gains later is on screen under this preset
    # without anybody editing it.
    DefaultPreset(name="Technical", hidden_fields=()),
)


def missing_default_presets(existing_names: Iterable[str]) -> list[DefaultPreset]:
    """Which of the three defaults no current preset carries, compared case-insensitively.

    **Add what is missing, never overwrite**, which is what makes restore idempotent and
    safe to offer whenever the diver asks: a default they edited keeps the edit, and a
    default they renamed stays under its new name with the original re-created beside it.
    The comparison is on the name for the same reason the unique index is - it is the only
    identity these rows have, and matching on a hidden set would resurrect nothing once the
    diver changed one.
    """
    taken = {name.strip().lower() for name in existing_names}
    return [preset for preset in DEFAULT_PRESETS if preset.name.lower() not in taken]


async def seed_default_presets(
    db: AsyncSession, *, user_id: int, commit: bool = True
) -> list[DiveFormPresetReadInternal]:
    """Create whichever defaults this account is missing, and return them in the order
    `DEFAULT_PRESETS` declares.

    One helper for both callers, so the account a diver registers today and the account
    they press "Restore default presets" on tomorrow get the same three things. Registration
    calls it with `commit=False` inside the transaction that creates the `User` row - either
    the account and its three presets land together or neither does; the route calls it with
    the default and commits.
    """
    missing = missing_default_presets(await dive_form_preset_names_for_user(db, user_id))

    created: list[DiveFormPresetReadInternal] = []
    for preset in missing:
        created.append(
            await crud_dive_form_presets.create(
                db=db,
                object=DiveFormPresetCreateInternal(
                    user_id=user_id, name=preset.name, hidden_fields=list(preset.hidden_fields)
                ),
                commit=False,
                schema_to_select=DiveFormPresetReadInternal,
                return_as_model=True,
            )
        )

    if commit and created:
        await db.commit()

    return created
