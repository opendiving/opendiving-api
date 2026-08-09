import uuid as uuid_pkg
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema


class GearType(StrEnum):
    """Broad category a gear item falls into.

    A closed vocabulary rather than free text so the same kind of kit is named
    the same way across a diver's whole list (no "Fins"/"fins"/"Fin" drift),
    which is what lets the UI group and filter by it. `OTHER` is the escape
    hatch for anything genuinely unusual.

    Declaring the members in the order kit is normally listed rather than
    alphabetically keeps that order available to any caller that wants to sort
    by it. This is the single source of truth for the vocabulary - it is
    deliberately *not* mirrored by a DB `CHECK` constraint (see DECISIONS.md).
    """

    MASK = "mask"
    SNORKEL = "snorkel"
    FINS = "fins"
    WETSUIT = "wetsuit"
    DRYSUIT = "drysuit"
    HOOD = "hood"
    GLOVES = "gloves"
    BOOTS = "boots"
    BCD = "bcd"
    REGULATOR = "regulator"
    COMPUTER = "computer"
    CYLINDER = "cylinder"
    WEIGHTS = "weights"
    LIGHT = "light"
    SMB = "smb"
    REEL = "reel"
    KNIFE = "knife"
    COMPASS = "compass"
    CAMERA = "camera"
    OTHER = "other"


class GearItemBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["MK25 EVO / S620Ti"])]
    brand: Annotated[str | None, Field(default=None, max_length=255, examples=["Scubapro"])]
    type: Annotated[
        GearType | None,
        Field(default=None, examples=[GearType.REGULATOR], description="Broad category this item falls into"),
    ]
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]
    rented: Annotated[bool, Field(default=False, description="Whether this item is rented rather than owned")]


class GearItemInfo(PublicUUIDSchema):
    """Summary of a gear item as embedded in a dive or a gear set, keyed by its public
    `uuid`. Lives here rather than in `dive.py`/`gear_set.py` because both embed it.

    `is_archived` is included so the UI can flag gear that a dive/set still references
    but which no longer shows up in the dive form's picker.
    """

    name: str
    brand: str | None = None
    type: GearType | None = None
    rented: bool = False
    is_archived: bool = False


class GearItemRead(GearItemBase, PublicUUIDSchema):
    """Public representation of a gear item, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).
    """

    user_uuid: uuid_pkg.UUID
    is_archived: bool = False
    archived_at: datetime | None = None
    dive_count: Annotated[int, Field(default=0, description="Number of the owner's dives this item was used on")]
    created_at: datetime


class GearItemReadInternal(GearItemBase, PublicUUIDSchema):
    """Mirrors the actual `gear_item` table columns (integer PK/FK), for server-side
    lookups only - never returned directly over the API (use `GearItemRead` for the
    public shape, which additionally resolves `user_id` to the owning user's `uuid`).
    """

    id: int
    user_id: int
    is_archived: bool = False
    archived_at: datetime | None = None
    dive_count: int = 0
    created_at: datetime


class GearItemCreate(GearItemBase):
    model_config = ConfigDict(extra="forbid")

    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this gear item belongs to")]


class GearItemCreateInternal(GearItemBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class GearItemUpdate(BaseModel):
    """Partial update. `is_archived` doubles as the archive/unarchive control - the API
    derives `archived_at` from it rather than letting callers set the timestamp directly.
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]
    brand: Annotated[str | None, Field(default=None, max_length=255)]
    type: Annotated[GearType | None, Field(default=None)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]
    rented: Annotated[bool | None, Field(default=None)]
    is_archived: Annotated[bool | None, Field(default=None, description="Set to archive/unarchive this item")]


class GearItemUpdateInternal(GearItemUpdate):
    archived_at: datetime | None = None
    updated_at: datetime


class GearItemDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
