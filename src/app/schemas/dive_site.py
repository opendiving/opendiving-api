import re
from collections.abc import Iterable
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self

from divejson.validate import folded
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ..core.schemas import (
    MAX_LIST_ITEMS,
    NOTES_MAX_LENGTH,
    PublicUUIDSchema,
    RejectsExplicitNulls,
    StoredName,
    StoredVocabulary,
    Trimmed,
)
from .dive import EntryType, WaterType
from .location import (
    LOCATION_NAME_MAX,
    Latitude,
    LocationInput,
    LocationRead,
    Longitude,
    WholeCoordinatePair,
)
from .tag import TagsUpdate, TagsWrite, trim_tag

# DiveJSON §6.10's bound on another name, and §6.10a's on an identifier.
OTHER_NAME_MAX = 255
IDENTIFIER_MAX = 255
# §5.5's producer-key pattern, which names a registry. The format bounds no producer key's
# length; this one is the app's, at an identifier's width.
REGISTRY_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
REGISTRY_MAX = 255

WIKIDATA = "wikidata"
OPENSTREETMAP = "openstreetmap"
WORMS = "worms"

# The identifier's form under each registry the format names, as its schema holds it. Any
# other registry's identifier is carried as written.
_IDENTIFIER_FORMS: dict[str, tuple[re.Pattern[str], str]] = {
    WIKIDATA: (re.compile(r"Q[1-9][0-9]*"), "the item id, Q and digits with no leading zero"),
    OPENSTREETMAP: (
        re.compile(r"(node|way|relation)/[1-9][0-9]*"),
        "the element type, a slash and the element's number, e.g. node/313862678",
    ),
    WORMS: (re.compile(r"[1-9][0-9]*"), "the AphiaID's digits with no leading zero, e.g. 127160"),
}

DEPTH_RANGE_MESSAGE = "depth_from must not be greater than depth_to"


def identifier_problem(registry: str, identifier: str) -> str | None:
    """Why an identifier is not of its registry's form, or `None` where it is - or where the
    format names no form for the registry."""
    form = _IDENTIFIER_FORMS.get(registry)
    if form is None or form[0].fullmatch(identifier):
        return None
    return f"a {registry} identifier is {form[1]}"


def canonical_other_names(name: str | None, other_names: Iterable[str]) -> list[str]:
    """Another name the site's own name already says, or an earlier one does, is dropped.

    DiveJSON's §3 rule 8 compares them trimmed and case-folded, through the format's own
    `folded`. Dropped rather than refused, as a repeated tag is collapsed: the name it
    repeats already says it, and a rename onto another name is a rename, not an error.
    """
    seen = set() if name is None else {folded(name)}
    kept: list[str] = []
    for other in other_names:
        key = folded(other)
        if key not in seen:
            seen.add(key)
            kept.append(other)
    return kept


def canonical_entry_types(values: Iterable[str]) -> list[EntryType]:
    """A set in `EntryType`'s declaration order, as `canonical_roles` stores a contact's -
    two equal sets are two equal lists. A value outside the vocabulary is left out."""
    present = set(values)
    return [entry for entry in EntryType if entry in present]


def validate_depth_range(depth_from: float | None, depth_to: float | None) -> None:
    """The shallow end above the deep one, or the same - `ck_dive_site_depth_range`, and
    §3 rule 2 for a site."""
    if depth_from is not None and depth_to is not None and depth_from > depth_to:
        raise ValueError(DEPTH_RANGE_MESSAGE)


class ExternalId(BaseModel):
    """The site's entry in a registry outside the logbook - DiveJSON §6.10a's External Id,
    without the `extensions` this app stores nowhere.

    An open list rather than a member per registry: a registry is named as a producer key
    is, the ones the format names hold their identifiers to its form, and any other is
    carried as written. Two sites may share an entry - a registry's object can be coarser
    than a diver's sites - so nothing makes one unique across a logbook.
    """

    model_config = ConfigDict(extra="forbid")

    registry: Annotated[
        str,
        StringConstraints(pattern=REGISTRY_PATTERN, max_length=REGISTRY_MAX),
        Field(examples=[OPENSTREETMAP], description="The registry, named as a DiveJSON producer key is"),
    ]
    identifier: Annotated[
        str,
        StringConstraints(min_length=1, max_length=IDENTIFIER_MAX),
        Field(examples=["node/313862678"], description="The registry's own identifier for the place"),
    ]

    @property
    def pair(self) -> tuple[str, str]:
        return (self.registry, self.identifier)

    @model_validator(mode="after")
    def _identifier_has_its_registry_s_form(self) -> Self:
        problem = identifier_problem(self.registry, self.identifier)
        if problem is not None:
            raise ValueError(problem)
        return self


class ExternalIdRead(BaseModel):
    """An External Id as stored: read back without the write's checks, a stored value being
    what it is."""

    registry: str
    identifier: str


def canonical_external_ids(entries: Iterable[ExternalId]) -> list[ExternalId]:
    """One entry per registry and identifier, compared exactly, the first kept - §3's rule
    beside rule 7. The format lets two sites share an entry; one site naming it twice says
    nothing more."""
    seen: set[tuple[str, str]] = set()
    kept: list[ExternalId] = []
    for entry in entries:
        if entry.pair not in seen:
            seen.add(entry.pair)
            kept.append(entry)
    return kept


# Trimmed as a tag is, and never blank - DiveJSON's 1-255.
OtherName = Annotated[
    str,
    BeforeValidator(trim_tag),
    StringConstraints(min_length=1, max_length=OTHER_NAME_MAX),
    Field(examples=["Sunabe Seawall"]),
]
_NAME_DESCRIPTION = "Name of the site"
_NOTES_DESCRIPTION = "What the diver wrote about the site"
_WATER_TYPE_DESCRIPTION = "Whether the water at the site is salt, fresh or brackish"
_LATITUDE_DESCRIPTION = "Latitude of the site's pin in degrees; set together with longitude, or both null"
_LONGITUDE_DESCRIPTION = "Longitude of the site's pin in degrees; set together with latitude, or both null"
_LOCATION_DESCRIPTION = "The town or area the site is in, replacing any stored one; null clears it"

SiteDepth = Annotated[float | None, Field(default=None, ge=0, allow_inf_nan=False, description="In metres")]
# The dive's range, `ck_dive_site_altitude_range`.
SiteAltitude = Annotated[
    int | None, Field(default=None, ge=-450, le=6500, examples=[372], description="Metres above sea level")
]


class DiveSiteColumnsBase(BaseModel):
    """What every shape of a dive site carries apart from its locality and the diver's notes.

    The locality is split out because the wire nests it and the table does not: a
    `DiveSiteRead` carries one `location` object, while `DiveSiteReadInternal` mirrors the
    `location_*` columns behind it so FastCRUD can select them by name.
    """

    name: Annotated[str, Trimmed, Field(max_length=255, examples=["Blue Hole"], description=_NAME_DESCRIPTION)]
    latitude: Annotated[Latitude, Field(description=_LATITUDE_DESCRIPTION)]
    longitude: Annotated[Longitude, Field(description=_LONGITUDE_DESCRIPTION)]


class DiveSiteBase(DiveSiteColumnsBase):
    """The wire's site, whose notes are the reading diver's and live on `saved_site`: the shapes
    that mirror `dive_site` take `DiveSiteColumnsBase` instead, where FastCRUD would otherwise
    find no such column and leave them at their default."""

    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH, description=_NOTES_DESCRIPTION)]


class DiveSiteLocationColumns(BaseModel):
    """The locality as `dive_site` stores it: flat, prefixed, beside the site's own pin.

    Never on the wire. The prefix is what keeps the two positions apart in the table - a
    site's `latitude` is the pin a diver dropped, `location_latitude` is the centre of the
    town the geocoder resolved, and nothing fills either from the other.

    **Permissive on purpose**, which is why the write shapes take the subclass below
    instead: this is what `DiveSiteReadInternal` mirrors the table with, and a row that
    only raw SQL could have written - an empty locality name, say - should read back as
    the odd thing it is rather than turn every read of that site into a 500. Same split,
    and the same reason, as `WholeCoordinatePair` not being on `DiveSiteBase`.
    """

    location_name: Annotated[str | None, Field(default=None, max_length=LOCATION_NAME_MAX)]
    location_latitude: Latitude
    location_longitude: Longitude
    location_bbox_south: Annotated[float | None, Field(default=None, ge=-90, le=90)]
    location_bbox_north: Annotated[float | None, Field(default=None, ge=-90, le=90)]
    location_bbox_west: Annotated[float | None, Field(default=None, ge=-180, le=180)]
    location_bbox_east: Annotated[float | None, Field(default=None, ge=-180, le=180)]


class DiveSiteLocationColumnsInput(DiveSiteLocationColumns):
    """The same columns on the way in, where an empty locality name is refused.

    §6.9 makes a place's `name` 1-255, so `""` is not a short name but a place with none:
    it exports a `location.name` the format's schema rejects and reads back as a nameless
    place. `LocationInput` already refuses it for every API caller; this closes the same
    hole on the admin panel's own form, which writes these columns directly. The
    migration's `nullif(location, '')` clears the rows the old unbounded field left behind,
    and this is what stops a new one arriving.

    Clearing the locality is untouched: `None` is still how a site entered with the wrong
    place is corrected back to "not recorded", and a minimum length says nothing about it.
    """

    location_name: Annotated[Annotated[str, Trimmed] | None, Field(default=None, max_length=LOCATION_NAME_MAX)]


class _DiveSiteMembersRead(BaseModel):
    """What a site holds beside its name, its pin, its locality and its notes, as stored -
    widened on the way out as every stored vocabulary is (*"A stored vocabulary is read
    back as a string"* in DECISIONS.md)."""

    other_names: Annotated[list[str], Field(default_factory=list)]
    external_ids: Annotated[list[ExternalIdRead], Field(default_factory=list)]
    depth_from: float | None = None
    depth_to: float | None = None
    water_type: StoredVocabulary | None = None
    altitude: int | None = None
    entry_types: Annotated[list[StoredVocabulary], Field(default_factory=list)]


class DiveSiteSummary(BaseModel):
    """What the diver's own live dives say of a site, counted at read time: every dive that
    names the site, at any position.

    Derived, never stored, so it is as fresh as the cache it is read through - which every
    write moving one of these figures drops.
    """

    dive_count: Annotated[int, Field(description="Live dives naming this site at any position")] = 0
    last_dived_on: Annotated[
        date | None, Field(description="The local date of the latest of them, as the diver logged it")
    ] = None
    max_dive_depth: Annotated[float | None, Field(description="The greatest `max_depth` among them, in metres")] = None
    species_count: Annotated[int, Field(description="Distinct species sighted on them")] = 0
    average_rating: Annotated[float | None, Field(description="The mean rating of the rated ones")] = None


# Who a site belongs to. A later kind of owner is a further value here, which is why this is
# not a boolean.
SITE_OWNER_ACCOUNT = "account"
SITE_OWNER_COMMONS = "opendivingcommons"
DiveSiteOwner = Literal["account", "opendivingcommons"]


class _DiveSiteRelation(BaseModel):
    """What a read says of a site's place in the reader's world, beside the site itself."""

    owner: Annotated[
        DiveSiteOwner,
        Field(description="`account`, the reader's own site, or `opendivingcommons`, a record every account shares"),
    ]
    saved: Annotated[bool, Field(description="Whether the site is on the reader's list")]
    # Per row rather than in an envelope: the credit is a condition of the record's licence, so
    # it travels with what it describes. A wire format, `[label](href)`, not display copy - the
    # clients collapse repeats by string, and an OpenStreetMap record's is byte-identical to the
    # geocoder's so that a menu listing both shows one credit.
    attribution: Annotated[
        str | None,
        Field(
            default=None,
            max_length=255,
            examples=["[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)"],
            description="The credit a Commons record's source asks for, to show wherever it is listed or shown; "
            "null on the reader's own site",
        ),
    ]


class DiveSiteRead(DiveSiteBase, _DiveSiteMembersRead, DiveSiteSummary, _DiveSiteRelation, PublicUUIDSchema):
    """Public representation of a dive site, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API), with its tags by name in
    the diver's order and the summary of the diver's dives there.

    `notes`, `tags`, the summary and `created_at` are the reader's, whoever else has the site:
    on a Commons record they have not saved the first three are empty and `created_at` is when
    the row became a Commons record.
    """

    name: StoredName
    location: LocationRead | None = None
    tags: Annotated[list[str], Field(default_factory=list)]
    created_at: datetime


class DiveSiteReadInternal(DiveSiteColumnsBase, _DiveSiteMembersRead, DiveSiteLocationColumns, PublicUUIDSchema):
    """Mirrors the actual `dive_site` table columns (integer PK/FK), for server-side
    lookups only - never returned directly over the API (use `DiveSiteRead` for the
    public shape, which additionally says who owns the site, nests the locality and carries
    the diver's notes, the tags and the summary).
    """

    name: StoredName
    id: int
    # Null on a Commons record, which no account owns.
    user_id: int | None
    created_at: datetime


class _DiveSiteMembersWrite(BaseModel):
    """The members a create takes, each held to the format's rule for it.

    The lists are made conforming rather than refused: a repeated registry entry or entry
    type is kept once, and another name the site's name or an earlier one already says is
    dropped (`canonical_other_names`). Every writer goes through one of the schemas built on
    this - the routes, and the admin panel's flat form - so a stored site always conforms.
    """

    other_names: Annotated[
        list[OtherName],
        Field(
            default_factory=list,
            max_length=MAX_LIST_ITEMS,
            description="Other names the site goes by, in the diver's order",
        ),
    ]
    external_ids: Annotated[
        list[ExternalId],
        Field(
            default_factory=list,
            max_length=MAX_LIST_ITEMS,
            description="The place's entries in registries outside the logbook",
        ),
    ]
    depth_from: SiteDepth
    depth_to: SiteDepth
    water_type: Annotated[
        WaterType | None, Field(default=None, examples=[WaterType.SALT], description=_WATER_TYPE_DESCRIPTION)
    ]
    altitude: SiteAltitude
    entry_types: Annotated[
        list[EntryType],
        Field(
            default_factory=list,
            max_length=MAX_LIST_ITEMS,
            examples=[[EntryType.SHORE, EntryType.BOAT]],
            description="Every way divers enter the water there, in any order - stored in vocabulary order",
        ),
    ]

    @field_validator("external_ids")
    @classmethod
    def _one_per_entry(cls, value: list[ExternalId]) -> list[ExternalId]:
        return canonical_external_ids(value)

    @field_validator("entry_types")
    @classmethod
    def _canonical_entry_types(cls, value: list[EntryType]) -> list[EntryType]:
        return canonical_entry_types(value)

    @model_validator(mode="after")
    def _conforms(self) -> Self:
        validate_depth_range(self.depth_from, self.depth_to)
        name = getattr(self, "name", None)
        conformed = canonical_other_names(name, self.other_names)
        if conformed != self.other_names:
            self.other_names = conformed
        return self


class DiveSiteCreate(DiveSiteBase, _DiveSiteMembersWrite, WholeCoordinatePair):
    model_config = ConfigDict(extra="forbid")

    location: Annotated[LocationInput | None, Field(default=None, description=_LOCATION_DESCRIPTION)]
    tags: TagsWrite


class DiveSiteCreateInternal(
    DiveSiteColumnsBase, _DiveSiteMembersWrite, DiveSiteLocationColumnsInput, WholeCoordinatePair
):
    """What reaches FastCRUD, so the locality is flat here where `DiveSiteCreate` nests it,
    and the tags and the notes - rows of other tables - are absent."""

    model_config = ConfigDict(extra="forbid")

    user_id: int


class _DiveSiteUpdateFields(WholeCoordinatePair, RejectsExplicitNulls):
    """What both update shapes carry, which is everything but the locality, the tags and the
    notes.

    The two differ only in how they spell a place: `DiveSiteUpdate` is column-shaped for
    CRUDAdmin and the `NOT NULL` sweep, `DiveSiteUpdateRequest` nests it for the API.

    A list replaces the stored one whole, and is made conforming as a create's is. Another
    name is checked against the name the body sends; against the stored name, and a rename
    against the stored other names, `patch_dive_site` checks, being the one that reads them.
    """

    # The locality is genuinely nullable and stays off this list: clearing it is how a
    # site entered with the wrong one gets corrected back to "not recorded", and
    # `patch_dive_site` reads that explicit null through `model_fields_set`. The depths,
    # the altitude and the water type are nullable for the same reason; the three lists
    # are not - an empty list is how one is cleared.
    #
    # So are the coordinates, but they answer to `WholeCoordinatePair` above instead:
    # both columns are nullable, and clearing the position means sending *both* as null.
    # Listing them here would refuse that - a site whose position was mistyped could
    # never be corrected back to "not recorded".
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "other_names", "external_ids", "entry_types")

    latitude: Annotated[Latitude, Field(description=_LATITUDE_DESCRIPTION)]
    longitude: Annotated[Longitude, Field(description=_LONGITUDE_DESCRIPTION)]
    name: Annotated[Annotated[str, Trimmed] | None, Field(max_length=255, default=None, description=_NAME_DESCRIPTION)]
    other_names: Annotated[
        list[OtherName] | None,
        Field(default=None, max_length=MAX_LIST_ITEMS, description="Replaces the other names whole"),
    ]
    external_ids: Annotated[
        list[ExternalId] | None,
        Field(default=None, max_length=MAX_LIST_ITEMS, description="Replaces the registry entries whole"),
    ]
    depth_from: SiteDepth
    depth_to: SiteDepth
    water_type: Annotated[WaterType | None, Field(default=None, description=_WATER_TYPE_DESCRIPTION)]
    altitude: SiteAltitude
    entry_types: Annotated[
        list[EntryType] | None,
        Field(default=None, max_length=MAX_LIST_ITEMS, description="Replaces the entry types whole"),
    ]

    @field_validator("external_ids")
    @classmethod
    def _one_per_entry(cls, value: list[ExternalId] | None) -> list[ExternalId] | None:
        return None if value is None else canonical_external_ids(value)

    @field_validator("entry_types")
    @classmethod
    def _canonical_entry_types(cls, value: list[EntryType] | None) -> list[EntryType] | None:
        return None if value is None else canonical_entry_types(value)

    @model_validator(mode="after")
    def _conforms(self) -> Self:
        validate_depth_range(self.depth_from, self.depth_to)
        if self.other_names is not None:
            conformed = canonical_other_names(self.name, self.other_names)
            if conformed != self.other_names:
                self.other_names = conformed
        return self


class DiveSiteUpdate(_DiveSiteUpdateFields, DiveSiteLocationColumnsInput):
    """CRUDAdmin's Dive Site form, and the shape `test_update_explicit_nulls.py` sweeps
    against the `dive_site` table's columns - so the locality is flat here, as the table
    has it. `TripUpdate` is split from `TripUpdateRequest` for the same reason.
    """

    model_config = ConfigDict(extra="forbid")


class DiveSiteUpdateRequest(_DiveSiteUpdateFields):
    """Request body for `PATCH /dive-site/{uuid}`.

    Naming `location` **replaces** the stored place wholesale rather than merging into it:
    it is a value object with no identity, so there is nothing to merge into, and a
    partial update would leave a cleared locality's centre and box behind. An explicit
    null clears it.
    """

    model_config = ConfigDict(extra="forbid")

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = (*_DiveSiteUpdateFields.NON_NULLABLE_FIELDS, "notes")

    location: Annotated[LocationInput | None, Field(default=None, description=_LOCATION_DESCRIPTION)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH, description=_NOTES_DESCRIPTION)]
    tags: TagsUpdate


class DiveSiteSave(BaseModel):
    """Request body for `PUT /dive-site/{uuid}/saved`: what the diver writes as they save it."""

    model_config = ConfigDict(extra="forbid")

    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH, description=_NOTES_DESCRIPTION)]
    tags: TagsWrite


class DiveSiteUpdateInternal(DiveSiteUpdate):
    updated_at: datetime


class DiveSiteListSort(StrEnum):
    """How `GET /dive-sites` orders a page. `name` is the default; the other two sort on the
    summary, most first, and break ties by name."""

    NAME = "name"
    DIVE_COUNT = "dive_count"
    # Most recently dived first, and a site no live dive names after every one that has.
    LAST_DIVED_ON = "last_dived_on"


class DiveSiteLookupItem(_DiveSiteRelation, PublicUUIDSchema):
    """One row of `GET /dive-sites/lookup`: the name and the locality a picker shows under it,
    and the members a dive form fills from the site picked, as `DiveSiteRead` carries them.

    Not the dive's `DiveSiteInfo`, which requires coordinates. `external_ids` is what lets a
    place search drop a geocoder row for an OpenStreetMap object a listed site already names;
    `other_names` is what shows why a search found a row whose name does not match it.
    """

    name: str
    other_names: Annotated[list[str], Field(default_factory=list)]
    latitude: float | None = None
    longitude: float | None = None
    location: LocationRead | None = None
    external_ids: Annotated[list[ExternalIdRead], Field(default_factory=list)]
    water_type: StoredVocabulary | None = None
    altitude: int | None = None
    entry_types: Annotated[list[StoredVocabulary], Field(default_factory=list)]
