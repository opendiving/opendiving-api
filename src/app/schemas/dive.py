import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema
from .dive_mixture import DiveMixtureCreate, DiveMixtureRead


class DiveBase(BaseModel):
    dive_number: Annotated[int, Field(examples=[5])]
    start_time: Annotated[datetime, Field(examples=[datetime.now()])]
    duration: Annotated[int, Field(examples=[2048], description="Dive duration in seconds")]

    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[float | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]

    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


class DiveSiteInfo(PublicUUIDSchema):
    """Summary of a dive site visited during a dive, keyed by its public `uuid`."""

    name: str
    location: str | None = None


class DiveRead(DiveBase, PublicUUIDSchema):
    """Public representation of a dive, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API). Cross-resource
    references (owning user, trip) are likewise exposed via their `uuid`.
    """

    user_uuid: uuid_pkg.UUID
    trip_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the trip this dive belongs to")
    ]
    created_at: datetime
    dive_sites: Annotated[
        list[DiveSiteInfo], Field(default_factory=list, description="Dive sites visited, in the order visited")
    ]


class DiveReadInternal(DiveBase, PublicUUIDSchema):
    """Mirrors the actual `dive` table columns (integer PK/FK), for server-side lookups
    only - never returned directly over the API (use `DiveRead`/`DiveReadWithMixtures`
    for the public shape, which additionally resolves `user_id`/`trip_id` to the owning
    user's/trip's `uuid` and attaches the dive's sites).
    """

    id: int
    user_id: int
    trip_id: int | None = None
    created_at: datetime


class DiveReadWithMixtures(DiveRead):
    mixtures: Annotated[list[DiveMixtureRead], Field(default_factory=list)]


class DiveCreate(DiveBase):
    model_config = ConfigDict(extra="forbid")

    trip_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the trip this dive belongs to")
    ]


class DiveCreateInternal(DiveBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    trip_id: int | None = None


class DiveCreateRequest(DiveCreate):
    """Request body for creating a dive, including its gas mixtures and dive site(s)."""

    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this dive belongs to")]
    mixtures: Annotated[list[DiveMixtureCreate], Field(default_factory=list)]
    dive_site_uuids: Annotated[
        list[uuid_pkg.UUID],
        Field(default_factory=list, description="Public ids of the dive sites visited, in the order visited"),
    ]


class DiveUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dive_number: Annotated[int | None, Field(examples=[5], default=None)]
    start_time: Annotated[datetime | None, Field(examples=[datetime.now()], default=None)]
    duration: Annotated[int | None, Field(examples=[2048], description="Dive duration in seconds", default=None)]
    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[float | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    trip_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the trip this dive belongs to")
    ]
    notes: Annotated[
        str | None,
        Field(
            max_length=NOTES_MAX_LENGTH,
            examples=["This is my updated dive notes."],
            default=None,
        ),
    ]


class DiveUpdateRequest(DiveUpdate):
    """Request body for updating a dive, including replacing its gas mixtures and dive site(s).

    If `mixtures`/`dive_site_uuids` is omitted, the existing mixtures/dive sites are left
    untouched. If provided (even as an empty list), all existing mixtures/dive sites are
    replaced with the given list.
    """

    mixtures: Annotated[list[DiveMixtureCreate] | None, Field(default=None)]
    dive_site_uuids: Annotated[
        list[uuid_pkg.UUID] | None,
        Field(default=None, description="Public ids of the dive sites visited, in the order visited"),
    ]


class DiveUpdateInternal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dive_number: Annotated[int | None, Field(examples=[5], default=None)]
    start_time: Annotated[datetime | None, Field(examples=[datetime.now()], default=None)]
    duration: Annotated[int | None, Field(examples=[2048], description="Dive duration in seconds", default=None)]
    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[float | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    trip_id: Annotated[int | None, Field(default=None, description="Internal id of the trip this dive belongs to")]
    notes: Annotated[
        str | None,
        Field(
            max_length=NOTES_MAX_LENGTH,
            examples=["This is my updated dive notes."],
            default=None,
        ),
    ]
    updated_at: datetime


class DiveDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
