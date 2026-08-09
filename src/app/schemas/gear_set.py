import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import PublicUUIDSchema
from .gear_item import GearItemInfo


class GearSetBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Sidemount"])]


class GearSetRead(GearSetBase, PublicUUIDSchema):
    """Public representation of a gear set, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API), with its member
    items embedded in the order they were added.
    """

    user_uuid: uuid_pkg.UUID
    created_at: datetime
    gear_items: Annotated[list[GearItemInfo], Field(default_factory=list, description="Items in the set, in order")]


class GearSetReadInternal(GearSetBase, PublicUUIDSchema):
    """Mirrors the actual `gear_set` table columns (integer PK/FK), for server-side
    lookups only - never returned directly over the API (use `GearSetRead`).
    """

    id: int
    user_id: int
    created_at: datetime


class GearSetCreateInternal(GearSetBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class GearSetCreateRequest(GearSetBase):
    """Request body for creating a gear set, including its member items."""

    model_config = ConfigDict(extra="forbid")

    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this gear set belongs to")]
    gear_item_uuids: Annotated[
        list[uuid_pkg.UUID], Field(default_factory=list, description="Public ids of the items in the set, in order")
    ]


class GearSetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]


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


class GearSetDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
