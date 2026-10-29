import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import PublicUUIDSchema, RejectsExplicitNulls, StoredName, Trimmed
from .gear_item import GearItemInfo

_NAME_DESCRIPTION = "Name of this gear set"


class GearSetBase(BaseModel):
    name: Annotated[str, Trimmed, Field(max_length=255, examples=["Sidemount"], description=_NAME_DESCRIPTION)]
    weight: Annotated[
        float | None,
        Field(
            default=None,
            ge=0,
            examples=[6.0],
            description="Ballast normally carried with this set, in kilograms - prefills a dive's weight",
        ),
    ]


class GearSetRead(GearSetBase, PublicUUIDSchema):
    """Public representation of a gear set, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API), with its member
    items embedded in the order they were added.
    """

    name: StoredName
    user_uuid: uuid_pkg.UUID
    created_at: datetime
    gear_items: Annotated[list[GearItemInfo], Field(default_factory=list, description="Items in the set, in order")]


class GearSetReadInternal(GearSetBase, PublicUUIDSchema):
    """Mirrors the actual `gear_set` table columns (integer PK/FK), for server-side
    lookups only - never returned directly over the API (use `GearSetRead`).
    """

    name: StoredName
    id: int
    user_id: int
    created_at: datetime


class GearSetCreateInternal(GearSetBase):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    user_id: int


class GearSetCreateRequest(GearSetBase):
    """Request body for creating a gear set, including its member items."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    gear_item_uuids: Annotated[
        list[uuid_pkg.UUID], Field(default_factory=list, description="Public ids of the items in the set, in order")
    ]


class GearSetUpdate(RejectsExplicitNulls):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # `weight` is nullable - a set that no longer prefills a ballast figure is a real
    # state, so clearing it stays allowed.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name",)

    name: Annotated[Annotated[str, Trimmed] | None, Field(max_length=255, default=None, description=_NAME_DESCRIPTION)]
    weight: Annotated[
        float | None, Field(default=None, ge=0, description="Ballast normally carried with this set, in kilograms")
    ]


class GearSetUpdateRequest(GearSetUpdate):
    """Request body for updating a gear set, including replacing its member items.

    If `gear_item_uuids` is omitted the set's items are left untouched; if provided
    (even as an empty list) they replace the set's current items wholesale.
    """

    gear_item_uuids: Annotated[
        list[uuid_pkg.UUID] | None,
        Field(default=None, description="Public ids of the items in the set, in order"),
    ]


class GearSetUpdateInternal(GearSetUpdate):
    updated_at: datetime
