import uuid as uuid_pkg
from datetime import date, datetime
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema, RejectsExplicitNulls
from .dive_site import WholeCoordinatePair

MAX_TRIP_LOCATIONS = 20

BBOX_MESSAGE = "bbox_south, bbox_north, bbox_west and bbox_east must be set together"
BBOX_NEEDS_COORDINATES_MESSAGE = "a bounding box needs latitude and longitude"
BBOX_ORDER_MESSAGE = "bbox_south must be less than or equal to bbox_north"


def _validate_date_range(start_date: date | None, end_date: date | None) -> None:
    if start_date is not None and end_date is not None and end_date < start_date:
        raise ValueError("end_date must be on or after start_date")


class TripLocationInput(WholeCoordinatePair):
    """One place on a trip, as the geocoder described it when the diver picked it.

    A value object, not a reference: the name and position are snapshotted rather than
    looked up, so nothing here resolves against a gazetteer on the way in. A location
    the geocoder could not answer for arrives as a bare `name` - that free-text escape
    hatch is what keeps a throttled provider from blocking a save.
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
    """Public shape of a trip location - a value object with no id of its own, because
    there is nothing to address it by: locations are replaced wholesale with the trip.
    """

    name: str
    display_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    bbox_south: float | None = None
    bbox_north: float | None = None
    bbox_west: float | None = None
    bbox_east: float | None = None


class TripBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Red Sea Liveaboard 2024"])]
    start_date: Annotated[date | None, Field(default=None, examples=["2024-06-01"])]
    end_date: Annotated[date | None, Field(default=None, examples=["2024-06-08"])]
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]

    @model_validator(mode="after")
    def check_date_range(self) -> TripBase:
        _validate_date_range(self.start_date, self.end_date)
        return self


class TripRead(TripBase, PublicUUIDSchema):
    """Public representation of a trip, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).
    """

    user_uuid: uuid_pkg.UUID
    created_at: datetime
    # `default_factory` rather than a required field, and load-bearing: `trip_cache:{uuid}`
    # entries live an hour and replay through this schema, so entries written before this
    # field existed have no `locations` key. Required, every warm read would 500 until the
    # last of them expired.
    locations: Annotated[list[TripLocationRead], Field(default_factory=list)]


class TripReadInternal(TripBase, PublicUUIDSchema):
    """Mirrors the actual `trip` table columns (integer PK/FK), for server-side lookups
    only - never returned directly over the API (use `TripRead` for the public shape,
    which additionally resolves `user_id` to the owning user's `uuid`).
    """

    id: int
    user_id: int
    created_at: datetime


class TripCreate(TripBase):
    model_config = ConfigDict(extra="forbid")
    start_date: Annotated[date, Field(examples=["2024-06-01"])]
    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this trip belongs to")]
    locations: Annotated[
        list[TripLocationInput],
        Field(
            default_factory=list,
            max_length=MAX_TRIP_LOCATIONS,
            description="Places this trip went to, in the order listed",
        ),
    ]


class TripCreateInternal(TripBase):
    model_config = ConfigDict(extra="forbid")
    start_date: Annotated[date, Field(examples=["2024-06-01"])]
    user_id: int


class TripUpdate(RejectsExplicitNulls):
    model_config = ConfigDict(extra="forbid")

    # `end_date` stays off this list on purpose: an open-ended trip is a real state, so
    # clearing it back to null is a legitimate edit. `start_date` is not - a trip without
    # one has nothing to sort the list by.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "start_date", "notes")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]
    start_date: Annotated[date | None, Field(default=None)]
    end_date: Annotated[date | None, Field(default=None)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]

    @model_validator(mode="after")
    def check_date_range(self) -> TripUpdate:
        _validate_date_range(self.start_date, self.end_date)
        return self


class TripUpdateRequest(TripUpdate):
    """Request body for updating a trip, including replacing its locations.

    Omit `locations` and the existing ones are left untouched; provide it - even as an
    empty list - and they are replaced wholesale with what was sent.

    Separate from `TripUpdate` rather than a field on it because `TripUpdate` is
    CRUDAdmin's Trip form schema (and the shape `test_update_explicit_nulls.py` sweeps
    against the `trip` table's columns), and `locations` is neither a column nor
    something the admin form could render.
    """

    locations: Annotated[
        list[TripLocationInput] | None,
        Field(
            default=None,
            max_length=MAX_TRIP_LOCATIONS,
            description="Places this trip went to, in the order listed. Omit to leave them unchanged.",
        ),
    ]


class TripUpdateInternal(TripUpdate):
    updated_at: datetime
