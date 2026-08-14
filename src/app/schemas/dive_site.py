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
    """Rejects half a position on the way in.

    A latitude without a longitude is not a partial position, it is a meaningless one. The
    rule lives on the *write* schemas only: nothing but this API writes the columns, but
    the database can still hold a half pair (there is no CHECK constraint behind this),
    and a read that 500s on one would be worse than a read that shows it. `patch_dive_site`
    enforces the same rule against the *effective* pair, since a PATCH body only carries
    the half that changed.
    """

    latitude: Latitude
    longitude: Longitude

    @model_validator(mode="after")
    def _coordinates_are_a_pair(self) -> WholeCoordinatePair:
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


class DiveSiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]
    location: Annotated[str | None, Field(default=None, max_length=255, examples=["Koh Tao, Thailand"])]
    # No pair validator here: an omitted coordinate means "unchanged", so whether the
    # result is a whole pair can only be decided against the stored row - `patch_dive_site`
    # does that.
    latitude: Latitude
    longitude: Longitude
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]


class DiveSiteUpdateInternal(DiveSiteUpdate):
    updated_at: datetime


class DiveSiteDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
