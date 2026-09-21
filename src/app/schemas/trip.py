import uuid as uuid_pkg
from datetime import date, datetime
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import (
    NOTES_MAX_LENGTH,
    PublicUUIDSchema,
    RejectsExplicitNulls,
    validate_date_range,
)
from .location import LocationInput, LocationRead

MAX_TRIP_PARTS = 20


class TripPartInput(BaseModel):
    """One stretch of a trip on the way in: an optional date range and an optional place.

    Both halves are optional and each absence means something. Dates and no location is a
    transit day or a week nobody geocoded; a location and no dates is a stop whose timing
    the diver has not filled in. A part carries no name of its own - `location.name` is
    the place's name, and a part without one is identified by its dates or its ordinal.
    """

    model_config = ConfigDict(extra="forbid")

    start_date: Annotated[date | None, Field(default=None, examples=["2024-06-01"])]
    end_date: Annotated[date | None, Field(default=None, examples=["2024-06-08"])]
    location: LocationInput | None = None

    @model_validator(mode="after")
    def check_date_range(self) -> TripPartInput:
        validate_date_range(self.start_date, self.end_date)
        return self


class TripPartRead(BaseModel):
    """Public shape of a trip part. No id: parts are replaced wholesale with the trip, so
    there is nothing to address one by."""

    start_date: date | None = None
    end_date: date | None = None
    location: LocationRead | None = None


class TripBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Red Sea Liveaboard 2024"])]
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


class TripRead(TripBase, PublicUUIDSchema):
    """Public representation of a trip, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).

    No span of its own: a trip's dates are its parts'.
    """

    parts: Annotated[list[TripPartRead], Field(default_factory=list)]
    user_uuid: uuid_pkg.UUID
    created_at: datetime


class TripReadInternal(TripBase, PublicUUIDSchema):
    """Mirrors the actual `trip` table columns (integer PK/FK), for server-side lookups
    only - never returned directly over the API (use `TripRead` for the public shape,
    which additionally resolves `user_id` to the owning user's `uuid` and embeds the parts).
    """

    id: int
    user_id: int
    created_at: datetime


class TripCreate(TripBase):
    model_config = ConfigDict(extra="forbid")

    parts: Annotated[
        list[TripPartInput],
        Field(
            default_factory=list,
            max_length=MAX_TRIP_PARTS,
            description="The stretches this trip ran, in the order the diver arranged them",
        ),
    ]


class TripCreateInternal(TripBase):
    model_config = ConfigDict(extra="forbid")
    user_id: int


class TripUpdate(RejectsExplicitNulls):
    model_config = ConfigDict(extra="forbid")

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "notes")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]


class TripUpdateRequest(TripUpdate):
    """Request body for updating a trip, including replacing its parts.

    Omit `parts` and the existing ones are left untouched; provide it - even as an empty
    list - and they are replaced wholesale with what was sent.

    Separate from `TripUpdate` rather than a field on it because `TripUpdate` is CRUDAdmin's
    Trip form schema (and the shape `test_update_explicit_nulls.py` sweeps against the
    `trip` table's columns), and `parts` is rows in another table rather than a trip column.
    """

    parts: Annotated[
        list[TripPartInput] | None,
        Field(
            default=None,
            max_length=MAX_TRIP_PARTS,
            description="The stretches this trip ran, in order. Omit to leave them unchanged.",
        ),
    ]


class TripUpdateInternal(TripUpdate):
    updated_at: datetime
