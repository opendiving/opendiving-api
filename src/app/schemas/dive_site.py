import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema


class DiveSiteBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Blue Hole"])]
    location: Annotated[str | None, Field(default=None, max_length=255, examples=["Koh Tao, Thailand"])]
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


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


class DiveSiteCreate(DiveSiteBase):
    model_config = ConfigDict(extra="forbid")
    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this dive site belongs to")]


class DiveSiteCreateInternal(DiveSiteBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class DiveSiteUpdate(BaseModel):
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
