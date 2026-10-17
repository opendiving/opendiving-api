from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ..crud.crud_dive_form_presets import DIVE_FORM_PRESET_NAME_KEY
from ..crud.unique_keys import unique_key_expressions
from ..models.dive_form_preset import DiveFormPreset
from ..schemas.dive_form_preset import DiveFormField, DiveFormPresetReadInternal


class DefaultPreset(NamedTuple):
    """One of the three presets every account is seeded with."""

    name: str
    hidden_fields: tuple[DiveFormField, ...]


# The three defaults, and the only *live* definition of them: the registration seed and the
# restore route read this tuple, so "default" cannot come to mean two things in anything
# running today. The revision that backfilled the accounts predating this feature
# (`e0cfbd603859`) carries a frozen copy that this one has already outgrown, and owes it
# nothing - see "The revision that backfills presets carries its own frozen copy of them"
# in DECISIONS.md before reaching for that file to "fix" the difference.
#
# They are seeded as **ordinary rows**. Nothing downstream special-cases them - a diver
# edits, renames or deletes any of the three exactly as they would one they saved
# themselves, and the only thing that remembers these names is `seed_default_presets`
# below, which adds back whichever is absent.
#
# Each set is written in `DiveFormField` declaration order, which is what
# `canonical_hidden_fields` would produce; `tests/test_dive_form_presets.py` asserts that
# rather than trusting the typing here.
DEFAULT_PRESETS: tuple[DefaultPreset, ...] = (
    DefaultPreset(
        # Keeps the required three plus dive site, maximum depth, rating, tags and notes: where,
        # how deep, and what it was like - a holiday diver's whole logbook entry. The file
        # import stays too, being the quickest way to fill that entry from a computer. Trip, course,
        # contact and people are hidden because each asks about a record the diver has to have
        # created first, bottom temperature because reading one off a computer is the point at
        # which this stops being the shortest form the app can offer, and the dive's type and
        # its conditions for the same reason. The per-cylinder keys only matter
        # once Gas Mixtures is shown again, and they are hidden so that showing it gives a
        # plain tank card rather than a technical one - helium among them, since a holiday
        # diver's cylinder holds air or nitrox and the answer is always zero, and the vessel
        # picker, a record the diver would have to have created first.
        name="Basic",
        hidden_fields=(
            DiveFormField.TRIP_UUID,
            DiveFormField.COURSE_UUID,
            DiveFormField.CONTACT_UUID,
            DiveFormField.PEOPLE,
            DiveFormField.TYPE,
            DiveFormField.AVG_DEPTH,
            DiveFormField.BOTTOM_TEMPERATURE,
            DiveFormField.AIR_TEMPERATURE,
            DiveFormField.VISIBILITY,
            DiveFormField.WATER_TYPE,
            DiveFormField.ALTITUDE,
            DiveFormField.CURRENT,
            DiveFormField.WAVES,
            DiveFormField.WEATHER,
            DiveFormField.ENTRY_TYPE,
            DiveFormField.BOAT_NAME,
            DiveFormField.MIXTURES,
            DiveFormField.GEAR_ITEM_UUIDS,
            DiveFormField.WEIGHT,
            DiveFormField.SIGHTINGS,
            DiveFormField.MIXTURE_GEAR_ITEM,
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
        # helium, which is nitrox and air's constant zero, not altitude, which almost
        # nobody dives at, and not water type, which holds for a whole trip and so is a
        # question already answered by where the diver is.
        name="Recreational",
        hidden_fields=(
            DiveFormField.WATER_TYPE,
            DiveFormField.ALTITUDE,
            DiveFormField.MIXTURE_PO2_LIMIT,
            DiveFormField.MIXTURE_HELIUM,
            DiveFormField.MIXTURE_ROLE,
            DiveFormField.MIXTURE_USAGE,
        ),
    ),
    DefaultPreset(
        # Everything but the rating and the topside conditions - the air temperature and the
        # weather describe the surface, and a technical log is about the dive. Not the empty
        # set, which the clients offer as the built-in "All": a stored row equal to it would
        # be a second name for the same form. A field the form gains later is still on screen
        # here without anybody editing it, the hidden set being what is stored.
        name="Technical",
        hidden_fields=(
            DiveFormField.AIR_TEMPERATURE,
            DiveFormField.WEATHER,
            DiveFormField.RATING,
        ),
    ),
)

# The clients' built-in preset for the empty hidden set, every field shown. It is not a row, so
# a stored preset under its name would put two of it in the menu; create and rename refuse it.
BUILT_IN_PRESET_NAME = "All"


def is_built_in_preset_name(name: str) -> bool:
    """Whether `name` is the built-in's, compared the way the unique name check compares:
    trimmed and case-insensitively.
    """
    return name.strip().lower() == BUILT_IN_PRESET_NAME.lower()


async def seed_default_presets(
    db: AsyncSession, *, user_id: int, commit: bool = True
) -> list[DiveFormPresetReadInternal]:
    """Create whichever defaults this account is missing, and return them in the order
    `DEFAULT_PRESETS` declares.

    **Add what is missing, never overwrite**, which is what makes restore idempotent and
    safe to offer whenever the diver asks: a default they edited keeps the edit, and a
    default they renamed stays under its new name with the original re-created beside it.
    "Missing" is the unique name index's answer, not a fold in Python: every default is
    inserted `ON CONFLICT DO NOTHING` on that index, so a name Postgres folds onto a stored
    one is skipped however Python would have folded it, and a concurrent restore's rows are
    simply there.

    One helper for both callers, so the account a diver registers today and the account
    they press "Restore default presets" on tomorrow get the same three things. Registration
    calls it with `commit=False` inside the transaction that creates the `User` row - either
    the account and its three presets land together or neither does; the route calls it with
    the default and commits.
    """
    now = datetime.now(UTC)
    inserted = await db.execute(
        pg_insert(DiveFormPreset)
        .values(
            [
                {
                    "user_id": user_id,
                    "name": preset.name,
                    "hidden_fields": [field.value for field in preset.hidden_fields],
                    "uuid": uuid7(),
                    "created_at": now,
                }
                for preset in DEFAULT_PRESETS
            ]
        )
        .on_conflict_do_nothing(index_elements=unique_key_expressions(DiveFormPreset, DIVE_FORM_PRESET_NAME_KEY))
        .returning(DiveFormPreset.id)
    )
    created_ids = list(inserted.scalars())
    if not created_ids:
        return []

    rows = await db.execute(select(DiveFormPreset).where(DiveFormPreset.id.in_(created_ids)))
    position = {preset.name: i for i, preset in enumerate(DEFAULT_PRESETS)}
    created = sorted(
        (DiveFormPresetReadInternal.model_validate(row, from_attributes=True) for row in rows.scalars()),
        key=lambda preset: position[preset.name],
    )
    if commit:
        await db.commit()
    return created
