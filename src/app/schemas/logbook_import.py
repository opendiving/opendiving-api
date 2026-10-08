"""The shapes logbook import reads and reports - a DiveJSON document seen from the
*reader* side, and the report that says what importing one would do.

A converted file - a UDDF file, a `.ssrf`, a FIT, a Suunto app or DM5 XML export, each file
of a zip of them - arrives here as a DiveJSON document like any other, because the converter's
output is one. What the report says of the conversion is `ImportReport.conversion`, what the
conversion could not carry, and each file's row in `ImportReport.members`.

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
from .checkin_details import CheckinDetailsUpdate, EmergencyContact, InsurancePolicy
from .contact import ADDRESS_POSTCODE_MAX, CONTACT_EMAIL_MAX, CONTACT_WEBSITE_MAX, ContactRole
from .course import CourseStatus
from .dive import (
    Current,
    DecoAlgorithm,
    DiveMode,
    DiveType,
    EntryType,
    RecordingDevice,
    Salinity,
    WaterType,
    Waves,
    Weather,
)
from .dive_mixture import GasRole, TankUsage
from .dive_profile import ProfileEventType
from .gear_service import ServiceKind
from .person import PERSON_EMAIL_MAX, PersonRole

# The spec's own string bounds (§6). Named rather than repeated inline because each one
# governs several members, and because each is also the width of the column behind it -
# so a mismatch between the two is a bug worth being able to find by name.
_NAME_MAX = 255
_LABEL_MAX = 120
_SHORT_MAX = 64
_QID_MAX = 32
_SHA256_LENGTH = 64
# §6.4b's own bounds on a device's members, and the widths of `dive_recording`'s columns.
_DEVICE_MAX = 64
_FIRMWARE_MAX = 32
# §6.1's bound on a phone number, the diver's and an emergency contact's alike.
_PHONE_MAX = 32
# §6.10a's bound on a registry's identifier for a site.
_IDENTIFIER_MAX = 255


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


def _unknown_items_dropped(enum: type[StrEnum]) -> BeforeValidator:
    """`_unknown_is_absent` for an array of closed values: an item outside the vocabulary is
    dropped and the rest kept, and a list with none left reads as absent (spec §5.6).

    Dropping the whole member instead would punish every reader on the first value a 1.x
    minor adds - one new role would erase every role beside it.
    """
    values = {member.value for member in enum}

    def coerce(value: Any) -> Any:
        if not isinstance(value, list):
            return None
        known = [item for item in value if item in values]
        return known or None

    return BeforeValidator(coerce)


def _null_is_empty(value: Any) -> Any:
    """A collection written as `null` reads as absent, i.e. as the empty one (spec §5.4).

    An absent collection already means empty (§4). A writer must not emit the null, but a
    reader that meets one has an unambiguous reading available and refusing it would fail
    a document over a member it did not need.
    """
    return [] if value is None else value


_Collection = BeforeValidator(_null_is_empty)

# A course's or certification's `training_center`, which the format had before contacts
# were records and every export made before then carries. §9 counts training centers among
# the members that work as identity documents, so the planner turns each distinct one into
# a contact rather than letting `extra="ignore"` lose it. Read here and nowhere else - the
# writer never emits it.
_LegacyTrainingCenter = Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]

# A course's or certification's `instructor_name`, on the same terms: the format had it before
# people were records, and the planner makes each distinct one a person the host names as its
# instructor. Read here and nowhere else - the writer never emits it.
_LegacyInstructorName = Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]

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
    written as the diver confirms them - see `DECISIONS.md`. The tag list this app's writer
    puts under its key is read, so a tag on no dive or site comes back. Every member is optional
    because §6.1 makes them so: a converter whose source records nothing about an owner
    omits the whole object rather than minting identity for a person."""

    uuid: uuid_pkg.UUID | None = None
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
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


class ImportPersonReference(_ReadModel):
    """One person on one dive, trip or course (spec §6.20). A role outside the vocabulary
    reads as absent and keeps the reference (§5.6)."""

    person_uuid: uuid_pkg.UUID
    role: Annotated[PersonRole | None, _unknown_is_absent(PersonRole), Field(default=None)]


_People = Annotated[list[ImportPersonReference], Field(default_factory=list), _Collection]


class ImportSighting(_ReadModel):
    """One species seen on one dive (spec §6.3a). The count is read as written and bounded by
    the planner, and the note is cut at this app's cap there."""

    species_uuid: uuid_pkg.UUID
    count: int | None = None
    notes: str | None = None


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
    type: Annotated[DiveType | None, _unknown_is_absent(DiveType), Field(default=None)]
    rating: int | None = None
    # Read as written: the planner trims each, and drops what the app cannot hold.
    tags: Annotated[list[str], Field(default_factory=list), _Collection]
    air_temperature: float | None = None
    current: Annotated[Current | None, _unknown_is_absent(Current), Field(default=None)]
    waves: Annotated[Waves | None, _unknown_is_absent(Waves), Field(default=None)]
    weather: Annotated[Weather | None, _unknown_is_absent(Weather), Field(default=None)]
    entry_type: Annotated[EntryType | None, _unknown_is_absent(EntryType), Field(default=None)]
    boat_name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    entry_position: ImportPosition | None = None
    exit_position: ImportPosition | None = None
    trip_uuid: uuid_pkg.UUID | None = None
    course_uuid: uuid_pkg.UUID | None = None
    contact_uuid: uuid_pkg.UUID | None = None
    site_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list), _Collection]
    gear_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list), _Collection]
    sightings: Annotated[list[ImportSighting], Field(default_factory=list), _Collection]
    people: _People
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
    position: ImportPosition | None = None
    bbox: ImportBoundingBox | None = None


class ImportTripPart(_ReadModel):
    starts_on: date | None = None
    ends_on: date | None = None
    location: ImportLocation | None = None
    accommodation_uuid: uuid_pkg.UUID | None = None


class ImportTrip(_ReadModel):
    uuid: uuid_pkg.UUID
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    # **A trip's `starts_on`, `ends_on` and `locations` are not members any more**, and
    # this reader does not accept them under those names: `extra="ignore"` means a 1.0
    # document written before the change is read as a trip with no parts rather than
    # failing, the same tolerance §5.6 asks for that a dive's `recordings` gets above.
    parts: Annotated[list[ImportTripPart], Field(default_factory=list), _Collection]
    people: _People
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
    instructor_name: _LegacyInstructorName = None
    instructor_number: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    contact_uuid: uuid_pkg.UUID | None = None
    people: _People
    training_center: _LegacyTrainingCenter = None
    notes: str | None = None
    created_at: datetime | None = None


class ImportExternalId(_ReadModel):
    """A site's registry entry (spec §6.10a), as written - its `extensions` ignored, this app
    storing them nowhere."""

    registry: str
    identifier: Annotated[str, Field(max_length=_IDENTIFIER_MAX)]


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
    # Read as written: the planner trims each and drops the ones the name already says.
    other_names: Annotated[list[Annotated[str, Field(max_length=_NAME_MAX)]], Field(default_factory=list), _Collection]
    location: ImportLocation | None = None
    position: ImportPosition | None = None
    # Shape only: the planner holds each to the registry's form and drops what is not.
    external_ids: Annotated[list[ImportExternalId], Field(default_factory=list), _Collection]
    depth_from: float | None = None
    depth_to: float | None = None
    water_type: Annotated[WaterType | None, _unknown_is_absent(WaterType), Field(default=None)]
    altitude: int | None = None
    entry_types: Annotated[list[EntryType] | None, _unknown_items_dropped(EntryType), Field(default=None)]
    tags: Annotated[list[str], Field(default_factory=list), _Collection]
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
    contact_uuid: uuid_pkg.UUID | None = None
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
    instructor_uuid: uuid_pkg.UUID | None = None
    instructor_name: _LegacyInstructorName = None
    instructor_number: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    contact_uuid: uuid_pkg.UUID | None = None
    training_center: _LegacyTrainingCenter = None
    course_uuid: uuid_pkg.UUID | None = None
    notes: str | None = None
    front_file: ImportStoredFile | None = None
    back_file: ImportStoredFile | None = None
    created_at: datetime | None = None


class ImportAddress(_ReadModel):
    """A contact's postal address (spec §6.19). `country` is REQUIRED in the format and
    optional here, as a location's `name` is: the planner drops an address without one and
    keeps the contact."""

    street: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    city: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    postcode: Annotated[str | None, Field(default=None, max_length=ADDRESS_POSTCODE_MAX)]
    region: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    country: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]


class ImportContact(_ReadModel):
    """A party the diver dealt with (spec §6.18).

    `email` and `website` are read as the strings they are. The format's own check on an
    email is an `@` and it checks a website not at all, while this app's write schema refuses
    what `EmailStr` or an absolute `http(s)` URL would not accept - so the planner drops such
    a value with a note rather than one bad address failing a logbook.
    """

    uuid: uuid_pkg.UUID
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    roles: Annotated[list[ContactRole] | None, _unknown_items_dropped(ContactRole), Field(default=None)]
    phone: Annotated[str | None, Field(default=None, max_length=_PHONE_MAX)]
    email: Annotated[str | None, Field(default=None, max_length=CONTACT_EMAIL_MAX)]
    website: Annotated[str | None, Field(default=None, max_length=CONTACT_WEBSITE_MAX)]
    address: ImportAddress | None = None
    notes: str | None = None
    created_at: datetime | None = None


class ImportPerson(_ReadModel):
    """An individual the diver was with (spec §6.20).

    `email` is read as the string it is, as a contact's is, and the planner drops one this
    app could not have written. `extensions` is read because this app's own writer puts a
    linked person's account there, which the planner may link again.
    """

    uuid: uuid_pkg.UUID
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    email: Annotated[str | None, Field(default=None, max_length=PERSON_EMAIL_MAX)]
    phone: Annotated[str | None, Field(default=None, max_length=_PHONE_MAX)]
    notes: str | None = None
    created_at: datetime | None = None
    extensions: dict[str, Any] | None = None


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
    contacts: Annotated[list[ImportContact], Field(default_factory=list), _Collection]
    people: Annotated[list[ImportPerson], Field(default_factory=list), _Collection]
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
    # The document was written before a change to the format, and a value was read as its
    # writer meant it - a profile axis in seconds, a readout on the dive, `en13319` as a
    # dive's water, `po2_limit`, a dive's `species_uuids`. Nothing was lost.
    READ_AS_WRITTEN = "read_as_written"
    # The record imported, but one of its values could not be stored as written.
    VALUE_DROPPED = "value_dropped"
    # A value the document does not state was derived from what it does - a dive's duration
    # and average depth from its profile's time in the water, its bottom temperature from its
    # coldest sample - as spec §5.4 lets a reader that says so. Nothing was lost.
    VALUE_DERIVED = "value_derived"
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
    # A check-in detail in the document is not offered: an emergency contact that names
    # nobody, a policy that names no insurer, a row past the list's cap, or an email that is
    # not an address or is this account's sign-in address.
    CHECK_IN_DETAIL_DROPPED = "check_in_detail_dropped"
    # A check-in detail the diver confirmed in the preview was written to the account; the
    # portrait they took from the archive among them.
    CHECK_IN_DETAIL_WRITTEN = "check_in_detail_written"
    # The diver took the archive's portrait, and the account's changed after the preview they
    # chose from, so the account's was kept.
    PORTRAIT_KEPT = "portrait_kept"
    # A person was linked (apply), or would be (preview), to the account on this instance its
    # entry names - the sentence names that account's current username.
    ACCOUNT_LINKED = "account_linked"
    # The tags the import adds to the caller's list, named in one note. Tags are members of a
    # dive rather than a collection, so no collection report counts them.
    TAGS_CREATED = "tags_created"


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

    The four counts are disjoint and sum to the number of records the import's documents
    carry in this collection - and for `contacts` and `people`, the records made of the
    training centers and the instructors an export written before either existed names on its
    courses and certifications, which it carries as strings rather than records. Each file's
    records are counted as an import of that file alone would count them, after the files
    before it. `restored` is its
    own figure and never hides inside `created` or `skipped`: un-deleting is the one thing
    this feature does that no other surface in the app can, and a diver restoring a backup
    is entitled to see it counted.
    """

    collection: Annotated[str, Field(examples=["dives"])]
    created: int
    linked: Annotated[int, Field(description="Matched an existing row of the caller's; nothing was written")]
    restored: Annotated[int, Field(description="A soft-deleted row of the caller's, brought back under its own uuid")]
    skipped: int


class ImportFileReport(BaseModel):
    """The files a logbook's documents name - dive-computer files and card images - which
    follow different rules from the records that reference them.

    A bare document carries file *metadata* and no bytes, so `restored` is zero and
    `not_contained` is every referenced file - which is not an error, just a smaller
    restore. Of the files a document names, only an archive puts bytes back. A dive-computer
    file imported as itself is not counted here: its row in `ImportReport.members` says
    whether it was kept. The diver's portrait is not among them either: it is offered apart,
    beside the check-in details.
    """

    referenced: Annotated[int, Field(description="Dive-computer files and card images the document names")]
    restored: Annotated[int, Field(description="Files whose bytes were written to this instance")]
    not_contained: Annotated[int, Field(description="Files with no bytes in this document - import the archive")]
    skipped: Annotated[int, Field(description="Files whose bytes are here but could not be stored")]


class ConversionConverter(BaseModel):
    """What converted the files, so a report can be attributed to a version of it."""

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
            description="Up to three paths into the source files, each under the name of the file it is in, e.g. "
            "`dives.uddf/dive/0/tankdata/1`",
            examples=[["dives.uddf/dive/0", "dives.uddf/dive/3"]],
        ),
    ]


class ConversionReport(BaseModel):
    """What converting the import's non-DiveJSON files could not carry. `null` when every
    file was DiveJSON already.

    One report for the whole import, whatever number of files was converted: the groups are
    the union over them, and a path names the file it is in. Grouped here rather than in the
    browser, and rather than folded into `notes`: one source habit makes one finding per
    record - eight dives with no UTC offset are eight findings - and the converter's list is
    unbounded, where `notes` has the planner's 500-note cap. The grouping is where a cap can
    live at all, which is also why these are not another `ImportNoteCode`: a conversion
    finding has a source path rather than a uuid and a collection, and none of those codes
    describes it.
    """

    format: Annotated[
        str,
        Field(
            description="The format the converted files were read as, as the converter names it, or `mixed` when "
            "they were not all one format",
            examples=["uddf"],
        ),
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


class ImportMemberNotKept(StrEnum):
    """Why a file that was read is not kept as a file of the dive it becomes.

    A file is kept when it is one recording's: one dive, carrying at most one computer's
    record of it. These are the ways a file read into dives is not.
    """

    # The file holds more than one dive, so it belongs to no one recording.
    SEVERAL_DIVES = "several_dives"
    # Its one dive carries more than one computer's record, and a file belongs to one of them.
    SEVERAL_RECORDINGS = "several_recordings"
    # Larger than a dive-computer file this app stores; its dive imports all the same.
    TOO_LARGE = "too_large"
    # Your account already stores these bytes - on the recording the file reaches, or another.
    ALREADY_STORED = "already_stored"
    # Nothing was written for its dive: it is already in your logbook, or it was skipped.
    NOT_WRITTEN = "not_written"


class ImportMemberReport(BaseModel):
    """One file of the import: a part of the request, or a file a zip among them held.

    `kept` and `not_kept` are about files read into dives. A DiveJSON document, a full-export
    archive and a zip are never kept as files of their own - an archive restores the files
    its document names, and a zip's are rows of their own - so they read `false` and `null`.
    """

    part: Annotated[int, Field(description="The index of the request's `file` part this came from, from 0")]
    container: Annotated[
        int | None,
        Field(
            default=None,
            description="For a file a zip held, the index in `members` of that zip's own row; `null` otherwise",
        ),
    ]
    name: Annotated[
        str, Field(description="The file's name, or its path inside the zip that held it", examples=["dive.fit"])
    ]
    byte_size: int
    sha256: str
    format: Annotated[
        str | None,
        Field(
            default=None,
            description="What the file was read as: a converter format id, `divejson`, `archive` or `zip`; `null` "
            "for a file nothing here reads",
            examples=["fit"],
        ),
    ]
    opened: Annotated[
        int | None,
        Field(default=None, description="On a zip's own row, how many files it opened into; `null` on any other"),
    ]
    kept: Annotated[bool, Field(description="Whether the file is kept on the dive it becomes, as that dive's file")]
    not_kept: Annotated[
        ImportMemberNotKept | None,
        Field(default=None, description="Why a file read into dives is not kept; `null` when it is, or has no dive"),
    ]
    refusal: Annotated[
        str | None,
        Field(default=None, description="Why the file was refused, in one sentence; `null` for a file that was read"),
    ]


class ImportDiveOutcome(StrEnum):
    """What the import does to one dive, as the most that happens to it."""

    CREATED = "created"
    # A dive of yours that was deleted, brought back under its own identifier.
    RESTORED = "restored"
    # Already in your logbook; nothing is written for it.
    LINKED = "linked"
    # A dive you have, gaining a file or another computer's recording.
    UPDATED = "updated"
    SKIPPED = "skipped"


class ImportDiveReport(BaseModel):
    """One dive the import creates or touches, and the files it came from.

    The result's rows are the dives an import wrote, by the identifiers they carry, so a
    later action over "the dives this import brought in" has its selection here.
    """

    uuid: Annotated[
        uuid_pkg.UUID | None,
        Field(
            default=None,
            description="The dive's identifier once written - yours for a dive you already have - or `null` for a "
            "dive the import skips",
        ),
    ]
    outcome: ImportDiveOutcome
    files_added: Annotated[
        int, Field(default=0, description="On an `updated` dive, how many files it gains; 0 on any other")
    ]
    recordings_added: Annotated[
        int,
        Field(default=0, description="On an `updated` dive, how many computers' recordings it gains; 0 on any other"),
    ]
    reason: Annotated[
        str | None, Field(default=None, description="On a `skipped` dive, why, in one sentence; `null` on any other")
    ]
    start_time: Annotated[
        datetime | date | None,
        Field(
            default=None,
            description="When the dive started, as the dive read gives it: with the dive's own UTC offset, naive "
            "where it records none, a bare date where it states no time",
        ),
    ]
    duration: Annotated[int | None, Field(default=None, description="Seconds")]
    max_depth: Annotated[float | None, Field(default=None, description="Metres")]
    device: Annotated[
        RecordingDevice | None,
        Field(
            default=None, description="What recorded the dive: its first recording's device, as the dive read has it"
        ),
    ]
    members: Annotated[
        list[int], Field(default_factory=list, description="The indexes in `members` of the files the dive came from")
    ]


# ---------------------------------------------------------------- the check-in details


class ImportEmailDetail(BaseModel):
    """The check-in email: what the account holds, and what the document proposes - an address
    the route takes back, normalized as it normalizes one. Never the sign-in address, unless
    that is already the account's check-in email."""

    detail: Literal["email"] = "email"
    account: str | None = None
    proposed: str


class ImportPhoneDetail(BaseModel):
    detail: Literal["phone"] = "phone"
    account: str | None = None
    proposed: str


class ImportDateOfBirthDetail(BaseModel):
    detail: Literal["date_of_birth"] = "date_of_birth"
    account: date | None = None
    proposed: date


class ImportEmergencyContactsDetail(BaseModel):
    """The account's list beside the document's, proposed as a list. Each document row that
    matches an account row - every member it carries equal to that row's - is that account
    row; otherwise the document's row stands as it is."""

    detail: Literal["emergency_contacts"] = "emergency_contacts"
    account: list[EmergencyContact]
    proposed: list[EmergencyContact]


class ImportInsurancePoliciesDetail(BaseModel):
    """Proposed as a list, on `ImportEmergencyContactsDetail`'s terms - so this app's own UDDF,
    which has no slot for a policy number, proposes the account's policies with theirs."""

    detail: Literal["insurance_policies"] = "insurance_policies"
    account: list[InsurancePolicy]
    proposed: list[InsurancePolicy]


ImportCheckInDetail = Annotated[
    ImportEmailDetail
    | ImportPhoneDetail
    | ImportDateOfBirthDetail
    | ImportEmergencyContactsDetail
    | ImportInsurancePoliciesDetail,
    Field(discriminator="detail"),
]


class ImportCheckInSubmission(CheckinDetailsUpdate):
    """The check-in details the diver confirmed in the preview, sent back beside the token,
    keyed and bounded as `PATCH /user/checkin-details` keys and bounds them.

    A member left out is not written; a scalar sent as `null` is cleared, and a list sent
    replaces the account's whole. A member the document does not carry is not written
    whatever is sent for it.
    """


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
    by the same planner run against the same files.

    An import of several files reports what importing each would, one after another in the
    import's order: the counts in `collections` are their sums, and `notes` their notes in
    that order. `members` and `dives` are the report's own rows over the batch, on the
    preview and on the result alike.

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
        Field(
            default_factory=list,
            description="Every decision worth telling the diver about, in document order and the import's",
        ),
    ]
    notes_truncated: Annotated[
        int,
        Field(
            default=0,
            description="Notes beyond the cap that are not in `notes`. Non-zero means the list above is not the "
            "whole story - the counts are still complete. At the cap a `value_derived` note gives way to any "
            "other, so the list keeps those first.",
        ),
    ]
    conversion: Annotated[
        ConversionReport | None,
        Field(
            default=None,
            description="Present when any file was converted from another format; `null` when every one was DiveJSON.",
        ),
    ]
    members: Annotated[
        list[ImportMemberReport],
        Field(
            default_factory=list,
            description="One row per file: each part of the request and each file a zip among them held, in the "
            "order the import reads them",
        ),
    ]
    dives: Annotated[
        list[ImportDiveReport],
        Field(
            default_factory=list,
            description="One row per dive the import creates or touches, in the order the import reaches them",
        ),
    ]


class ImportPreview(ImportReport):
    """What `POST /import/logbook/preview` returns. Nothing has been written.

    `format`, `version` and `generator` are the *imported document's* - the first the import
    reads - which for a converted file is the converter's output rather than the file a diver
    picked, so they read `divejson`, `1.0` and `divejson convert` there. What the diver's
    files were is `conversion.format` and each row of `members`.
    """

    format: Annotated[str, Field(description="The imported document's own `format` marker", examples=["divejson"])]
    version: Annotated[str, Field(description="The imported document's declared version", examples=["1.0"])]
    generator: Annotated[
        ImportGenerator | None, Field(default=None, description="What produced the imported document, if it said")
    ]
    archive: Annotated[
        bool,
        Field(
            description="Whether the import carries a full-export archive, the container that restores the stored "
            "files its logbook names. A zip of dive-computer files is not one: its files are read one by one."
        ),
    ]
    token: Annotated[
        str,
        Field(
            description="Hand this back to `POST /import/logbook` with the same files under the same names. It "
            "attests which bytes this report describes and nothing else - the import re-reads, re-converts and "
            "re-plans from scratch."
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
            description="The archive's portrait, offered beside the account's. `null` when the import carries none "
            "it can offer - `notes` say why - or carries the account's own, framed the same.",
        ),
    ]


class ImportResult(ImportReport):
    """What `POST /import/logbook` returns. Everything in it has been committed."""
