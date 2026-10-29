import uuid as uuid_pkg
from datetime import datetime
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import (
    NOTES_MAX_LENGTH,
    BlankIsNone,
    PublicUUIDSchema,
    RejectsExplicitNulls,
    StoredName,
    StoredVocabulary,
    Trimmed,
)
from .gear_service import GearServiceScheduleInfo


class GearType(StrEnum):
    """Broad category a gear item falls into.

    A closed vocabulary rather than free text so the same kind of kit is named
    the same way across a diver's whole list (no "Fins"/"fins"/"Fin" drift),
    which is what lets the UI group and filter by it. `OTHER` is the escape
    hatch for anything genuinely unusual.

    Declaring the members in the order kit is normally listed rather than
    alphabetically keeps that order available to any caller that wants to sort
    by it. This is the single source of truth for the vocabulary *within this
    codebase* - it is deliberately *not* mirrored by a DB `CHECK` constraint
    (see DECISIONS.md).

    The members and their order are DiveJSON 1.0's `gear_item.type` enum,
    value for value. Adding a member here without the format having it, or
    reordering these, breaks that parity - see DECISIONS.md for how it is
    checked.
    """

    MASK = "mask"
    SNORKEL = "snorkel"
    FINS = "fins"
    WETSUIT = "wetsuit"
    DRYSUIT = "drysuit"
    VEST = "vest"
    HOOD = "hood"
    GLOVES = "gloves"
    BOOTS = "boots"
    BCD = "bcd"
    BACKPLATE = "backplate"
    WING = "wing"
    REGULATOR = "regulator"
    REBREATHER = "rebreather"
    COMPUTER = "computer"
    TRANSMITTER = "transmitter"
    CYLINDER = "cylinder"
    LIGHT = "light"
    SCOOTER = "scooter"
    SMB = "smb"
    MIRROR = "mirror"
    WHISTLE = "whistle"
    REEL = "reel"
    KNIFE = "knife"
    LINE_CUTTER = "line_cutter"
    SHEARS = "shears"
    COMPASS = "compass"
    CAMERA = "camera"
    OTHER = "other"


class CylinderMaterial(StrEnum):
    """What a cylinder is made of - DiveJSON 1.0's `gear_item.material`, which takes UDDF's
    `tankmaterial` spellings in that order. No DB `CHECK`, as `GearType` has none."""

    STEEL = "steel"
    ALUMINIUM = "aluminium"
    CARBON = "carbon"


# A vessel's facts, unbounded here and bounded on the write shapes: the read shapes inherit
# this base, and a bound on it would validate stored rows on the way out (`DiveMixtureBase`).
_VOLUME_DESCRIPTION = "Litres of water capacity - a manifolded twinset's combined. Meaningful on a cylinder."
_WORKING_PRESSURE_DESCRIPTION = "Bar the vessel is rated to, never what was in it. Meaningful on a cylinder."
_MATERIAL_DESCRIPTION = "What the vessel is made of. Meaningful on a cylinder."


GearBrand = Annotated[str, Trimmed, Field(max_length=255)]

_NAME_DESCRIPTION = "What the diver calls this item"
_BRAND_DESCRIPTION = "Manufacturer or brand; blank is stored as null"
_GEAR_NOTES_DESCRIPTION = "What the diver wrote about this item"
_TYPE_DESCRIPTION = "Broad category this item falls into"
_RENTED_DESCRIPTION = "Whether this item is rented rather than owned"


class GearItemBase(BaseModel):
    name: Annotated[str, Trimmed, Field(max_length=255, examples=["MK25 EVO / S620Ti"], description=_NAME_DESCRIPTION)]
    brand: Annotated[
        GearBrand | None,
        BlankIsNone,
        Field(default=None, examples=["Scubapro"], description=_BRAND_DESCRIPTION),
    ]
    type: Annotated[
        GearType | None,
        Field(default=None, examples=[GearType.REGULATOR], description=_TYPE_DESCRIPTION),
    ]
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH, description=_GEAR_NOTES_DESCRIPTION)]
    rented: Annotated[bool, Field(default=False, description=_RENTED_DESCRIPTION)]
    volume: Annotated[float | None, Field(default=None, examples=[12.0], description=_VOLUME_DESCRIPTION)]
    working_pressure: Annotated[
        float | None, Field(default=None, examples=[232.0], description=_WORKING_PRESSURE_DESCRIPTION)
    ]
    material: Annotated[
        CylinderMaterial | None,
        Field(default=None, examples=[CylinderMaterial.STEEL], description=_MATERIAL_DESCRIPTION),
    ]


class GearItemInfo(PublicUUIDSchema):
    """Summary of a gear item as embedded in a dive or a gear set, keyed by its public
    `uuid`. Lives here rather than in `dive.py`/`gear_set.py` because both embed it.

    `is_archived` is included so the UI can flag gear that a dive/set still references
    but which no longer shows up in the dive form's picker. The vessel's facts ride along so
    a dive's tank reads its vessel off the row it already holds.
    """

    name: str
    brand: str | None = None
    # `StoredVocabulary`, not `GearType`: this shape is embedded in every dive and gear
    # set, so an unrecognized stored `type` would fail those whole responses. See
    # *"A stored vocabulary is read back as a string"* in DECISIONS.md.
    type: StoredVocabulary | None = None
    rented: bool = False
    is_archived: bool = False
    volume: float | None = None
    working_pressure: float | None = None
    material: StoredVocabulary | None = None


class GearItemRead(GearItemBase, PublicUUIDSchema):
    """Public representation of a gear item, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).
    """

    name: StoredName
    brand: StoredName | None = None
    # Overrides `GearItemBase.type`, which stays `GearType` for the writes that base
    # actually validates.
    type: StoredVocabulary | None = None  # type: ignore[assignment]  # widening a write base's field; see `StoredVocabulary`
    material: StoredVocabulary | None = None  # type: ignore[assignment]  # widening a write base's field; see `StoredVocabulary`

    user_uuid: uuid_pkg.UUID
    is_archived: bool = False
    archived_at: datetime | None = None
    dive_count: Annotated[
        int,
        Field(
            default=0,
            description="Number of the owner's dives this item was used on - listed in a dive's gear or linked "
            "from one of its cylinders, counted once either way",
        ),
    ]
    dive_time: Annotated[
        int,
        Field(default=0, description="Summed time in the water of those dives, in seconds - what an hours rule counts"),
    ]
    # This item's servicing rules, embedded so the gear list can badge "service due"
    # without a request per row. Carries due dates and thresholds only, never a computed
    # status - see `ServiceStatus` in `schemas/gear_service.py`.
    service: Annotated[
        list[GearServiceScheduleInfo],
        Field(default_factory=list, description="Service schedules attached to this item"),
    ]
    created_at: datetime


class GearItemReadInternal(GearItemBase, PublicUUIDSchema):
    """Mirrors the actual `gear_item` table columns (integer PK/FK), for server-side
    lookups only - never returned directly over the API (use `GearItemRead` for the
    public shape, which additionally resolves `user_id` to the owning user's `uuid`).
    """

    name: StoredName
    brand: StoredName | None = None
    type: StoredVocabulary | None = None  # type: ignore[assignment]  # widening a write base's field; see `StoredVocabulary`
    material: StoredVocabulary | None = None  # type: ignore[assignment]  # widening a write base's field; see `StoredVocabulary`

    id: int
    user_id: int
    is_archived: bool = False
    archived_at: datetime | None = None
    dive_count: int = 0
    dive_time: int = 0
    created_at: datetime


# The bounds every write shape carries, with the `CHECK`s on `gear_item` behind them: the
# mixture's volume and start-pressure bands, for the same vessel seen from the dive.
VesselVolume = Annotated[float | None, Field(default=None, gt=0, examples=[12.0], description=_VOLUME_DESCRIPTION)]
VesselWorkingPressure = Annotated[
    float | None, Field(default=None, gt=0, le=350, examples=[232.0], description=_WORKING_PRESSURE_DESCRIPTION)
]


class GearItemCreate(GearItemBase):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    volume: VesselVolume
    working_pressure: VesselWorkingPressure


class GearItemCreateInternal(GearItemBase):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    user_id: int
    volume: VesselVolume
    working_pressure: VesselWorkingPressure


class GearItemUpdate(RejectsExplicitNulls):
    """Partial update. `is_archived` doubles as the archive/unarchive control - the API
    derives `archived_at` from it rather than letting callers set the timestamp directly.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # `brand` and `type` are genuinely nullable, so clearing either is a real edit -
    # `patch_gear_item` reads an explicit `brand` null through `model_fields_set` when it
    # re-checks (brand, name) uniqueness.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "notes", "rented", "is_archived")

    name: Annotated[Annotated[str, Trimmed] | None, Field(max_length=255, default=None, description=_NAME_DESCRIPTION)]
    brand: Annotated[GearBrand | None, BlankIsNone, Field(default=None, description=_BRAND_DESCRIPTION)]
    type: Annotated[GearType | None, Field(default=None, description=_TYPE_DESCRIPTION)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH, description=_GEAR_NOTES_DESCRIPTION)]
    rented: Annotated[bool | None, Field(default=None, description=_RENTED_DESCRIPTION)]
    is_archived: Annotated[bool | None, Field(default=None, description="Set to archive/unarchive this item")]
    # Clearable, so absent from `NON_NULLABLE_FIELDS`. A changed `volume` is written through
    # to every dive cylinder linked to the item (`patch_gear_item`).
    volume: VesselVolume
    working_pressure: VesselWorkingPressure
    material: Annotated[CylinderMaterial | None, Field(default=None, description=_MATERIAL_DESCRIPTION)]


class GearItemUpdateInternal(GearItemUpdate):
    archived_at: datetime | None = None
    updated_at: datetime


class GearItemLookupItem(PublicUUIDSchema):
    """One row of `GET /gear-items/lookup`: `GearItemInfo`'s members, which a picker stores
    for its label and its Rented and Archived badges, and a cylinder picker for the capacity
    a pick copies. Its own class rather than that one, so the dive's embedded shape can grow
    without widening every lookup row."""

    name: str
    brand: str | None = None
    type: StoredVocabulary | None = None
    rented: bool = False
    is_archived: bool = False
    volume: float | None = None
    working_pressure: float | None = None
    material: StoredVocabulary | None = None
