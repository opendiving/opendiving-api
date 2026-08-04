import uuid as uuid_pkg
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema


def _validate_date_range(start_date: date | None, end_date: date | None) -> None:
    if start_date is not None and end_date is not None and end_date < start_date:
        raise ValueError("end_date must be on or after start_date")


class TripBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Red Sea Liveaboard 2024"])]
    location: Annotated[str | None, Field(default=None, max_length=255, examples=["Koh Tao, Thailand"])]
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


class TripCreateInternal(TripBase):
    model_config = ConfigDict(extra="forbid")
    start_date: Annotated[date, Field(examples=["2024-06-01"])]
    user_id: int


class TripUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]
    location: Annotated[str | None, Field(default=None, max_length=255, examples=["Koh Tao, Thailand"])]
    start_date: Annotated[date | None, Field(default=None)]
    end_date: Annotated[date | None, Field(default=None)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]

    @model_validator(mode="after")
    def check_date_range(self) -> TripUpdate:
        _validate_date_range(self.start_date, self.end_date)
        return self


class TripUpdateInternal(TripUpdate):
    updated_at: datetime


class TripDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
