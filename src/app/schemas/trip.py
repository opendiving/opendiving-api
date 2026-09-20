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
from .dive_site import WholeCoordinatePair

MAX_TRIP_PARTS = 20

BBOX_MESSAGE = "bbox_south, bbox_north, bbox_west and bbox_east must be set together"
BBOX_NEEDS_COORDINATES_MESSAGE = "a bounding box needs latitude and longitude"
BBOX_ORDER_MESSAGE = "bbox_south must be less than or equal to bbox_north"


class TripLocationInput(WholeCoordinatePair):
    """The place half of a trip part, as the geocoder described it when the diver picked it.

    A value object, not a reference: the name is snapshotted rather than looked up, so
    nothing here resolves against a gazetteer on the way in. A location the geocoder could
    not answer for arrives as a bare `name` - that free-text escape hatch is what keeps a
    throttled provider from blocking a save.
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Moalboal"])]
    display_name: Annotated[str | None, Field(default=None, max_length=512, examples=["Moalboal, Cebu, Philippines"])]
    bbox_south: Annotated[float | None, Field(default=None, ge=-90, le=90, examples=[9.89])]
    bbox_north: Annotated[float | None, Field(default=None, ge=-90, le=90, examples=[9.98])]
    bbox_west: Annotated[float | None, Field(default=None, ge=-180, le=180, examples=[123.35])]
    bbox_east: Annotated[float | None, Field(default=None, ge=-180, le=180, examples=[123.44])]

    @model_validator(mode="after")
    def _bounding_box_is_whole(self) -> TripLocationInput:
        corners = (self.bbox_south, self.bbox_north, self.bbox_west, self.bbox_east)
        if any(corner is not None for corner in corners):
            if not all(corner is not None for corner in corners):
                raise ValueError(BBOX_MESSAGE)
            if self.latitude is None or self.longitude is None:
                raise ValueError(BBOX_NEEDS_COORDINATES_MESSAGE)
            # Only the north/south pair is ordered. West > east is a legitimate box that
            # crosses the antimeridian, and Nominatim returns those for real places -
            # rejecting it would refuse to record Fiji or the Chukchi Sea.
            if self.bbox_south is not None and self.bbox_north is not None and self.bbox_south > self.bbox_north:
                raise ValueError(BBOX_ORDER_MESSAGE)
        return self


class TripLocationRead(BaseModel):
    """Public shape of a part's place - a value object with no id of its own, because
    there is nothing to address it by: parts are replaced wholesale with the trip.
    """

    name: str
    display_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    bbox_south: float | None = None
    bbox_north: float | None = None
    bbox_west: float | None = None
    bbox_east: float | None = None


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
    location: TripLocationInput | None = None

    @model_validator(mode="after")
    def check_date_range(self) -> TripPartInput:
        validate_date_range(self.start_date, self.end_date)
        return self


class TripPartRead(BaseModel):
    """Public shape of a trip part. No id: parts are replaced wholesale with the trip, so
    there is nothing to address one by."""

    start_date: date | None = None
    end_date: date | None = None
    location: TripLocationRead | None = None


class TripBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Red Sea Liveaboard 2024"])]
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


class TripRead(TripBase, PublicUUIDSchema):
    """Public representation of a trip, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).

    `locations`, `start_date` and `end_date` are the deploy-skew shim: the web build the
    flagship was serving before this one reads all three, and the two halves deploy off
    their own pushes in an order nobody chose. They are derived from `parts` on the way
    out and `api-3` deletes them.
    """

    # `default_factory` rather than a required field, and load-bearing: `trip_cache:{uuid}`
    # entries live an hour and replay through this schema, so entries written before this
    # field existed have no `parts` key. Required, every warm read would 500 until the
    # last of them expired.
    parts: Annotated[list[TripPartRead], Field(default_factory=list)]
    locations: Annotated[list[TripLocationRead], Field(default_factory=list)]
    start_date: date | None = None
    end_date: date | None = None
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


class _LegacyTripDates(BaseModel):
    """The members the previously deployed web build sends, kept accepted rather than
    refused.

    Every write schema here is `extra="forbid"`, so without this the build the flagship is
    serving while the two halves deploy apart would take a 422 on every trip it created or
    edited. When `parts` is present it wins and these are ignored; otherwise they are
    translated into parts by the rule the migration used. `api-3` deletes this class and
    both its users.
    """

    start_date: Annotated[date | None, Field(default=None, examples=["2024-06-01"])]
    end_date: Annotated[date | None, Field(default=None, examples=["2024-06-08"])]
    locations: Annotated[
        list[TripLocationInput] | None,
        Field(default=None, max_length=MAX_TRIP_PARTS, deprecated="Send `parts` instead."),
    ]


class TripCreate(TripBase, _LegacyTripDates):
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


class TripUpdateRequest(TripUpdate, _LegacyTripDates):
    """Request body for updating a trip, including replacing its parts.

    Omit `parts` and the existing ones are left untouched; provide it - even as an empty
    list - and they are replaced wholesale with what was sent.

    Separate from `TripUpdate` rather than fields on it because `TripUpdate` is CRUDAdmin's
    Trip form schema (and the shape `test_update_explicit_nulls.py` sweeps against the
    `trip` table's columns), and none of these is a trip column.
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
