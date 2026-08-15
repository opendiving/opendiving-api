"""Admin-panel schemas for the `trip_location` table.

The public API never takes a body into these: a trip's locations arrive nested in
`TripCreate`/`TripUpdateRequest` as `TripLocationInput` (see `schemas/trip.py`) and are
replaced wholesale. These exist so CRUDAdmin can render the rows.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class TripLocationBase(BaseModel):
    trip_id: Annotated[int, Field(examples=[1], description="ID of the trip")]
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Moalboal"])]
    position: Annotated[int, Field(default=0, examples=[0], description="Order the location was listed in (0 = first)")]
    display_name: Annotated[str | None, Field(default=None, max_length=512, examples=["Moalboal, Cebu, Philippines"])]
    latitude: Annotated[float | None, Field(default=None, ge=-90, le=90, examples=[9.9367])]
    longitude: Annotated[float | None, Field(default=None, ge=-180, le=180, examples=[123.3958])]
    bbox_south: Annotated[float | None, Field(default=None, ge=-90, le=90)]
    bbox_north: Annotated[float | None, Field(default=None, ge=-90, le=90)]
    bbox_west: Annotated[float | None, Field(default=None, ge=-180, le=180)]
    bbox_east: Annotated[float | None, Field(default=None, ge=-180, le=180)]


class TripLocationReadInternal(TripLocationBase):
    """Mirrors the actual `trip_location` table columns (integer PK/FK).

    Nothing reads it today - `admin/views.py` registers only the create and update
    schemas, and every server-side read goes through `TripLocationRead` in
    `schemas/trip.py`, the bare value object with no ids on it at all. Kept for the same
    reason `DiveDiveSiteRead` is: a table's schema module carries the full set, so the
    next thing that needs the internal shape finds it rather than inventing a second one.
    """

    id: int


class TripLocationCreate(TripLocationBase):
    model_config = ConfigDict(extra="forbid")


class TripLocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trip_id: Annotated[int | None, Field(default=None, description="ID of the trip")]
    name: Annotated[str | None, Field(default=None, min_length=1, max_length=255)]
    position: Annotated[int | None, Field(default=None, description="Order the location was listed in (0 = first)")]
    display_name: Annotated[str | None, Field(default=None, max_length=512)]
    latitude: Annotated[float | None, Field(default=None, ge=-90, le=90)]
    longitude: Annotated[float | None, Field(default=None, ge=-180, le=180)]
    bbox_south: Annotated[float | None, Field(default=None, ge=-90, le=90)]
    bbox_north: Annotated[float | None, Field(default=None, ge=-90, le=90)]
    bbox_west: Annotated[float | None, Field(default=None, ge=-180, le=180)]
    bbox_east: Annotated[float | None, Field(default=None, ge=-180, le=180)]


class TripLocationDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
