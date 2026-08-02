from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class UserDiveStatsBase(BaseModel):
    total_dives: Annotated[int, Field(default=0, examples=[42])]
    max_depth: Annotated[float, Field(default=0, examples=[30.5])]
    total_time: Annotated[int, Field(default=0, description="Total dive time in seconds", examples=[36000])]
    species_seen: Annotated[int, Field(default=0, examples=[0])]


class UserDiveStats(UserDiveStatsBase):
    user_id: int
    created_at: datetime
    updated_at: datetime | None = None


class UserDiveStatsRead(UserDiveStatsBase):
    user_id: int
    created_at: datetime


class UserDiveStatsCreateInternal(UserDiveStatsBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class UserDiveStatsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_dives: Annotated[int | None, Field(default=None)]
    max_depth: Annotated[float | None, Field(default=None)]
    total_time: Annotated[int | None, Field(default=None)]
    species_seen: Annotated[int | None, Field(default=None)]


class UserDiveStatsUpdateInternal(UserDiveStatsUpdate):
    updated_at: datetime


class UserDiveStatsDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
