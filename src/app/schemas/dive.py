from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import PersistentDeletion, TimestampSchema, UUIDSchema


class DiveBase(BaseModel):
    dive_number: Annotated[int, Field(examples=[5])]
    start_time: Annotated[datetime, Field(examples=[datetime.now()])]
    duration: Annotated[int, Field(examples=[2048], description="Dive duration in seconds")]

    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[int | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]

    notes: Annotated[str, Field(default="")]


class Dive(TimestampSchema, DiveBase, UUIDSchema, PersistentDeletion):
    user_id: int


class DiveRead(DiveBase):
    id: int
    user_id: int
    created_at: datetime


class DiveCreate(DiveBase):
    model_config = ConfigDict(extra="forbid")


class DiveCreateInternal(DiveCreate):
    user_id: int


class DiveUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dive_number: Annotated[int | None, Field(examples=[5], default=None)]
    start_time: Annotated[datetime | None, Field(examples=[datetime.now()], default=None)]
    duration: Annotated[
        int | None, Field(examples=[2048], description="Dive duration in seconds", default=None)
    ]
    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[int | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    notes: Annotated[
        str | None,
        Field(
            max_length=63206,
            examples=["This is my updated dive notes."],
            default=None,
        ),
    ]


class DiveUpdateInternal(DiveUpdate):
    updated_at: datetime


class DiveDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
