import uuid as uuid_pkg
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.schemas import PublicUUIDSchema, RejectsExplicitNulls


class DiveFormField(StrEnum):
    """A field of the dive form a diver may hide, and the whole vocabulary of them.

    The API owns this list and validates against it: `hidden_fields` on a preset, and
    `user.dive_form_hidden_fields`, are both `list[DiveFormField]`, so an unknown name is a
    422 rather than a string stored unchecked. The clients mirror it by hand, the way they
    mirror `TankUsage` and the water types, and a **two-sided guard** keeps the mirror
    honest without a cross-repo test: the api half
    (`tests/test_dive_form_presets.py::TestTheVocabularyNamesRealFields`) asserts every
    value here names a field of `DiveCreateRequest` - or, with the `mixture.` prefix, of
    `DiveMixtureCreate` - that is *not required* there, and the web half asserts its own
    registry equals its form schema's optional keys exactly, minus a short list of keys
    that side exempts by name. API-optional is deliberately wider than what is hideable
    (`gas_number` has no input at all; a cylinder's `volume` and `oxygen` are optional on
    both sides and exempt by name on the web side), which is why this side is a subset
    check and the other is an equality check.

    **These values are stored data, not labels.** A preset row and a user's current state
    name them, so renaming a member is a data migration over `dive_form_preset.hidden_fields`
    and `user.dive_form_hidden_fields` rather than a rename.

    The `mixture.` prefix rather than react-hook-form's `mixtures.${index}.po2_limit`: a key
    names a field of *every* cylinder, not of one, and hiding it hides that input on every
    tank card.

    Declaration order is form order, and it is load-bearing twice over: it is the canonical
    order every stored list is rewritten into (`canonical_hidden_fields`), so two equal sets
    are two equal lists and a client can compare them with one loop, and it is the order the
    clients take their panel rows from.

    Fields that cannot be hidden are deliberately absent, and for two different reasons.
    `dive_number`, `start_time` and `duration` the form requires. A cylinder's `volume`
    and `oxygen` it does not - they became blank-able when a cylinder was allowed to record
    a mix with no vessel - but the web side shows both always and exempts each by name from
    the registry its equality check reads, on the ground that they are what a cylinder
    *is*. Either way there is no member here to hide them by.

    `helium` was in that second group and is not any more. It is the one of the three a
    diver can be certain about without measuring: a cylinder of air or nitrox has none, and
    a logbook that never records a trimix fill is asking a question whose answer is always
    the same. Volume and oxygen stay exempt because a cylinder that records neither says
    nothing at all; one that records no helium is a cylinder of air.
    """

    TRIP_UUID = "trip_uuid"
    COURSE_UUID = "course_uuid"
    DIVE_SITE_UUIDS = "dive_site_uuids"
    MAX_DEPTH = "max_depth"
    AVG_DEPTH = "avg_depth"
    BOTTOM_TEMPERATURE = "bottom_temperature"
    VISIBILITY = "visibility"
    WATER_TYPE = "water_type"
    ALTITUDE = "altitude"
    MIXTURES = "mixtures"
    GEAR_ITEM_UUIDS = "gear_item_uuids"
    WEIGHT = "weight"
    SPECIES_UUIDS = "species_uuids"
    NOTES = "notes"

    MIXTURE_PO2_LIMIT = "mixture.po2_limit"
    MIXTURE_HELIUM = "mixture.helium"
    MIXTURE_START_PRESSURE = "mixture.start_pressure"
    MIXTURE_END_PRESSURE = "mixture.end_pressure"
    MIXTURE_ROLE = "mixture.role"
    MIXTURE_USAGE = "mixture.usage"


# The prefix that marks a key as naming a field of every cylinder rather than of the dive.
MIXTURE_FIELD_PREFIX = "mixture."


def canonical_hidden_fields(values: Iterable[DiveFormField]) -> list[DiveFormField]:
    """Collapse duplicates and impose `DiveFormField`'s declaration order.

    Every write goes through this, on a preset and on the user column alike, so a stored
    hidden set is a *set* spelled as a list: two equal sets are two equal lists, and a client
    deciding which preset matches the current state compares element by element instead of
    building sets of its own. Callers may send any order; what comes back is form order.
    """
    present = set(values)
    return [field for field in DiveFormField if field in present]


class DiveFormPresetBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Warm water"])]
    hidden_fields: Annotated[
        list[DiveFormField],
        Field(
            default_factory=list,
            # Nothing can hide more fields than exist. The cap is on the *input* list, so
            # it is checked before duplicates collapse - a body padded with repeats is a
            # 422 rather than a large list that canonicalizes down to a small one.
            max_length=len(DiveFormField),
            description="Fields hidden by this preset, in any order - stored in form order with duplicates collapsed",
            examples=[[DiveFormField.ALTITUDE, DiveFormField.MIXTURE_PO2_LIMIT]],
        ),
    ]

    @field_validator("hidden_fields")
    @classmethod
    def _canonicalize(cls, value: list[DiveFormField]) -> list[DiveFormField]:
        return canonical_hidden_fields(value)


class DiveFormPresetRead(DiveFormPresetBase, PublicUUIDSchema):
    """Public representation of a dive form preset, keyed by its opaque `uuid` rather than
    the sequential internal `id` (which is never exposed over the API).
    """

    user_uuid: uuid_pkg.UUID
    created_at: datetime


class DiveFormPresetReadInternal(DiveFormPresetBase, PublicUUIDSchema):
    """Mirrors the actual `dive_form_preset` table columns (integer PK/FK), for server-side
    lookups only - never returned directly over the API (use `DiveFormPresetRead`, which
    additionally resolves `user_id` to the owning user's `uuid`).
    """

    id: int
    user_id: int
    created_at: datetime


class DiveFormPresetCreate(DiveFormPresetBase):
    model_config = ConfigDict(extra="forbid")

    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this preset belongs to")]


class DiveFormPresetCreateInternal(DiveFormPresetBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class DiveFormPresetUpdate(RejectsExplicitNulls):
    """Partial update: a rename, a new hidden set, or both."""

    model_config = ConfigDict(extra="forbid")

    # Both columns are `NOT NULL`. A preset that hides nothing is the empty list, never
    # null - which is what "Technical" is.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "hidden_fields")

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=255)]
    hidden_fields: Annotated[
        list[DiveFormField] | None,
        Field(default=None, max_length=len(DiveFormField), description="Replaces the preset's hidden set wholesale"),
    ]

    @field_validator("hidden_fields")
    @classmethod
    def _canonicalize(cls, value: list[DiveFormField] | None) -> list[DiveFormField] | None:
        return None if value is None else canonical_hidden_fields(value)


class DiveFormPresetUpdateInternal(DiveFormPresetUpdate):
    updated_at: datetime
