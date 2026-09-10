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
and every one of these limits is also the width of the column it lands in.
"""

import uuid as uuid_pkg
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from ..core.schemas import NOTES_MAX_LENGTH
from .certification import CertificationAgency
from .course import CourseStatus
from .dive import WaterType
from .dive_mixture import GasRole, TankUsage
from .dive_profile import ProfileEventType
from .gear_service import ServiceKind

# The spec's own string bounds (§6). Named rather than repeated inline because each one
# governs several members, and because each is also the width of the column behind it -
# so a mismatch between the two is a bug worth being able to find by name.
_NAME_MAX = 255
_LABEL_MAX = 120
_SHORT_MAX = 64
_DISPLAY_NAME_MAX = 512
_QID_MAX = 32
_SHA256_LENGTH = 64


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


class ImportDiver(_ReadModel):
    """Read, reported, and never applied - see `DECISIONS.md`. Every member is optional
    because §6.1 makes them so: a converter whose source records nothing about an owner
    omits the whole object rather than minting identity for a person."""

    uuid: uuid_pkg.UUID | None = None
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    username: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    email: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    created_at: datetime | None = None
    extensions: dict[str, Any] | None = None


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
    events: Annotated[list[ImportEvent], Field(default_factory=list), _Collection]


class ImportCylinder(_ReadModel):
    volume: float | None = None
    start_pressure: float | None = None
    end_pressure: float | None = None
    oxygen: float | None = None
    helium: float | None = None
    po2_limit: float | None = None
    gas_number: int | None = None
    role: Annotated[GasRole | None, _unknown_is_absent(GasRole), Field(default=None)]
    usage: Annotated[TankUsage | None, _unknown_is_absent(TankUsage), Field(default=None)]


class ImportDive(_ReadModel):
    uuid: uuid_pkg.UUID
    dive_number: int | None = None
    # No offset validator, unlike `DiveCreate` in `schemas/dive.py`: an offset-less
    # `started_at` is spec §5.2's local date-time, and admitting it is the reason
    # `dive.utc_offset_minutes` became nullable. `DiveUpdate` has since dropped its
    # validator too, but for the narrower reason that it may only *preserve* what this
    # endpoint created - see `core/utils/datetime_offset.py`.
    started_at: datetime | None = None
    duration: int | None = None
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]
    max_depth: float | None = None
    avg_depth: float | None = None
    bottom_temperature: float | None = None
    # A number on the wire (spec §6.2 - half-metre visibility is a real low-vis fact) and
    # an `Integer` column here, which is the one place the format is finer than the app.
    visibility: float | None = None
    weight: float | None = None
    water_type: Annotated[WaterType | None, _unknown_is_absent(WaterType), Field(default=None)]
    altitude: int | None = None
    cns_start: float | None = None
    cns_end: float | None = None
    otu_start: float | None = None
    otu_end: float | None = None
    surface_pressure: float | None = None
    entry_position: ImportPosition | None = None
    exit_position: ImportPosition | None = None
    trip_uuid: uuid_pkg.UUID | None = None
    course_uuid: uuid_pkg.UUID | None = None
    site_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list), _Collection]
    gear_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list), _Collection]
    species_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list), _Collection]
    cylinders: Annotated[list[ImportCylinder], Field(default_factory=list), _Collection]
    source_file: ImportStoredFile | None = None
    profile: ImportProfile | None = None
    created_at: datetime | None = None


class ImportTripLocation(_ReadModel):
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    display_name: Annotated[str | None, Field(default=None, max_length=_DISPLAY_NAME_MAX)]
    position: ImportPosition | None = None
    bbox: ImportBoundingBox | None = None


class ImportTrip(_ReadModel):
    uuid: uuid_pkg.UUID
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    locations: Annotated[list[ImportTripLocation], Field(default_factory=list), _Collection]
    starts_on: date | None = None
    ends_on: date | None = None
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]
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
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]
    created_at: datetime | None = None


class ImportDiveSite(_ReadModel):
    uuid: uuid_pkg.UUID
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    location: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    position: ImportPosition | None = None
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]
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
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]
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
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]
    created_at: datetime | None = None


class ImportCertification(_ReadModel):
    uuid: uuid_pkg.UUID
    agency: Annotated[CertificationAgency | None, _unknown_is_absent(CertificationAgency), Field(default=None)]
    agency_other: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    certification_number: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    certified_on: date | None = None
    expires_on: date | None = None
    instructor_name: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    instructor_number: Annotated[str | None, Field(default=None, max_length=_SHORT_MAX)]
    training_center: Annotated[str | None, Field(default=None, max_length=_NAME_MAX)]
    course_uuid: uuid_pkg.UUID | None = None
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]
    front_file: ImportStoredFile | None = None
    back_file: ImportStoredFile | None = None
    created_at: datetime | None = None


class ImportDocument(_ReadModel):
    """A whole DiveJSON document as this reader sees it.

    `format` and `version` are the only required members: they are what a reader dispatches
    on before parsing further (spec §4), and a payload without them is not a DiveJSON
    document at all - which is a 415 rather than a 422, exactly as an unrecognized
    dive-computer export is at `POST /dive/parse`.

    `extensions` is here and absent from `ExportEnvelope`, and the asymmetry is correct in
    both directions: this app's writer emits none, and a reader that refused a foreign
    producer's would be violating §5.6.
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
    # The `diver` member was read and deliberately not applied.
    DIVER_NOT_APPLIED = "diver_not_applied"


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
    """The binaries, which follow different rules from the records that reference them.

    A bare document carries file *metadata* and no bytes, so `restored` is zero and
    `not_contained` is every referenced file - which is not an error, just a smaller
    restore. Only an archive can put bytes back.
    """

    referenced: Annotated[int, Field(description="Stored files the document names")]
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
    grouping is where a cap can live at all, which is also why these are not a twelfth
    `ImportNoteCode`: a conversion finding has a source path rather than a uuid and a
    collection, and none of the eleven codes describes it.
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


class ImportResult(ImportReport):
    """What `POST /import/logbook` returns. Everything in it has been committed."""
