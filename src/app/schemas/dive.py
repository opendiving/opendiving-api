from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import PersistentDeletion, TimestampSchema, UUIDSchema
from .dive_mixture import DiveMixtureCreate, DiveMixtureRead


class DiveBase(BaseModel):
    dive_number: Annotated[int, Field(examples=[5])]
    start_time: Annotated[datetime, Field(examples=[datetime.now()])]
    duration: Annotated[int, Field(examples=[2048], description="Dive duration in seconds")]

    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[float | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    trip_id: Annotated[int | None, Field(default=None, description="ID of the trip this dive belongs to")]

    notes: Annotated[str, Field(default="")]


class Dive(TimestampSchema, DiveBase, UUIDSchema, PersistentDeletion):
    user_id: int


class DiveSiteInfo(BaseModel):
    id: int
    name: str
    location: str | None = None


class DiveRead(DiveBase):
    id: int
    user_id: int
    created_at: datetime
    dive_sites: Annotated[
        list[DiveSiteInfo], Field(default_factory=list, description="Dive sites visited, in the order visited")
    ]


class DiveReadWithMixtures(DiveRead):
    mixtures: Annotated[list[DiveMixtureRead], Field(default_factory=list)]


class DiveCreate(DiveBase):
    model_config = ConfigDict(extra="forbid")


class DiveCreateInternal(DiveCreate):
    user_id: int


class DiveCreateRequest(DiveCreate):
    """Request body for creating a dive, including its gas mixtures and dive site(s)."""

    mixtures: Annotated[list[DiveMixtureCreate], Field(default_factory=list)]
    dive_site_ids: Annotated[
        list[int],
        Field(default_factory=list, description="IDs of the dive sites visited, in the order visited"),
    ]


class DiveUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dive_number: Annotated[int | None, Field(examples=[5], default=None)]
    start_time: Annotated[datetime | None, Field(examples=[datetime.now()], default=None)]
    duration: Annotated[
        int | None, Field(examples=[2048], description="Dive duration in seconds", default=None)
    ]
    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[float | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    trip_id: Annotated[int | None, Field(default=None, description="ID of the trip this dive belongs to")]
    notes: Annotated[
        str | None,
        Field(
            max_length=63206,
            examples=["This is my updated dive notes."],
            default=None,
        ),
    ]


class DiveUpdateRequest(DiveUpdate):
    """Request body for updating a dive, including replacing its gas mixtures and dive site(s).

    If `mixtures`/`dive_site_ids` is omitted, the existing mixtures/dive sites are left
    untouched. If provided (even as an empty list), all existing mixtures/dive sites are
    replaced with the given list.
    """

    mixtures: Annotated[list[DiveMixtureCreate] | None, Field(default=None)]
    dive_site_ids: Annotated[
        list[int] | None,
        Field(default=None, description="IDs of the dive sites visited, in the order visited"),
    ]


class DiveUpdateInternal(DiveUpdate):
    updated_at: datetime


class DiveDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
