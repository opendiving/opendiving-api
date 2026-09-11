"""The shape of `logbook.divejson` - a complete, structured copy of a diver's logbook.

This is a **DiveJSON 1.0 document**. DiveJSON is the open dive-log interchange format
this project maintains (<https://divejson.org>, repository of record
<https://github.com/divejson/divejson>), and its specification - not this module - is the
normative statement of the shape. What lives here is the writer's half of the
reference implementation: every member below is defined in `spec/divejson.md` §§4-6, and
the JSON Schema the `divejson` package publishes plus the beyond-schema rules in that
spec's §3 are what `tests/test_export_json.py` holds this to, through
`divejson.validate_document` - the reference validator itself, no longer a copy of it kept
in this repository.

It replaced the app's own `opendiving-export` format rather than sitting beside it: two
JSON shapes for one logbook is two things to keep in step forever, and the public one
would not have been the one the app itself used. So this file is both "the export" and
"the format", and the questions its old `EXPORT_VERSION` comment left open - when the
free-change window closes, what a version bump means - are answered by the specification's
§7 instead.

Where it differs from the API's own read shapes, and why:

- **No nulls, anywhere.** Absence is the only spelling of "not recorded" (spec §5.4), so
  the writer serializes with `exclude_none=True` and the schema rejects an explicit null.
  An empty `notes` is written as *absent* for the same reason: the column is `NOT NULL`
  with `""` standing for "the diver wrote nothing", so this app cannot tell a blank note
  from no note, and emitting `""` would claim the stronger of the two.
- **Nothing is re-scaled or re-unitised.** Depths meters, pressures bar, temperatures
  Celsius, durations seconds - which is the format's own canonical system (spec §5.1), so
  the app's wire values travel unchanged. The embedded profile keeps the integer scales
  `GET /dive/{uuid}/recording/{rid}/profile` uses, which the spec fixes too. The diver's `units`
  preference is account data, says which system they read in, and changes none of it
  (DECISIONS.md, *"Measurements are metric in the database and on the wire; `units` is
  who's looking"*).
- **Records reference each other by public `uuid`**, never by internal integer id - an
  implementation detail of this database that would be actively misleading in a file
  meant to outlive it. Spec §5.3 makes that the format's rule and adds referential
  closure: every uuid a record names is defined in the same document.
- **Whatever the format has no core member for rides `extensions.opendiving`** (spec
  §5.5): the diver's account preferences, and which parser read a stored dive-computer
  file. A writer may not invent core members, so this is the sanctioned slot.

The one derived value in here is `archive_path`, which is a fact about the zip rather than
about the logbook.
"""

import uuid as uuid_pkg
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from ..core.schemas import PublicUUIDSchema
from .certification import CertificationAgency
from .course import CourseStatus
from .dive import DiveLocalStartTime, WaterType
from .dive_mixture import DiveMixtureBase
from .dive_profile import DiveProfileRead
from .gear_item import GearType
from .gear_service import ServiceKind

# The format marker and the version the writer declares, both spec-defined literals and
# both required to be the document's first two members so a reader can dispatch before
# parsing further (spec §4). `version` is `"major.minor"` as a *string*: minor versions
# are additive, which the old bare integer could not signal without either lying or
# breaking every reader.
DIVEJSON_FORMAT = "divejson"
DIVEJSON_VERSION = "1.0"

# Spec §8. The `.divejson` extension is what `export_filename` is asked for, and the media
# type is what `GET /export/divejson` serves; IANA vendor-tree registration follows the
# specification's own 1.0 freeze rather than preceding it.
DIVEJSON_MEDIA_TYPE = "application/vnd.dive+json"
DIVEJSON_EXTENSION = "divejson"

# This producer's key inside every `extensions` object (spec §5.5). Stable by contract -
# a reader that learned to understand our entries keeps understanding them.
DIVEJSON_PRODUCER_KEY = "opendiving"

# Every `extensions` member: producer key to that producer's payload. Typed loosely on
# purpose - the spec allows any JSON value under a key, and a reader must not fail on
# content it does not recognize.
# The `= None` default is written at each use site rather than folded in here, because
# Pydantic's mypy plugin does not read a default out of `Annotated[T, Field(default=...)]`
# and would report every construction that omits it as a missing argument.
ExportExtensions = Annotated[
    dict[str, Any] | None,
    Field(description="Producer-keyed extension data (DiveJSON spec §5.5)"),
]


class ExportGenerator(BaseModel):
    """What wrote the document. A reader that hits something odd wants to know which
    version produced it - the same reason UDDF has `<generator>`."""

    name: str
    version: str | None = None


class ExportPosition(BaseModel):
    """A WGS 84 point, both halves required.

    An object rather than a `latitude`/`longitude` pair of members, because half a
    coordinate is unrepresentable: the grouping is "nothing invented" enforced by shape
    rather than by rule (spec §6).
    """

    latitude: float
    longitude: float


class ExportBoundingBox(BaseModel):
    """The rectangle a geocoder returned for a named place, so a reader can frame a map
    around the whole area without re-geocoding. `west` may exceed `east`, which means the
    box crosses the antimeridian."""

    south: float
    north: float
    west: float
    east: float


class ExportDiver(PublicUUIDSchema):
    """Whose logbook this is.

    `units`, `gear_service_emails` and the dive form's hidden fields and presets are
    application preferences rather than logbook data, so the format gives them no core
    member and they travel under this producer's key (spec §6.1). They are here at all
    because `/export/archive` promises nothing in the account is reachable only through the
    app - which is the whole reason the presets ride along too, UI configuration or not.
    """

    name: str
    username: str
    email: str
    created_at: datetime
    extensions: ExportExtensions = None


class ExportStoredFile(PublicUUIDSchema):
    """A binary the source logbook stores: a dive-computer export, or one side of a c-card.

    `sha256` is the stored digest of the bytes, not one computed at export time, so
    checking an extracted file against it verifies the whole round trip - database column
    to zip member - rather than just that the zip is internally consistent.

    `archive_path` is **absent** outside an archive: there is no container for the path to
    point into, and the format has one spelling of "not applicable" (spec §6.7). Which
    parser read a dive-computer file rides `extensions.opendiving.parser_key` - parser
    registries are application-specific and have no core member.

    A dive-computer file hangs off a **recording** rather than off the dive, and a recording
    may carry several: the same computer exported twice in two formats is one record in two
    spellings. Each keeps its own `uuid`, which is what makes them addressable across a
    round trip and what §3's uuid-uniqueness rule is checked against.
    """

    original_filename: str
    content_type: str
    byte_size: int
    sha256: str
    archive_path: str | None = None
    extensions: ExportExtensions = None


class ExportDevice(BaseModel):
    """What recorded one recording, as its own export named it (spec §6.4b).

    Every member OPTIONAL and every string non-empty: a member the source never wrote is
    absent, never `""` and never `null`. An object with no member at all is not written -
    `envelope._recording` drops it rather than emitting `{}`, which the schema would accept
    and which would claim the file named a computer it did not.

    `brand` is the maker, and it is the same word §6.12 uses for a gear item's - one word for
    one concept across the format.
    """

    brand: str | None = None
    model: str | None = None
    serial: Annotated[str | None, Field(default=None, description="Opaque, as the source wrote it; never parsed")]
    firmware: str | None = None
    name: Annotated[str | None, Field(default=None, description="What the device calls itself, as its owner set it")]
    dive_number: Annotated[
        int | None,
        Field(default=None, ge=0, description="The device's own counter - not the diver's numbering, which is §6.2's"),
    ]


class ExportRecording(BaseModel):
    """One device's record of one dive (spec §6.4a).

    **In order, and the first is primary** - the one a reader shows by default and the one a
    single-profile consumer takes. Order rather than a flag, matching the format: a flag
    every writer has to set is a value every reader has to default.

    `started_at` is written **only when it differs from the dive's**, because §6.4a says an
    absent one means the dive's. A second computer that entered the water later has its own;
    the ordinary single-computer dive does not, and writing a copy of the dive's start on
    every recording would be noise a reader has to compare rather than read.

    A recording carries at least one of `device`, `profile` and `source_files` - §3's
    beyond-schema rule 4 - which this writer satisfies by construction: it only emits a
    recording for a row that has one.
    """

    device: ExportDevice | None = None
    started_at: Annotated[
        DiveLocalStartTime | None,
        Field(
            default=None,
            description="This device's own start, when it differs from the dive's. Absent means the dive's (§6.4a).",
        ),
    ]
    source_files: Annotated[list[ExportStoredFile], Field(default_factory=list, description="In attach order")]
    profile: DiveProfileRead | None = None


class ExportDive(PublicUUIDSchema):
    """One dive, with everything that hangs off it embedded rather than referenced.

    `site_uuids` is in visit order - index 0 is the primary site - which is the ordering
    UDDF cannot express and the reason this list exists at all. `gear_uuids` and
    `species_uuids` are the diver's own order in the same way.

    **`source_file` and `profile` are not members of a dive**, and that is the format's
    change rather than this app's preference: both moved onto `recordings[]` with nothing
    left behind, because a dive can be recorded by more than one computer and a copy on the
    dive would be one more invariant for a writer to break. A reader wanting "the" profile
    takes `recordings[0].profile`.

    A recording's `profile` is the full per-sample payload in the same integer scales the
    API serves, so the document alone can redraw every curve without re-parsing the files.
    It is `DiveProfileRead`, the base of what `GET /dive/{uuid}/recording/{rid}/profile`
    returns: one profile vocabulary on both surfaces, rather than a second set of models
    that could drift. That route serves `RecordingProfileRead`, which adds a `provenance`
    the format has no core member for and which therefore cannot ride the document's
    `profile` object - the schema closes it.
    """

    dive_number: int
    # Offset-aware wherever the source recorded an offset, and offset-less where it did
    # not - spec §5.2's local date-time, which the writer emits verbatim from the column
    # pair rather than fabricating a zone for. `exported_at` on the envelope is the one
    # member that must always carry one, and it is generated rather than recorded.
    started_at: DiveLocalStartTime
    duration: Annotated[int, Field(description="Dive duration in seconds, as logged")]
    notes: str | None = None
    max_depth: float | None = None
    avg_depth: Annotated[
        float | None, Field(default=None, description="Never greater than `max_depth` - a checked rule, spec §6.2")
    ]
    bottom_temperature: Annotated[float | None, Field(default=None, description="In degrees Celsius")]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    weight: Annotated[float | None, Field(default=None, description="Total ballast carried, in kilograms")]
    # Water type is in this document and in `dives.csv`, and in neither UDDF: 3.2.2 has no
    # *per-dive* salinity or density slot at all - the `density` elements it does have are
    # site-level (`sitedata`) and deco-planner input (`baseCalculationType`), neither of
    # which is a fact about one dive. Altitude does have one, and UDDF gets it. See
    # DECISIONS.md.
    water_type: WaterType | None = None
    altitude: Annotated[
        int | None, Field(default=None, description="Elevation of the water surface, in meters above sea level")
    ]
    cns_start: float | None = None
    cns_end: float | None = None
    otu_start: float | None = None
    otu_end: float | None = None
    surface_pressure: Annotated[float | None, Field(default=None, description="Ambient surface pressure, in bar")]
    # UDDF 3.2.2 has nowhere to put a per-dive position - its only `<geography>` hangs off
    # a `<site>`, and neither `informationbeforedive` nor `waypoint` has a coordinate
    # element - so this document and `dives.csv` are the only two exports that carry them.
    # See DECISIONS.md.
    entry_position: ExportPosition | None = None
    exit_position: ExportPosition | None = None
    trip_uuid: uuid_pkg.UUID | None = None
    course_uuid: uuid_pkg.UUID | None = None
    site_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list, description="In visit order")]
    gear_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list, description="In the diver's own order")]
    species_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list, description="In the diver's own order")]
    # `DiveMixtureBase` rather than the API's `DiveMixtureRead`, which carries the
    # internal row `id`. Nothing here references a cylinder, so that id would be the one
    # integer key in the document. Its member names are the spec's Cylinder members
    # already, `gas_number` included.
    cylinders: Annotated[list[DiveMixtureBase], Field(default_factory=list)]
    recordings: Annotated[
        list[ExportRecording],
        Field(default_factory=list, description="What recorded this dive, in order; the first is primary"),
    ]
    created_at: datetime


class ExportTripLocation(BaseModel):
    """One place a trip went, as the geocoder described it when the diver picked it.

    A value object with no `uuid`, because it has none to export: trip locations are
    per-trip rows replaced wholesale with the trip, so nothing in this document - or in
    the database - references one. The bounding box travels with the point because it is
    what the geocoder said the place *covers*, and a reader redrawing the trip's map wants
    the region rather than a pin in the middle of a country.
    """

    name: str
    display_name: str | None = None
    position: ExportPosition | None = None
    bbox: ExportBoundingBox | None = None


class ExportTrip(PublicUUIDSchema):
    name: str
    locations: Annotated[
        list[ExportTripLocation],
        Field(default_factory=list, description="Places this trip went to, in the order the diver listed them"),
    ]
    starts_on: date
    ends_on: date | None = None
    notes: str | None = None
    created_at: datetime


class ExportCourse(PublicUUIDSchema):
    """One training course, with no reference of its own.

    The link is on the *children*: an `ExportDive` and an `ExportCertification` each carry
    a `course_uuid`, so a reader rebuilds the grouping by walking those rather than by
    reading a list here. Same direction as `ExportTrip`, and for the same reason - the
    course is the thing that keeps existing when a dive is deleted.
    """

    name: str
    agency: CertificationAgency
    agency_other: str | None = None
    status: CourseStatus
    starts_on: date | None = None
    ends_on: date | None = None
    instructor_name: str | None = None
    instructor_number: str | None = None
    training_center: str | None = None
    notes: str | None = None
    created_at: datetime


class ExportDiveSite(PublicUUIDSchema):
    name: str
    location: str | None = None
    position: ExportPosition | None = None
    notes: str | None = None
    created_at: datetime


class ExportSpecies(PublicUUIDSchema):
    """One species from the catalog, as far as this diver's dives reference it.

    The odd one out in this document: every other collection here is the diver's own rows,
    while the species catalog belongs to nobody (see `models/species.py`). What is
    exported is the slice the logbook points at, which is what makes the document
    self-contained - a reader resolving `ExportDive.species_uuids` finds every one of them
    defined here.

    `aphia_id` is the field that matters outside this database, and the format says so:
    the uuids are this instance's, the AphiaID is the World Register of Marine Species'
    own identifier and the interchange identity a reader matches its own catalog on (spec
    §6.11). `wikidata_qid` does the same job for anything that would rather start from
    Wikidata. The record is a snapshot for human readers, never a source a reader creates
    catalog rows from.
    """

    aphia_id: int
    scientific_name: str
    common_name: str | None = None
    rank: str
    wikidata_qid: str | None = None
    created_at: datetime


class ExportGearItem(PublicUUIDSchema):
    name: str
    brand: str | None = None
    type: GearType | None = None
    notes: str | None = None
    rented: bool
    archived: bool
    archived_at: datetime | None = None
    dive_count: int
    created_at: datetime


class ExportGearSet(PublicUUIDSchema):
    name: str
    weight: float | None = None
    gear_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list, description="In the set's own order")]
    created_at: datetime


class ExportGearServiceSchedule(PublicUUIDSchema):
    gear_uuid: uuid_pkg.UUID
    type: ServiceKind
    label: str | None = None
    starts_on: date
    interval_months: int | None = None
    interval_dives: int | None = None
    dive_count_at_start: int
    active: bool
    last_service_on: date | None = None
    next_due_on: date | None = None
    next_due_at_dive_count: int | None = None
    created_at: datetime


class ExportGearServiceRecord(PublicUUIDSchema):
    gear_uuid: uuid_pkg.UUID
    gear_service_schedule_uuid: uuid_pkg.UUID | None = None
    type: ServiceKind
    serviced_on: date
    dive_count_at_service: int
    label: str | None = None
    performed_by: str | None = None
    notes: str | None = None
    created_at: datetime


class ExportCertification(PublicUUIDSchema):
    """One c-card.

    The two scans are `front_file`/`back_file` rather than a list with a side
    discriminator, because a card has one front and one back and a list could claim two
    fronts (spec §6.16).
    """

    agency: CertificationAgency
    agency_other: str | None = None
    name: str
    certification_number: str | None = None
    certified_on: date | None = None
    expires_on: date | None = None
    instructor_name: str | None = None
    instructor_number: str | None = None
    training_center: str | None = None
    course_uuid: uuid_pkg.UUID | None = None
    notes: str | None = None
    front_file: ExportStoredFile | None = None
    back_file: ExportStoredFile | None = None
    created_at: datetime


class ExportEnvelope(BaseModel):
    """The whole document, in the member order spec §4 declares.

    **Never used to serialize.** `services/export/envelope.py` streams the file a record
    at a time so a thousand-dive log with its profiles never sits in memory whole, which
    means this model would drift out of step with the real output if nothing checked it.
    `tests/test_export_json.py` closes that gap by validating the streamed bytes against
    this model *and* against the vendored JSON Schema and the spec's beyond-schema rules -
    so it is the app's statement of the format, kept honest by a test rather than by being
    on the write path.
    """

    format: str = DIVEJSON_FORMAT
    version: str = DIVEJSON_VERSION
    exported_at: datetime
    generator: ExportGenerator
    diver: ExportDiver
    dives: Annotated[list[ExportDive], Field(default_factory=list)]
    trips: Annotated[list[ExportTrip], Field(default_factory=list)]
    courses: Annotated[list[ExportCourse], Field(default_factory=list)]
    sites: Annotated[list[ExportDiveSite], Field(default_factory=list)]
    species: Annotated[list[ExportSpecies], Field(default_factory=list)]
    gear: Annotated[list[ExportGearItem], Field(default_factory=list)]
    gear_sets: Annotated[list[ExportGearSet], Field(default_factory=list)]
    gear_service_schedules: Annotated[list[ExportGearServiceSchedule], Field(default_factory=list)]
    gear_service_records: Annotated[list[ExportGearServiceRecord], Field(default_factory=list)]
    certifications: Annotated[list[ExportCertification], Field(default_factory=list)]
