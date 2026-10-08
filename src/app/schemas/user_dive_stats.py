import uuid as uuid_pkg
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class UserDiveStatsBase(BaseModel):
    total_dives: Annotated[int, Field(default=0, examples=[42])]
    max_depth: Annotated[float, Field(default=0, examples=[30.5])]
    total_time: Annotated[int, Field(default=0, description="Total dive time in seconds", examples=[36000])]
    species_seen: Annotated[int, Field(default=0, examples=[0])]


class UserDiveStatsRead(UserDiveStatsBase):
    """Public representation, keyed by the owning user's opaque `uuid` rather than the
    sequential internal `user_id` (which is never exposed over the API).
    """

    user_uuid: uuid_pkg.UUID
    created_at: datetime
    dive_site_count: Annotated[
        int, Field(description="Distinct dive sites the live dives name, at any position", examples=[12])
    ]
    first_dive_on: Annotated[
        date | None,
        Field(description="The earliest live dive's own local day; null with no dives", examples=["2014-03-08"]),
    ]
    last_dive_on: Annotated[
        date | None,
        Field(description="The latest live dive's own local day; null with no dives", examples=["2026-10-05"]),
    ]


class UserDiveStatsReadInternal(UserDiveStatsBase):
    """Mirrors the actual `user_dive_stats` table columns (integer FK), for server-side
    lookups only - never returned directly over the API (use `UserDiveStatsRead` for the
    public shape, which additionally resolves `user_id` to the owning user's `uuid`).
    """

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
