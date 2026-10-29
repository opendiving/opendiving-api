"""A dive draft's shapes: the form's values with every member optional and no bound.

`DiveDraftValues` has exactly `DiveCreateRequest`'s member names, its lists' items' included,
with the same types and vocabularies and none of the bounds or cross-member rules a dive
enforces - a draft is unfinished by definition, and `POST /dive-draft/{uuid}/dive` validates the
dive it becomes. What a draft refuses is a wrong type, a word outside a vocabulary, a naive start
time and a member the dive does not have. `tests/test_dive_drafts.py` holds the names to
`DiveCreateRequest`'s, so a member added to one and not the other fails there rather than turning
every draft write that carries it into a 422.

The read shapes take the stored JSON as it is: vocabularies as strings (*"A stored vocabulary is
read back as a string"* in DECISIONS.md) and unknown members ignored.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated, Any, ClassVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ..core.schemas import RejectsExplicitNulls, StoredVocabulary
from .dive import Abundance, Current, DiveStartTime, DiveType, EntryType, WaterType, Waves, Weather
from .dive_mixture import GasRole, TankUsage
from .person import PersonReference, PersonReferenceRead

DIVE_DRAFT_NOT_FOUND = "Dive draft not found"

_WRITE = ConfigDict(extra="forbid", allow_inf_nan=False)


class DiveDraftMixture(BaseModel):
    """A cylinder as the form holds it: `DiveMixtureCreate`'s members, none bounded."""

    model_config = _WRITE

    volume: Annotated[float | None, Field(default=None, description="Cylinder volume in liters")]
    start_pressure: Annotated[float | None, Field(default=None, description="Starting pressure in bar")]
    end_pressure: Annotated[float | None, Field(default=None, description="Ending pressure in bar")]
    oxygen: Annotated[float | None, Field(default=None, description="Oxygen percentage")]
    helium: Annotated[float | None, Field(default=None, description="Helium percentage")]
    po2_limit: Annotated[float | None, Field(default=None, description="Planned ppO2 for this gas, in bar")]
    gas_number: Annotated[int | None, Field(default=None, description="How the source export identifies this cylinder")]
    role: Annotated[GasRole | None, Field(default=None, description="What the cylinder was carried for")]
    usage: Annotated[TankUsage | None, Field(default=None, description="How the cylinder was breathed")]
    gear_item_uuid: Annotated[
        uuid_pkg.UUID | None,
        Field(
            default=None,
            description="Public id of the diver's gear item this cylinder is; null for a rental or unrecorded vessel",
        ),
    ]


class DiveDraftSighting(BaseModel):
    """`SightingWrite`'s members, unbounded and with no rule between count and band. The
    species is what a sighting is, so it is the one member a draft's sighting needs."""

    model_config = _WRITE

    species_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the species seen")]
    count: Annotated[int | None, Field(default=None, description="How many were counted; null is seen, not counted")]
    abundance: Annotated[
        Abundance | None, Field(default=None, description="How many were seen, as a band, where there is no count")
    ]
    notes: Annotated[str, Field(default="", description="What the diver wrote about this sighting")]


class DiveDraftValues(BaseModel):
    model_config = _WRITE

    dive_number: Annotated[int | None, Field(default=None, description="The diver's own number for this dive")]
    start_time: Annotated[
        DiveStartTime | None,
        Field(
            default=None,
            examples=["2021-04-04T10:04:47.910+02:00"],
            description="When the dive started, with the UTC offset of the timezone it happened in",
        ),
    ]
    submerged_time: Annotated[
        int | None,
        Field(default=None, description="The dive computer's time in the water, in seconds, surface time excluded"),
    ]
    max_depth: Annotated[float | None, Field(default=None, description="Deepest point reached, in meters")]
    avg_depth: Annotated[float | None, Field(default=None, description="Average depth over the dive, in meters")]
    bottom_temperature: Annotated[
        float | None, Field(default=None, description="Water temperature at the bottom, in °C")
    ]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    weight: Annotated[float | None, Field(default=None, description="Total ballast carried, in kilograms")]
    water_type: Annotated[
        WaterType | None, Field(default=None, description="What the water was, as a dive computer calibrates")
    ]
    altitude: Annotated[
        int | None, Field(default=None, description="Elevation of the water surface, in meters above sea level")
    ]
    type: Annotated[DiveType | None, Field(default=None, description="What kind of dive it was")]
    rating: Annotated[int | None, Field(default=None, description="The diver's rating, 1 to 5")]
    air_temperature: Annotated[float | None, Field(default=None, description="Air temperature at the surface, in °C")]
    current: Annotated[Current | None, Field(default=None, description="How strong the current was")]
    waves: Annotated[Waves | None, Field(default=None, description="What the water surface was like")]
    weather: Annotated[Weather | None, Field(default=None, description="What the sky was doing")]
    entry_type: Annotated[EntryType | None, Field(default=None, description="Where the diver got in")]
    boat_name: Annotated[str | None, Field(default=None, description="The boat's name")]
    notes: Annotated[str | None, Field(default=None, description="The diver's free-text notes on the dive")]
    trip_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the trip this dive belongs to")
    ]
    course_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the training course this dive was on")
    ]
    contact_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the contact that ran the dive")
    ]
    mixtures: Annotated[list[DiveDraftMixture] | None, Field(default=None, description="The cylinders breathed from")]
    dive_site_uuids: Annotated[
        list[uuid_pkg.UUID] | None,
        Field(default=None, description="Public ids of the dive sites visited, in the order visited"),
    ]
    gear_item_uuids: Annotated[
        list[uuid_pkg.UUID] | None, Field(default=None, description="The gear items ticked on the loaded set")
    ]
    sightings: Annotated[
        list[DiveDraftSighting] | None, Field(default=None, description="The species spotted, in the order listed")
    ]
    people: Annotated[
        list[PersonReference] | None,
        Field(default=None, description="The people on the dive, in the diver's order, each with the role they had"),
    ]
    tags: Annotated[list[str] | None, Field(default=None, description="The tags by name, in the diver's order")]

    def stored(self) -> dict[str, Any]:
        """What `dive_draft.values` holds: the members given, as JSON."""
        return self.model_dump(mode="json", exclude_none=True)


class DiveDraftMixtureRead(BaseModel):
    volume: float | None = None
    start_pressure: float | None = None
    end_pressure: float | None = None
    oxygen: float | None = None
    helium: float | None = None
    po2_limit: float | None = None
    gas_number: int | None = None
    role: StoredVocabulary | None = None
    usage: StoredVocabulary | None = None
    gear_item_uuid: uuid_pkg.UUID | None = None


class DiveDraftSightingRead(BaseModel):
    species_uuid: uuid_pkg.UUID
    count: int | None = None
    abundance: StoredVocabulary | None = None
    notes: str = ""


class DiveDraftValuesRead(BaseModel):
    """`DiveDraftValues` as stored. A reference to a record the account no longer holds is
    not in it: an id is dropped from its list, a single reference and a cylinder's vessel
    read as null, and a sighting or a person entry whose record is gone is dropped whole."""

    dive_number: int | None = None
    start_time: datetime | None = None
    submerged_time: int | None = None
    max_depth: float | None = None
    avg_depth: float | None = None
    bottom_temperature: float | None = None
    visibility: int | None = None
    weight: float | None = None
    water_type: StoredVocabulary | None = None
    altitude: int | None = None
    type: StoredVocabulary | None = None
    rating: int | None = None
    air_temperature: float | None = None
    current: StoredVocabulary | None = None
    waves: StoredVocabulary | None = None
    weather: StoredVocabulary | None = None
    entry_type: StoredVocabulary | None = None
    boat_name: str | None = None
    notes: str | None = None
    trip_uuid: uuid_pkg.UUID | None = None
    course_uuid: uuid_pkg.UUID | None = None
    contact_uuid: uuid_pkg.UUID | None = None
    mixtures: list[DiveDraftMixtureRead] | None = None
    dive_site_uuids: list[uuid_pkg.UUID] | None = None
    gear_item_uuids: list[uuid_pkg.UUID] | None = None
    sightings: list[DiveDraftSightingRead] | None = None
    people: list[PersonReferenceRead] | None = None
    tags: list[str] | None = None


class DiveDraftCreate(BaseModel):
    """The form's whole state. A `gear_set_uuid` that names no set of the caller's is stored
    as none rather than refused, as a reference inside `values` is kept rather than checked."""

    model_config = ConfigDict(extra="forbid")

    gear_set_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="The gear set loaded as the checklist")
    ]
    values: Annotated[
        DiveDraftValues,
        Field(default_factory=DiveDraftValues, description="The form's values; every member is optional"),
    ]


class DiveDraftUpdate(RejectsExplicitNulls):
    """`values`, when present, replaces the draft's wholesale, and `gear_set_uuid`, when
    present, sets or clears the set: a client sends the form's whole state."""

    model_config = ConfigDict(extra="forbid")

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("values",)

    gear_set_uuid: Annotated[
        uuid_pkg.UUID | None,
        Field(default=None, description="Public id of the gear set loaded as the checklist; null clears it"),
    ]
    values: Annotated[
        DiveDraftValues | None,
        Field(default=None, description="Replaces the draft's values wholesale. Omit to leave them; null is refused."),
    ]


class DiveDraftRead(BaseModel):
    uuid: uuid_pkg.UUID
    created_at: datetime
    updated_at: datetime
    gear_set_uuid: uuid_pkg.UUID | None = None
    values: DiveDraftValuesRead


def _as_stored(values: dict[str, Any]) -> dict[str, Any]:
    return DiveDraftValues.model_validate(values).stored()


class DiveDraftCreateInternal(BaseModel):
    """CRUDAdmin's create and update, which write the columns: `values` is checked and stored
    as the API's routes check and store it."""

    model_config = ConfigDict(extra="forbid")

    user_id: int
    gear_set_id: int | None = None
    values: Annotated[dict[str, Any], AfterValidator(_as_stored), Field(default_factory=dict)]


class DiveDraftReadInternal(BaseModel):
    """The row as `fetch_owned_or_raise` reads it."""

    id: int
    uuid: uuid_pkg.UUID
    user_id: int
