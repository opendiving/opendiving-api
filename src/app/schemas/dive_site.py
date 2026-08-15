import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema

Latitude = Annotated[float | None, Field(default=None, ge=-90, le=90, examples=[27.8506])]
Longitude = Annotated[float | None, Field(default=None, ge=-180, le=180, examples=[34.3136])]

COORDINATE_PAIR_MESSAGE = "latitude and longitude must be set together"


class DiveSiteBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Blue Hole"])]
    location: Annotated[str | None, Field(default=None, max_length=255, examples=["Koh Tao, Thailand"])]
    latitude: Latitude
    longitude: Longitude
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


class WholeCoordinatePair(BaseModel):
    """Rejects half a position on the way in - a latitude without a longitude is not a
    partial position, it is a meaningless one.

    Two conditions, because a PATCH can produce a half pair two ways: **naming** one
    coordinate and not the other (`{"latitude": 27.7}` writes one column and leaves the
    stale other), or naming both with only one **value** (`{"latitude": 27.7,
    "longitude": null}`). Sending the pair or nothing keeps a whole row whole without the
    route ever reading the stored one - which also means two concurrent PATCHes cannot
    interleave into a half pair the way a read-then-compare check would allow.

    The rule lives on the *write* schemas only - every application path in, the admin
    panel included, goes through one of them, so only raw SQL can put a half pair in the
    table. That is reason enough to keep it off the read schemas: a row like that should
    read back as half a position rather than turn every read of it into a 500.
    """

    latitude: Latitude
    longitude: Longitude

    @model_validator(mode="after")
    def _coordinates_are_a_pair(self) -> WholeCoordinatePair:
        if len({"latitude", "longitude"} & self.model_fields_set) == 1:
            raise ValueError(COORDINATE_PAIR_MESSAGE)
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError(COORDINATE_PAIR_MESSAGE)
        return self


class DiveSiteRead(DiveSiteBase, PublicUUIDSchema):
    """Public representation of a dive site, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).
    """

    user_uuid: uuid_pkg.UUID
    created_at: datetime


class DiveSiteReadInternal(DiveSiteBase, PublicUUIDSchema):
    """Mirrors the actual `dive_site` table columns (integer PK/FK), for server-side
    lookups only - never returned directly over the API (use `DiveSiteRead` for the
    public shape, which additionally resolves `user_id` to the owning user's `uuid`).
    """

    id: int
    user_id: int
    created_at: datetime


class DiveSiteCreate(DiveSiteBase, WholeCoordinatePair):
    model_config = ConfigDict(extra="forbid")
    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this dive site belongs to")]


class DiveSiteCreateInternal(DiveSiteBase, WholeCoordinatePair):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class DiveSiteUpdate(WholeCoordinatePair):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]
    location: Annotated[str | None, Field(default=None, max_length=255, examples=["Koh Tao, Thailand"])]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]


class DiveSiteUpdateInternal(DiveSiteUpdate):
    updated_at: datetime


class DiveSiteDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
