import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema
from ..core.utils.datetime_offset import require_utc_offset
from .dive_mixture import DiveMixtureCreate, DiveMixtureRead
from .gear_item import GearItemInfo

_START_TIME_EXAMPLE = "2021-04-04T10:04:47.910+02:00"

# `start_time` always carries an explicit UTC offset over the API, both ways: on input,
# it's the offset the caller (e.g. the web app, defaulting to the browser's own offset)
# knows the dive happened in; on output, it's reconstructed from the dive's stored
# `utc_offset_minutes` (see `core/utils/datetime_offset.py`) so a dive always displays in
# the timezone it was actually logged in, not the viewer's. A naive datetime (no offset)
# is rejected rather than silently assumed to be UTC or local.
DiveStartTime = Annotated[datetime, AfterValidator(require_utc_offset)]


class DiveBase(BaseModel):
    dive_number: Annotated[int, Field(examples=[5])]
    start_time: Annotated[DiveStartTime, Field(examples=[_START_TIME_EXAMPLE])]
    duration: Annotated[int, Field(examples=[2048], description="Dive duration in seconds")]

    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[float | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    weight: Annotated[
        float | None, Field(default=None, examples=[6.0], description="Total ballast carried, in kilograms")
    ]

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
    # A dive records the gear items used on it, never the gear *set* they were loaded
    # from: sets are purely a form-filling shortcut and can be edited or deleted
    # afterwards without rewriting history (see `models/gear_set.py`).
    gear_items: Annotated[
        list[GearItemInfo], Field(default_factory=list, description="Gear items used, in the order listed")
    ]


class DiveReadInternal(DiveBase, PublicUUIDSchema):
    """Mirrors the actual `dive` table columns (integer PK/FK), for server-side lookups
    only - never returned directly over the API (use `DiveRead`/`DiveReadWithMixtures`
    for the public shape, which additionally resolves `user_id`/`trip_id` to the owning
    user's/trip's `uuid` and attaches the dive's sites).

    `start_time` here is the raw stored UTC instant (not yet re-combined with
    `utc_offset_minutes` - see `combine_start_time()`), since that recombination only
    makes sense once converting to the public `DiveRead` shape.
    """

    id: int
    user_id: int
    trip_id: int | None = None
    utc_offset_minutes: Annotated[
        int, Field(description="UTC offset (minutes) start_time was originally expressed in, e.g. 120 for +02:00")
    ]
    created_at: datetime


class DiveFileInfo(PublicUUIDSchema):
    """Metadata about the dive-computer export a dive was imported from - never its
    bytes, which are only ever served by `GET /dive/{uuid}/file`."""

    original_filename: str
    content_type: str
    byte_size: int
    parser_key: Annotated[str, Field(description="Identifier of the parser that read this file, e.g. `suunto_xml`")]
    updated_at: datetime | None = None


class DiveGasUse(BaseModel):
    """Surface-normalized gas consumption for a dive, derived from its duration, average
    depth and cylinder pressures - see `services/dive_gas.py` for the arithmetic and for
    the (deliberately strict) conditions under which it's derivable at all.

    Present as a whole or not at all, rather than field-by-field: a dive either records
    enough to know what it consumed or it doesn't, and a half-populated version - litres
    used but no rate, say - would read as a number worth acting on when it isn't. Divers
    plan gas off these figures.
    """

    gas_used: Annotated[float, Field(examples=[1800.0], description="Gas breathed, in liters at surface pressure")]
    rmv: Annotated[
        float,
        Field(
            examples=[14.29],
            description="Respiratory minute volume: liters per minute at surface pressure. Cylinder-independent, "
            "so it's the figure to compare across dives.",
        ),
    ]
    sac_bar_per_min: Annotated[
        float,
        Field(
            examples=[1.19],
            description="Surface air consumption in bar per minute. Only meaningful alongside this dive's cylinder "
            "volume, but it's what a pressure gauge actually shows.",
        ),
    ]


class DiveGasUsePoint(BaseModel):
    """One dive's entry in a user's gas-use history (`GET /user/gas-use-history`).

    Carries just enough of the dive to plot and label a point and to link back to it -
    not a trimmed `DiveRead`. The series exists to be graphed, and every field here is
    either an axis, a tooltip, or the link target.
    """

    dive_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the dive this point came from")]
    dive_number: int
    start_time: Annotated[
        DiveStartTime,
        Field(
            examples=[_START_TIME_EXAMPLE],
            description="The dive's own offset-aware start time, exactly as `DiveRead` reports it - the x axis",
        ),
    ]
    avg_depth: Annotated[float, Field(description="Average depth the consumption was normalized from, in meters")]
    gas_use: DiveGasUse


class DiveReadWithMixtures(DiveRead):
    mixtures: Annotated[list[DiveMixtureRead], Field(default_factory=list)]
    # Deliberately here rather than on `DiveRead`, which `DiveReadWithMixtures` extends:
    # putting it on the parent would inherit it onto the paginated list response too,
    # adding a query to `_cached_read_dives` - the hottest path in the app - for
    # something only the detail page renders.
    source_file: Annotated[
        DiveFileInfo | None,
        Field(default=None, description="The dive-computer export this dive was imported from, if any"),
    ]
    # Here rather than on `DiveRead` for the same reason as `source_file` above, with one
    # extra: it's derived from the mixtures, which the list response doesn't carry at all.
    # Putting it on the parent would mean a batched mixture lookup in `_cached_read_dives`
    # purely to compute it.
    gas_use: Annotated[
        DiveGasUse | None,
        Field(
            default=None,
            description="Surface-normalized gas consumption, or null when the dive doesn't record enough to derive it",
        ),
    ]


class DiveCreate(DiveBase):
    model_config = ConfigDict(extra="forbid")

    trip_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the trip this dive belongs to")
    ]


class DiveCreateInternal(DiveBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    trip_id: int | None = None
    # `start_time` on this schema is the UTC instant to store (already split from the
    # public, offset-aware `start_time` via `split_start_time()`), paired with the offset
    # it was split from.
    utc_offset_minutes: int


class DiveCreateRequest(DiveCreate):
    """Request body for creating a dive, including its gas mixtures, dive site(s) and gear."""

    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this dive belongs to")]
    mixtures: Annotated[list[DiveMixtureCreate], Field(default_factory=list)]
    dive_site_uuids: Annotated[
        list[uuid_pkg.UUID],
        Field(default_factory=list, description="Public ids of the dive sites visited, in the order visited"),
    ]
    gear_item_uuids: Annotated[
        list[uuid_pkg.UUID],
        Field(default_factory=list, description="Public ids of the gear items used, in the order listed"),
    ]


class DiveUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dive_number: Annotated[int | None, Field(examples=[5], default=None)]
    start_time: Annotated[DiveStartTime | None, Field(examples=[_START_TIME_EXAMPLE], default=None)]
    duration: Annotated[int | None, Field(examples=[2048], description="Dive duration in seconds", default=None)]
    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[float | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    weight: Annotated[float | None, Field(default=None, description="Total ballast carried, in kilograms")]
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
    """Request body for updating a dive, including replacing its gas mixtures, dive site(s)
    and gear.

    If `mixtures`/`dive_site_uuids`/`gear_item_uuids` is omitted, the existing
    mixtures/dive sites/gear are left untouched. If provided (even as an empty list), the
    existing ones are replaced with the given list.
    """

    mixtures: Annotated[list[DiveMixtureCreate] | None, Field(default=None)]
    dive_site_uuids: Annotated[
        list[uuid_pkg.UUID] | None,
        Field(default=None, description="Public ids of the dive sites visited, in the order visited"),
    ]
    gear_item_uuids: Annotated[
        list[uuid_pkg.UUID] | None,
        Field(default=None, description="Public ids of the gear items used, in the order listed"),
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
    weight: Annotated[float | None, Field(default=None, description="Total ballast carried, in kilograms")]
    trip_id: Annotated[int | None, Field(default=None, description="Internal id of the trip this dive belongs to")]
    utc_offset_minutes: Annotated[int | None, Field(default=None)]
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
