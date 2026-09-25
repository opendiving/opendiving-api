"""The shapes logbook import reads and reports - a DiveJSON document seen from the
*reader* side, and the report that says what importing one would do.

A converted upload - a UDDF file, a `.ssrf`, a FIT, a Suunto app or DM5 XML export, a zip
of any one of them - arrives here as a DiveJSON document like any other, because the converter's
output is one. Nothing below the reader learns an upload was converted; the only trace is
`ImportReport.conversion`, which is what the conversion could not carry.

**This is not `schemas/export.py` inverted, and the differences are the whole point.**
That module is the writer's declaration: it emits exactly what DiveJSON 1.0 defines and a
test holds it to the schema. A reader has different obligations, all of them in the
specification's own words:

- **Unknown members are ignored** (spec §5.6), because minor versions are additive and a
  reader must not reject a `1.1` document for carrying something it has not heard of. So
  every model here is `extra="ignore"` rather than `extra="forbid"`, and the envelope
  carries the top-level `extensions` member `ExportEnvelope` deliberately does not - a
  foreign producer may put one there and refusing it would be non-conforming.
- **An unknown value in a closed vocabulary reads as absent** (spec §5.6 again), which is
  what makes that additive promise hold for a member like a gear item's `type`. Pydantic
  would raise on one, so every enum member here goes through `_unknown_is_absent`.
- **A stray `null` reads as absent** (spec §5.4). Writers must not emit one, but a reader
  that meets one SHOULD carry on, so every optional member is `T | None` and every
  collection tolerates a null in place of an empty array.

What this deliberately does **not** do is check conformance. `divejson.validate_document`
is the writer-facing statement of spec §3, and this importer is not a second copy of it: it
skips and reports a dangling reference where the validator rejects the document, because a
reader's job is to salvage a logbook rather than to grade one. That the package is now on
the request path changes nothing here - a converter validates what it writes, which is a
writer's obligation, and the importer still grades nothing it is handed. `DECISIONS.md`,
*"The importer is a reader, not a validator"*, has the reasoning.

String bounds *are* enforced here, and they are the specification's own (§6): a value
longer than the format allows is a malformed document rather than something to truncate,
and every one of these limits is also the width of the column it lands in. `notes` has none,
the format having dropped its cap - a note is prose, and the planner stores what this app's
own cap admits (`NOTES_MAX_LENGTH`) and reports the rest.
"""

import uuid as uuid_pkg
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from ..core.utils.datetime_offset import full_date_is_a_date
from .certification import CertificationAgency
from .course import CourseStatus
from .dive import DecoAlgorithm, DiveMode, Salinity, WaterType
from .dive_mixture import GasRole, TankUsage
from .dive_profile import ProfileEventType
from .gear_service import ServiceKind
from .user import (
    ANCHOR_REQUIRED_MESSAGES,
    EMERGENCY_CONTACT_FIELDS,
    INSURANCE_FIELDS,
    BirthDate,
    CheckInName,
    CheckInPhone,
    CheckInShortText,
    is_blank,
    lacks_its_anchor,
)

# The spec's own string bounds (§6). Named rather than repeated inline because each one
# governs several members, and because each is also the width of the column behind it -
# so a mismatch between the two is a bug worth being able to find by name.
_NAME_MAX = 255
_LABEL_MAX = 120
_SHORT_MAX = 64
_FULL_NAME_MAX = 512
_QID_MAX = 32
_SHA256_LENGTH = 64
# §6.4b's own bounds on a device's members, and the widths of `dive_recording`'s columns.
_DEVICE_MAX = 64
_FIRMWARE_MAX = 32
# §6.1's bound on a phone number, the diver's and an emergency contact's alike.
_PHONE_MAX = 32


def _unknown_is_absent(enum: type[StrEnum]) -> BeforeValidator:
    """Spec §5.6, as a validator: a value outside the vocabulary reads as *not recorded*.

    Minor versions may add members to an OPTIONAL closed set, and a reader implementing
    1.0 has to keep working when it meets one - "treat that member as absent" is the
    format's own answer, and it is a MUST. Pydantic's default is the opposite, so without
    this a single unfamiliar gear type would 422 a whole logbook.

    Applied to the REQUIRED vocabularies too (a certification's `agency`, a service
    record's `type`), where absent then makes the record uninterpretable and the planner
    skips it with a note. That is the same rule followed to its conclusion rather than a
    second one: §7 freezes REQUIRED vocabularies precisely so this case does not arise
    from a conforming document.
    """
    values = {member.value for member in enum}

    def coerce(value: Any) -> Any:
        return value if value in values else None

    return BeforeValidator(coerce)


def _null_is_empty(value: Any) -> Any:
    """A collection written as `null` reads as absent, i.e. as the empty one (spec §5.4).

    An absent collection already means empty (§4). A writer must not emit the null, but a
    reader that meets one has an unambiguous reading available and refusing it would fail
    a document over a member it did not need.
    """
    return [] if value is None else value


_Collection = BeforeValidator(_null_is_empty)

# A start as a document spells it: a date-time, or on a dive a bare date.
ImportStart = Annotated[datetime | date | None, BeforeValidator(full_date_is_a_date), Field(default=None)]


class _ReadModel(BaseModel):
    """Every model in this module ignores members it does not know (spec §5.6)."""

    model_config = ConfigDict(extra="ignore")


class ImportGenerator(_ReadModel):
    name: str | None = None
    version: str | None = None


class ImportPosition(_ReadModel):
    """Both halves required - half a coordinate is unrepresentable, and the app's
    `ck_*_position_pair` constraints refuse one anyway."""

    latitude: float
    longitude: float


class ImportBoundingBox(_ReadModel):
    south: float
    north: float
    west: float
    east: float


class ImportEmergencyContact(_ReadModel):
    """`name` is REQUIRED in the format and optional here: the importer grades nothing, and
    the planner drops a contact nobody is named in with a note rather than offering it."""

    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    phone: Annotated[str | None, Field(default=None, max_length=_PHONE_MAX)]
    relationship: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]


class ImportInsurance(_ReadModel):
    """`provider` is optional here for the reason `ImportEmergencyContact.name` is."""

    provider: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    number: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    expires_on: date | None = None


class ImportStoredFile(_ReadModel):
    """Metadata for a binary. In a bare document there is nothing behind it, which is why
    `archive_path` is what tells the two paths apart (spec §6.7)."""

    uuid: uuid_pkg.UUID | None = None
    original_filename: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    content_type: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    byte_size: int | None = None
    sha256: Annotated[
        str | None, Field(default=None, min_length=_SHA256_LENGTH, max_length=_SHA256_LENGTH, pattern=r"^[0-9a-f]+$")
    ]
    archive_path: str | None = None
    extensions: dict[str, Any] | None = None


class ImportDiver(_ReadModel):
    """Whose logbook the document is. Its identity and settings are read and reported, and
    never applied; its check-in details and its portrait are offered in the preview and
    written as the diver confirms them - see `DECISIONS.md`. Every member is optional
    because §6.1 makes them so: a converter whose source records nothing about an owner
    omits the whole object rather than minting identity for a person."""

    uuid: uuid_pkg.UUID | None = None
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    username: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    email: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    phone: Annotated[str | None, Field(default=None, max_length=_PHONE_MAX)]
    born_on: date | None = None
    emergency_contacts: Annotated[list[ImportEmergencyContact], Field(default_factory=list), _Collection]
    insurances: Annotated[list[ImportInsurance], Field(default_factory=list), _Collection]
    portrait_file: ImportStoredFile | None = None
    created_at: datetime | None = None
    extensions: dict[str, Any] | None = None


class ImportSeries(_ReadModel):
    times: Annotated[list[int], Field(default_factory=list), _Collection]
    values: Annotated[list[int], Field(default_factory=list), _Collection]


class ImportPressureSeries(ImportSeries):
    gas_number: int | None = None


class ImportEvent(_ReadModel):
    time: int | None = None
    type: Annotated[ProfileEventType | None, _unknown_is_absent(ProfileEventType), Field(default=None)]
    gas_number: int | None = None
    label: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]


class ImportProfile(_ReadModel):
    duration: int | None = None
    depth: ImportSeries | None = None
    ceiling: ImportSeries | None = None
    temperature: ImportSeries | None = None
    pressures: Annotated[list[ImportPressureSeries], Field(default_factory=list), _Collection]
    # The six decompression channels, read back on exactly the terms they are written. §6.4
    # floors all six at zero, and the planner re-validates each one under §6.5's rules the
    # way it does the three above - a document is not trusted for having been written by
    # this app.
    ndl: ImportSeries | None = None
    tts: ImportSeries | None = None
    ppo2: ImportSeries | None = None
    cns: ImportSeries | None = None
    gradient_factor: ImportSeries | None = None
    surface_gradient_factor: ImportSeries | None = None
    events: Annotated[list[ImportEvent], Field(default_factory=list), _Collection]


class ImportDevice(_ReadModel):
    """What recorded one recording, as the document names it (spec §6.4b).

    Every member is bounded to the length of the column it lands in, unlike most of this
    module: these do not merely feed a report, they are stored on `dive_recording` and its
    device columns are `String(64)` (and `String(32)` for firmware). A document naming a
    serial longer than the column is a document this app cannot store that member of, and a
    Pydantic bound is where that is decided rather than an `IntegrityError` mid-import.
    """

    brand: Annotated[str | None, Field(default=None, max_length=_DEVICE_MAX)]
    model: Annotated[str | None, Field(default=None, max_length=_DEVICE_MAX)]
    serial: Annotated[str | None, Field(default=None, max_length=_DEVICE_MAX)]
    firmware: Annotated[str | None, Field(default=None, max_length=_FIRMWARE_MAX)]
    name: Annotated[str | None, Field(default=None, max_length=_DEVICE_MAX)]
    dive_number: int | None = None


class ImportDecoModel(_ReadModel):
    """The decompression model one device ran on one dive (spec §6.4c).

    `algorithm` goes through `_unknown_is_absent` like every other closed vocabulary here:
    the member is OPTIONAL, so §7 lets the family list grow in a minor version, and a `vpm`
    from a 1.1 document has to read as "not recorded" rather than 422 a whole logbook.

    `name` is bounded to the format's own 64, which is also `dive_recording.deco_name`'s
    width - a document naming a model longer than the column is one this app cannot store
    that member of, and a Pydantic bound is where that is decided rather than an
    `IntegrityError` mid-import.

    The three integers are bounded by the planner rather than here, with the pair's ordering
    rule beside them: the bounds are droppable per member and a drop wants a note, which is
    what `_bounded` is for.
    """

    algorithm: Annotated[DecoAlgorithm | None, _unknown_is_absent(DecoAlgorithm), Field(default=None)]
    name: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    gf_low: int | None = None
    gf_high: int | None = None
    conservatism: int | None = None


class ImportRecording(_ReadModel):
    """One device's record of one dive (spec §6.4a).

    `started_at` absent means *the dive's*, which is the format's rule and not a default this
    app invented - so the planner substitutes the dive's start rather than leaving the column
    NULL, and a recording whose device entered the water later carries its own.

    A recording with none of `device`, `profile`, `source_files` and a readout describes
    nothing (§3's rule 4) and the planner drops it with a note rather than creating an empty
    row. **`mode`, `deco_model` and `salinity` are not on that list**, which the spec states
    outright: a setting with nothing recorded behind it is a setting nothing recorded a dive
    with.
    """

    device: ImportDevice | None = None
    mode: Annotated[DiveMode | None, _unknown_is_absent(DiveMode), Field(default=None)]
    deco_model: ImportDecoModel | None = None
    salinity: Annotated[Salinity | None, _unknown_is_absent(Salinity), Field(default=None)]
    started_at: ImportStart
    surface_pressure: float | None = None
    cns_start: float | None = None
    cns_end: float | None = None
    otu_start: float | None = None
    otu_end: float | None = None
    source_files: Annotated[list[ImportStoredFile], Field(default_factory=list), _Collection]
    profile: ImportProfile | None = None


class ImportCylinder(_ReadModel):
    volume: float | None = None
    start_pressure: float | None = None
    end_pressure: float | None = None
    oxygen: float | None = None
    helium: float | None = None
    ppo2_limit: float | None = None
    gas_number: int | None = None
    role: Annotated[GasRole | None, _unknown_is_absent(GasRole), Field(default=None)]
    usage: Annotated[TankUsage | None, _unknown_is_absent(TankUsage), Field(default=None)]


class ImportDive(_ReadModel):
    uuid: uuid_pkg.UUID
    number: int | None = None
    # No offset validator, unlike `DiveCreate` in `schemas/dive.py`: an offset-less
    # `started_at` is spec §5.2's local date-time, and admitting it is the reason
    # `dive.utc_offset_minutes` became nullable. `DiveUpdate` has since dropped its
    # validator too, but for the narrower reason that it may only *preserve* what this
    # endpoint created - see `core/utils/datetime_offset.py`. A bare date reads as a `date`,
    # which the planner stores as the date-only state.
    started_at: ImportStart
    duration: int | None = None
    notes: str | None = None
    max_depth: float | None = None
    avg_depth: float | None = None
    bottom_temperature: float | None = None
    # A number on the wire (spec §6.2 - half-metre visibility is a real low-vis fact) and
    # an `Integer` column here, which is the one place the format is finer than the app.
    visibility: float | None = None
    weight: float | None = None
    water_type: Annotated[WaterType | None, _unknown_is_absent(WaterType), Field(default=None)]
    altitude: int | None = None
    entry_position: ImportPosition | None = None
    exit_position: ImportPosition | None = None
    trip_uuid: uuid_pkg.UUID | None = None
    course_uuid: uuid_pkg.UUID | None = None
    site_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list), _Collection]
    gear_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list), _Collection]
    species_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list), _Collection]
    cylinders: Annotated[list[ImportCylinder], Field(default_factory=list), _Collection]
    # **`source_file` and `profile` are not members of a dive any more**, and this reader
    # does not accept them under those names: `extra="ignore"` means a 1.0 document written
    # before the change is read as a dive with no recordings rather than failing, which is
    # the tolerance §5.6 asks for and the only behaviour available - there is no version
    # member distinguishing the two shapes, 1.0 being untagged when the members moved.
    recordings: Annotated[list[ImportRecording], Field(default_factory=list), _Collection]
    created_at: datetime | None = None


class ImportLocation(_ReadModel):
    """A named place (spec §6.9), read the same way on both hosts.

    `name` is REQUIRED in the format and optional here, because a reader salvages rather
    than grades: a place with no name is something the planner drops with a note, not a
    document it refuses.
    """

    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    full_name: Annotated[str | None, Field(default=None, max_length=_FULL_NAME_MAX)]
    position: ImportPosition | None = None
    bbox: ImportBoundingBox | None = None


class ImportTripPart(_ReadModel):
    starts_on: date | None = None
    ends_on: date | None = None
    location: ImportLocation | None = None


class ImportTrip(_ReadModel):
    uuid: uuid_pkg.UUID
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    # **A trip's `starts_on`, `ends_on` and `locations` are not members any more**, and
    # this reader does not accept them under those names: `extra="ignore"` means a 1.0
    # document written before the change is read as a trip with no parts rather than
    # failing, the same tolerance §5.6 asks for that a dive's `recordings` gets above.
    parts: Annotated[list[ImportTripPart], Field(default_factory=list), _Collection]
    notes: str | None = None
    created_at: datetime | None = None


class ImportCourse(_ReadModel):
    uuid: uuid_pkg.UUID
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    agency: Annotated[CertificationAgency | None, _unknown_is_absent(CertificationAgency), Field(default=None)]
    agency_other: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    status: Annotated[CourseStatus | None, _unknown_is_absent(CourseStatus), Field(default=None)]
    starts_on: date | None = None
    ends_on: date | None = None
    instructor_name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    instructor_number: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    training_center: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    notes: str | None = None
    created_at: datetime | None = None


class ImportDiveSite(_ReadModel):
    """One dive site (spec §6.10).

    **`location` is an object, and a document that spells it as a string is refused** - not
    read as a name, not salvaged. §6.9 defines one shape and this reader implements it;
    `reader.py` turns the resulting type error into a sentence naming the cause, because a
    diver holding an export written before the change can do something about it.

    `position` is the site's own pin. The locality's centre is `location.position`, and
    neither is read into the other.
    """

    uuid: uuid_pkg.UUID
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    location: ImportLocation | None = None
    position: ImportPosition | None = None
    notes: str | None = None
    created_at: datetime | None = None


class ImportSpecies(_ReadModel):
    """The one collection that never becomes rows of the caller's. `aphia_id` is the
    interchange identity (spec §6.11) and the only member this importer acts on - the rest
    is a snapshot for human readers, and creating a catalog row from it is what design
    decision 7 forbids."""

    uuid: uuid_pkg.UUID
    aphia_id: int | None = None
    scientific_name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    common_name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    rank: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    wikidata_qid: Annotated[str | None, Field(default=None, max_length=_QID_MAX)]
    created_at: datetime | None = None


class ImportGearItem(_ReadModel):
    uuid: uuid_pkg.UUID
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    brand: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    # `GearType` is not imported as an enum here on purpose: `schemas/gear_item.py` owns
    # the vocabulary, and this is the one place a value outside it must read as absent
    # rather than as a 422.
    type: str | None = None
    notes: str | None = None
    rented: bool | None = None
    archived: bool | None = None
    archived_at: datetime | None = None
    # Derived (spec §5.7): read so the report can say it was, recomputed on import.
    dive_count: int | None = None
    created_at: datetime | None = None


class ImportGearSet(_ReadModel):
    uuid: uuid_pkg.UUID
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    weight: float | None = None
    gear_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list), _Collection]
    created_at: datetime | None = None


class ImportGearServiceSchedule(_ReadModel):
    uuid: uuid_pkg.UUID
    gear_uuid: uuid_pkg.UUID | None = None
    type: Annotated[ServiceKind | None, _unknown_is_absent(ServiceKind), Field(default=None)]
    label: Annotated[str | None, Field(default=None, max_length=_LABEL_MAX)]
    starts_on: date | None = None
    interval_months: int | None = None
    interval_dives: int | None = None
    # A snapshot, not a derived member (spec §5.7): imported as recorded, because no
    # recomputation procedure exists for "the item's lifetime count when this rule began".
    dive_count_at_start: int | None = None
    active: bool | None = None
    # Derived (spec §5.7) - recomputed by `recalculate_service_schedule` after the write.
    last_service_on: date | None = None
    next_due_on: date | None = None
    next_due_at_dive_count: int | None = None
    created_at: datetime | None = None


class ImportGearServiceRecord(_ReadModel):
    uuid: uuid_pkg.UUID
    gear_uuid: uuid_pkg.UUID | None = None
    gear_service_schedule_uuid: uuid_pkg.UUID | None = None
    type: Annotated[ServiceKind | None, _unknown_is_absent(ServiceKind), Field(default=None)]
    serviced_on: date | None = None
    dive_count_at_service: int | None = None
    label: Annotated[str | None, Field(default=None, max_length=_LABEL_MAX)]
    performed_by: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    notes: str | None = None
    created_at: datetime | None = None


class ImportCertification(_ReadModel):
    uuid: uuid_pkg.UUID
    agency: Annotated[CertificationAgency | None, _unknown_is_absent(CertificationAgency), Field(default=None)]
    agency_other: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    number: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    certified_on: date | None = None
    expires_on: date | None = None
    instructor_name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    instructor_number: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    training_center: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    course_uuid: uuid_pkg.UUID | None = None
    notes: str | None = None
    front_file: ImportStoredFile | None = None
    back_file: ImportStoredFile | None = None
    created_at: datetime | None = None


class ImportDocument(_ReadModel):
    """A whole DiveJSON document as this reader sees it.

    `format` and `version` are the only required members: they are what a reader dispatches
    on before parsing further (spec §4), and a payload without them is not a DiveJSON
    document at all - which is a 415 rather than a 422, exactly as an unrecognized
    dive-computer export is at `POST /dive/parse`.

    `extensions` is here as on `ExportEnvelope`: this app's writer marks its profile axis
    there, and a reader that refused a foreign producer's would be violating §5.6.
    """

    format: str
    version: str
    exported_at: datetime | None = None
    generator: ImportGenerator | None = None
    diver: ImportDiver | None = None
    dives: Annotated[list[ImportDive], Field(default_factory=list), _Collection]
    trips: Annotated[list[ImportTrip], Field(default_factory=list), _Collection]
    courses: Annotated[list[ImportCourse], Field(default_factory=list), _Collection]
    sites: Annotated[list[ImportDiveSite], Field(default_factory=list), _Collection]
    species: Annotated[list[ImportSpecies], Field(default_factory=list), _Collection]
    gear: Annotated[list[ImportGearItem], Field(default_factory=list), _Collection]
    gear_sets: Annotated[list[ImportGearSet], Field(default_factory=list), _Collection]
    gear_service_schedules: Annotated[list[ImportGearServiceSchedule], Field(default_factory=list), _Collection]
    gear_service_records: Annotated[list[ImportGearServiceRecord], Field(default_factory=list), _Collection]
    certifications: Annotated[list[ImportCertification], Field(default_factory=list), _Collection]
    extensions: dict[str, Any] | None = None


# ---------------------------------------------------------------- the report


class ImportNoteCode(StrEnum):
    """Why one record or value did not import exactly as the document described it.

    A code beside the sentence so a client can group, count and style the notes without
    parsing prose - the `MixtureImportNotes` discipline (`opendiving-web`) raised from one
    dive to a whole logbook. The sentence is still what a diver reads; the code is what a
    UI sorts on.
    """

    # The record cannot be represented at all, so nothing was written for it.
    RECORD_SKIPPED = "record_skipped"
    # An existing row of the caller's already says this, so the import pointed at it
    # rather than creating a duplicate.
    RECORD_LINKED = "record_linked"
    # The two remap codes say the same thing about *this* record - it was imported under an
    # identifier other than the one the document gave it, and neither is an error. They
    # differ in what happened to every **other** record's reference to that identifier, and
    # the name carries that rather than the sentence alone, so a client can branch on it.
    #
    # The document's uuid belongs to another account on this instance. The record is created
    # under a fresh one and every reference to it is rewritten to follow it.
    RECORD_REMAPPED_REFERENCES_FOLLOW = "record_remapped_references_follow"
    # Two records of one collection claim the same uuid (not conforming - spec §5.3). The
    # *second* is created under a fresh one and nothing is rewritten, so references to that
    # identifier stay where the document put them: on the first of the two.
    RECORD_REMAPPED_REFERENCES_STAY = "record_remapped_references_stay"
    # A soft-deleted row of the caller's came back, under its original uuid.
    RECORD_RESTORED = "record_restored"
    # The document was written before a change to the format, by a writer this app knows, and
    # a value was read as that writer meant it - a profile axis in seconds, a readout on the
    # dive, `en13319` as a dive's water, `po2_limit`. Nothing was lost.
    READ_AS_WRITTEN = "read_as_written"
    # The record imported, but one of its values could not be stored as written.
    VALUE_DROPPED = "value_dropped"
    # A reference names a record the document does not define, or one that was skipped.
    REFERENCE_UNRESOLVED = "reference_unresolved"
    # A species could not be matched to this instance's catalog, so the sighting link
    # was dropped. Never a reason to drop the dive.
    SPECIES_UNRESOLVED = "species_unresolved"
    # A file the document references whose bytes are not in it.
    FILE_NOT_CONTAINED = "file_not_contained"
    # A file whose bytes are here but which cannot be stored against this account.
    FILE_SKIPPED = "file_skipped"
    # A recording of a dive the document describes as new turned out to be a second
    # computer's record of a dive the caller already has, so it was added to that dive
    # instead of a second dive being created for it.
    RECORDING_ATTACHED = "recording_attached"
    # A recording matched one the caller already has - the same device, the same start -
    # so it filled that recording's blanks rather than being added beside it.
    RECORDING_FILLED = "recording_filled"
    # The `diver` member's identity and settings were read and deliberately not applied.
    DIVER_NOT_APPLIED = "diver_not_applied"
    # An emergency contact or an insurance in the document is not offered: it names nobody
    # or no insurer, or it comes after the first and the account holds one of each.
    CHECK_IN_DETAIL_DROPPED = "check_in_detail_dropped"
    # A check-in detail the diver confirmed in the preview was written to the account; the
    # portrait they took from the archive among them.
    CHECK_IN_DETAIL_WRITTEN = "check_in_detail_written"
    # The diver took the archive's portrait, and the account's changed after the preview they
    # chose from, so the account's was kept.
    PORTRAIT_KEPT = "portrait_kept"


class ImportNote(BaseModel):
    """One thing the import decided, addressed to the diver.

    `uuid` is the document's own identifier for the record, not the row's: on the remap
    branch there is no row yet at preview time, and the document is what the diver can
    look the record up in.
    """

    code: ImportNoteCode
    collection: Annotated[
        str | None,
        Field(default=None, description="Which envelope collection this is about, e.g. `dives`", examples=["dives"]),
    ]
    uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="The record's uuid as the document spells it")
    ]
    message: Annotated[str, Field(description="One sentence, ready to render")]


class ImportCollectionReport(BaseModel):
    """What would happen (preview) or did happen (apply) to one envelope collection.

    The four counts are disjoint and sum to the number of records the document carries in
    this collection. `restored` is its own figure and never hides inside `created` or
    `skipped`: un-deleting is the one thing this feature does that no other surface in the
    app can, and a diver restoring a backup is entitled to see it counted.
    """

    collection: Annotated[str, Field(examples=["dives"])]
    created: int
    linked: Annotated[int, Field(description="Matched an existing row of the caller's; nothing was written")]
    restored: Annotated[int, Field(description="A soft-deleted row of the caller's, brought back under its own uuid")]
    skipped: int


class ImportFileReport(BaseModel):
    """The logbook's binaries - dive-computer files and card images - which follow different
    rules from the records that reference them.

    A bare document carries file *metadata* and no bytes, so `restored` is zero and
    `not_contained` is every referenced file - which is not an error, just a smaller
    restore. Only an archive can put bytes back. The diver's portrait is not among them: it
    is offered apart, beside the check-in details.
    """

    referenced: Annotated[int, Field(description="Dive-computer files and card images the document names")]
    restored: Annotated[int, Field(description="Files whose bytes were written to this instance")]
    not_contained: Annotated[int, Field(description="Files with no bytes in this document - import the archive")]
    skipped: Annotated[int, Field(description="Files whose bytes are here but could not be stored")]


class ConversionConverter(BaseModel):
    """What converted the upload, so a report can be attributed to a version of it."""

    name: Annotated[str, Field(examples=["divejson"])]
    version: Annotated[str, Field(examples=["0.3.0"])]


class ConversionNoteGroup(BaseModel):
    """One thing the conversion could not carry, and everywhere it came up.

    **`kind` is an opaque string and must stay one.** The converter's kind set grew from
    three to four while this feature was being built, and the pin that decides which set
    this build sees moves without anybody here touching a line - so a kind this app has
    never seen can arrive between one deploy and the next. As a `StrEnum` or a `Literal`
    that would raise while *serialising the response*, turning a readable logbook into a
    500; as a string it renders as itself and a client styles what it knows. This is the
    api half of a tolerance the web card relies on, and the deliberate opposite of
    `ImportNote.code`, which is this app's own closed vocabulary and can be an enum
    precisely because nothing outside this repository adds to it.
    """

    kind: Annotated[
        str,
        Field(
            description="What sort of finding this is - `absent`, `inferred`, `resolved`, `dropped` at the time of "
            "writing. Treat an unfamiliar value as a plain finding rather than an error.",
            examples=["absent"],
        ),
    ]
    message: Annotated[str, Field(description="One sentence, ready to render")]
    count: Annotated[int, Field(description="How many places raised this, which may be more than `wheres` lists")]
    wheres: Annotated[
        list[str],
        Field(
            default_factory=list,
            description="Up to three paths into the source document, e.g. `dive/0/tankdata/1`",
            examples=[["dive/0", "dive/3"]],
        ),
    ]


class ConversionReport(BaseModel):
    """What converting a non-DiveJSON upload could not carry. `null` for a native document.

    Grouped here rather than in the browser, and rather than folded into `notes`: one source
    habit makes one finding per record - eight dives with no UTC offset are eight findings -
    and the converter's list is unbounded, where `notes` has the planner's 500-note cap. The
    grouping is where a cap can live at all, which is also why these are not another
    `ImportNoteCode`: a conversion finding has a source path rather than a uuid and a
    collection, and none of those codes describes it.
    """

    format: Annotated[
        str, Field(description="The format the upload was read as, as the converter names it", examples=["uddf"])
    ]
    converter: ConversionConverter
    groups: Annotated[
        list[ConversionNoteGroup],
        Field(default_factory=list, description="Findings grouped by kind and message, in first-seen order"),
    ]
    groups_truncated: Annotated[
        int,
        Field(
            default=0,
            description="Groups beyond the cap that are not in `groups`. Non-zero means the list above is a prefix.",
        ),
    ]


# ---------------------------------------------------------------- the check-in details


class ImportCheckInEmergencyContact(BaseModel):
    """An emergency contact as the preview shows it and as the diver sends it back.

    One shape for both directions, so a client edits the proposal and submits what it holds.
    The bounds are `UserUpdate`'s for the same columns, which is what a submission has to
    meet; `name` is optional in the shape because the account's own contact may lack one,
    and a submission that leaves it out beside a phone or a relationship is refused.
    """

    model_config = ConfigDict(extra="forbid")

    name: CheckInName | None = None
    phone: CheckInPhone | None = None
    relationship: CheckInShortText | None = None


class ImportCheckInInsurance(BaseModel):
    """A dive insurance, on `ImportCheckInEmergencyContact`'s terms, `provider` its anchor."""

    model_config = ConfigDict(extra="forbid")

    provider: CheckInName | None = None
    number: CheckInShortText | None = None
    expires_on: date | None = None


class ImportBornOnDetail(BaseModel):
    """The date of birth: what the account holds, and what the document proposes."""

    detail: Literal["born_on"] = "born_on"
    account: date | None = None
    proposed: date


class ImportPhoneDetail(BaseModel):
    detail: Literal["phone"] = "phone"
    account: str | None = None
    proposed: str


class ImportEmergencyContactDetail(BaseModel):
    """The proposal is whole: the account's contact where every member the document's
    carries equals the account's, otherwise the document's contact alone."""

    detail: Literal["emergency_contact"] = "emergency_contact"
    account: ImportCheckInEmergencyContact | None = None
    proposed: ImportCheckInEmergencyContact


class ImportInsuranceDetail(BaseModel):
    """Proposed whole, on `ImportEmergencyContactDetail`'s terms."""

    detail: Literal["insurance"] = "insurance"
    account: ImportCheckInInsurance | None = None
    proposed: ImportCheckInInsurance


ImportCheckInDetail = Annotated[
    ImportBornOnDetail | ImportPhoneDetail | ImportEmergencyContactDetail | ImportInsuranceDetail,
    Field(discriminator="detail"),
]


class ImportCheckInSubmission(BaseModel):
    """The check-in details the diver confirmed in the preview, sent back beside the token.

    A detail left out is not written; one sent as `null` is cleared. An object replaces the
    account's whole, so a member it leaves out is cleared with it. A detail the document
    does not carry is not written whatever is sent for it.
    """

    model_config = ConfigDict(extra="forbid")

    born_on: BirthDate | None = None
    phone: CheckInPhone | None = None
    emergency_contact: ImportCheckInEmergencyContact | None = None
    insurance: ImportCheckInInsurance | None = None

    def columns(self) -> dict[str, dict[str, Any]]:
        """Each submitted detail as the account columns it writes, keyed by detail.

        A blank string is written as `null`, the columns' one spelling of unset.
        """
        contact = self.emergency_contact or ImportCheckInEmergencyContact()
        insurance = self.insurance or ImportCheckInInsurance()
        every: dict[str, dict[str, Any]] = {
            "born_on": {"date_of_birth": self.born_on},
            "phone": {"phone": self.phone},
            "emergency_contact": dict(
                zip(EMERGENCY_CONTACT_FIELDS, (contact.name, contact.phone, contact.relationship), strict=True)
            ),
            "insurance": dict(
                zip(INSURANCE_FIELDS, (insurance.provider, insurance.number, insurance.expires_on), strict=True)
            ),
        }
        return {
            detail: {column: None if is_blank(value) else value for column, value in columns.items()}
            for detail, columns in every.items()
            if detail in self.model_fields_set
        }

    def anchor_errors(self) -> list[dict[str, Any]]:
        """`PATCH /user`'s anchor rule, as validation errors located inside this body."""
        columns = self.columns()
        errors = []
        for detail, fields, member in (
            ("emergency_contact", EMERGENCY_CONTACT_FIELDS, "name"),
            ("insurance", INSURANCE_FIELDS, "provider"),
        ):
            if detail in columns and lacks_its_anchor(columns[detail], fields):
                errors.append(
                    {
                        "type": "missing",
                        "loc": (detail, member),
                        "msg": ANCHOR_REQUIRED_MESSAGES[fields[0]],
                        "input": None,
                    }
                )
        return errors


# ---------------------------------------------------------------- the portrait


class ImportPortraitOffer(BaseModel):
    """The archive's portrait beside the account's, for the diver to take or keep.

    Beside the check-in details rather than among them: a client that knows only those never
    sends a choice, and the account keeps its portrait.
    """

    account_sha256: Annotated[
        str | None,
        Field(
            description="The account's portrait as its digest - the `?v=` of `GET /user/portrait` - or `null` "
            "without one. Send it back as the choice's `account_sha256`."
        ),
    ]
    proposed: Annotated[
        str,
        Field(description="The archive's portrait as a `data:image/webp;base64,` URL, framed as it would be stored"),
    ]


class ImportPortraitChoice(BaseModel):
    """What the diver chose for the archive's portrait, sent back beside the token.

    `account_sha256` is the account's portrait as the preview showed it, so a portrait
    replaced or removed since is kept whatever the choice.
    """

    model_config = ConfigDict(extra="forbid")

    choice: Literal["take", "keep"]
    account_sha256: Annotated[
        str | None, Field(min_length=_SHA256_LENGTH, max_length=_SHA256_LENGTH, pattern=r"^[0-9a-f]+$")
    ]


class ImportReport(BaseModel):
    """The body of both responses: the same shape whether it is a plan or a result.

    Deliberately one model rather than two: a preview a diver approved and the result they
    got back are only worth comparing if they are the same shape, and the two are produced
    by the same planner run against the same document.

    `conversion` is on the report rather than on the preview alone for the same reason: the
    result panel is what stays on screen after an import, and a diver who was told at
    preview that their computer's gas mixes could not be carried should still be told it
    afterwards.
    """

    collections: Annotated[
        list[ImportCollectionReport],
        Field(default_factory=list, description="One entry per envelope collection, in the envelope's own order"),
    ]
    files: ImportFileReport
    notes: Annotated[
        list[ImportNote],
        Field(default_factory=list, description="Every decision worth telling the diver about, in document order"),
    ]
    notes_truncated: Annotated[
        int,
        Field(
            default=0,
            description="Notes beyond the cap that are not in `notes`. Non-zero means the list above is a prefix, "
            "not the whole story - the counts are still complete.",
        ),
    ]
    conversion: Annotated[
        ConversionReport | None,
        Field(
            default=None,
            description="Present when the upload was converted from another format; `null` when it was DiveJSON.",
        ),
    ]


class ImportPreview(ImportReport):
    """What `POST /import/logbook/preview` returns. Nothing has been written.

    `format`, `version` and `generator` are the *imported document's*, which for a converted
    upload is the converter's output rather than the file a diver picked - so they read
    `divejson`, `1.0` and `divejson convert` there. What the diver's file was is
    `conversion.format`.
    """

    format: Annotated[str, Field(description="The imported document's own `format` marker", examples=["divejson"])]
    version: Annotated[str, Field(description="The imported document's declared version", examples=["1.0"])]
    generator: Annotated[
        ImportGenerator | None, Field(default=None, description="What produced the imported document, if it said")
    ]
    archive: Annotated[
        bool,
        Field(
            description="Whether this upload was a container carrying the stored files. A zip of dive-computer "
            "files is not one: it converts to a logbook that references no stored files at all."
        ),
    ]
    token: Annotated[
        str,
        Field(
            description="Hand this back to `POST /import/logbook` with the same file. It attests which bytes this "
            "report describes and nothing else - the import re-reads, re-converts and re-plans from scratch."
        ),
    ]
    check_in_details: Annotated[
        list[ImportCheckInDetail],
        Field(
            default_factory=list,
            description="One entry per check-in detail the document carries, the account's value beside the "
            "proposal. Send back the ones the diver keeps or edits as `check_in_details`; nothing else is written.",
        ),
    ]
    portrait: Annotated[
        ImportPortraitOffer | None,
        Field(
            default=None,
            description="The archive's portrait, offered beside the account's. `null` when the upload carries none "
            "it can offer - `notes` say why - or carries the account's own, framed the same.",
        ),
    ]


class ImportResult(ImportReport):
    """What `POST /import/logbook` returns. Everything in it has been committed."""
