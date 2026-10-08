"""Deciding what importing a document would do, without doing any of it.

The planner is the whole of the import's judgement and none of its writing. It resolves
every uuid against this instance, picks create / link / restore / skip for each record,
converts the document's members into column values, drops what the database would refuse,
and builds the report a diver reads. `writer.py` then walks the result and issues
statements; it makes no decisions of its own.

**Preview and apply run the same planner, and apply runs it again rather than replaying a
plan.** Preview stores nothing, so its answer is a prediction; between the two calls a row
can be deleted, restored by hand, or created under a name the document also uses.
Replanning inside the apply transaction is what makes the restore branch's re-evaluation
automatic rather than a rule somebody has to remember, and it costs a handful of indexed
lookups.

Four rules run through everything below, and each is the format's rather than this app's:

- **A uuid is preserved when nothing on this instance claims it**, identifies the same
  object when the *caller* already owns it, and is remapped to a fresh one when it belongs
  to somebody else - so a cross-instance migration keeps identity and a cross-account copy
  gets its own.
- **A soft-deleted row of the caller's is restored**, wholesale and under its original
  uuid, and counted separately from everything else. This is the application's only
  un-delete path.
- **Nothing is invented** (spec §5.4). A member the document omits imports as absent; where
  the column cannot hold absent and no default honestly means "not recorded", the *record*
  is skipped and reported rather than filled in.
- **Nothing is fatal.** A value the database would refuse is dropped and reported, a record
  that cannot be built is skipped and reported, and the import carries on. What refuses a
  file lives in `reader.py`, and what refuses a whole import there, in `parts.py` and in the
  batch's storage check (`batch.py`).

A plan is one file's. An import of several plans each in turn against the logbook as the
files before it left it, which is what `batch.py` owns.
"""

import hashlib
import uuid as uuid_pkg
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from divejson import IN_WATER_DEPTH, InWater
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ...core.schemas import NOTES_MAX_LENGTH
from ...core.utils.datetime_offset import split_dive_start_time
from ...core.utils.uploads import safe_filename
from ...crud.crud_checkin_details import read_checkin_details
from ...crud.crud_dive_sites import sites_by_external_id
from ...crud.crud_dive_species import StoredSighting
from ...models.certification import Certification
from ...models.contact import Contact
from ...models.course import Course
from ...models.dive import Dive
from ...models.dive_file import DiveFile
from ...models.dive_recording import DiveRecording
from ...models.dive_site import DiveSite
from ...models.gear_item import GearItem
from ...models.gear_service_record import GearServiceRecord
from ...models.gear_service_schedule import GearServiceSchedule
from ...models.gear_set import GearSet
from ...models.person import Person
from ...models.species import Species
from ...models.tag import Tag
from ...models.trip import Trip
from ...models.user import User
from ...schemas.certification import AGENCY_OTHER_NOT_ALLOWED_MESSAGE, CertificationAgency, CertificationSide
from ...schemas.contact import ADDRESS_FIELDS, CONTACT_ADDRESS_PREFIX, canonical_roles, check_website
from ...schemas.dive_profile import MILLISECONDS_PER_SECOND
from ...schemas.dive_site import ExternalId, canonical_entry_types, canonical_external_ids, canonical_other_names
from ...schemas.export import DIVEJSON_PRODUCER_KEY
from ...schemas.gear_item import GearType
from ...schemas.location import DIVE_SITE_LOCATION_PREFIX, LOCATION_FIELDS
from ...schemas.logbook_import import (
    ImportCertification,
    ImportCheckInDetail,
    ImportCheckInSubmission,
    ImportCollectionReport,
    ImportContact,
    ImportCourse,
    ImportCylinder,
    ImportDive,
    ImportDiver,
    ImportDiveSite,
    ImportFileReport,
    ImportGearItem,
    ImportGearServiceRecord,
    ImportGearServiceSchedule,
    ImportGearSet,
    ImportLocation,
    ImportMemberNotKept,
    ImportNote,
    ImportNoteCode,
    ImportPerson,
    ImportPersonReference,
    ImportPortraitChoice,
    ImportPortraitOffer,
    ImportProfile,
    ImportSpecies,
    ImportStoredFile,
    ImportTrip,
)
from ...schemas.person import PersonRole
from ...schemas.tag import TAG_NAME_MAX, tag_key, trim_tag
from ...schemas.user_picture import PictureCrop
from ..certification_files import MAX_CARD_FILE_SIZE
from ..dive_files import MAX_DIVE_FILE_SIZE
from ..dive_profiles import (
    IMPORT_PARSER_KEY,
    NormalizedProfile,
    derive_gas_attribution,
    downsample,
)
from ..dive_reader import FALLBACK_CONTENT_TYPE, bottom_temperature, content_type_of, reads, two_places
from ..dive_recordings import (
    DeviceIdentity,
    RecordingCandidate,
    RecordingFacts,
    is_same_dive_strict,
    is_same_recording,
    load_candidates,
)
from ..person_links import Account, accounts_by_uuid, claim_link_slot, link_budget_remaining
from ..recording_shape import INT32_MAX, Bound, Drop, bounded, finite, gate_figures, in_water_of, shape_recording
from ..user_pictures import (
    MAX_PICTURE_UPLOAD_SIZE,
    PORTRAIT_FRAME,
    HeldPicture,
    ImportedPicture,
    InvalidCropError,
    UnsupportedPictureError,
    get_held_picture,
    preview_data_url,
    process_imported_original,
)
from .check_in import propose, to_write
from .reader import LoadedImport

# The collections the envelope declares, in the order it declares them (spec §4). The
# report follows this order; resolution does not - see `_RESOLUTION_ORDER`.
COLLECTIONS: tuple[str, ...] = (
    "dives",
    "trips",
    "courses",
    "sites",
    "species",
    "gear",
    "gear_sets",
    "gear_service_schedules",
    "gear_service_records",
    "certifications",
    "contacts",
    "people",
)

# References only ever point *backwards* along this order, so one pass resolves everything:
# a trip's parts, a course, a service record, a certification and a dive name a contact; a
# trip, a course, a certification and a dive name people; a dive names a trip, a course,
# sites, gear and species; a gear set, a schedule and a service record name gear; a
# certification names a course. Nothing here is recursive, which is what
# makes a fixed order enough rather than a graph walk - and it is the order `writer.py`
# writes in, so a reference always resolves to a row that already exists.
_RESOLUTION_ORDER: tuple[str, ...] = (
    "contacts",
    "people",
    "trips",
    "courses",
    "sites",
    "species",
    "gear",
    "gear_sets",
    "gear_service_schedules",
    "gear_service_records",
    "certifications",
    "dives",
)

_EMAIL = TypeAdapter(EmailStr)

# How many notes a report carries. A logbook whose every record has something to say about
# it would otherwise produce a response larger than the document it describes; the counts
# stay complete either way, and `notes_truncated` says how many are missing. Well past any
# real import - a clean round trip of the demo logbook produces a handful - but for the
# derived values it reports, one per dive of a dive-computer file, which yield (`add_note`).
MAX_NOTES = 500


def add_note(notes: list[ImportNote], note: ImportNote) -> int:
    """Append `note` under the cap, and say how many notes that cost: 0, or the 1 dropped.

    At the cap a derived value's note gives way to any other, the latest first. It is the one
    kind written once per dive of a dive-computer file, so a batch of them would otherwise
    crowd every other note out of the list, a skipped record's among them.
    """
    if len(notes) < MAX_NOTES:
        notes.append(note)
        return 0
    if note.code != ImportNoteCode.VALUE_DERIVED:
        derived = [index for index, kept in enumerate(notes) if kept.code == ImportNoteCode.VALUE_DERIVED]
        if derived:
            del notes[derived[-1]]
            notes.append(note)
    return 1


# `IMPORT_PARSER_KEY` is **defined in `services/dive_profiles.py`** and imported above rather
# than declared here, because that module is where the value means something: it is one of
# the two `UNREPRODUCIBLE_PROVENANCES` both backfills refuse to overwrite. It lived here
# while this module was the only writer of it and the justification was "a bare-imported dive
# is never a backfill candidate, because `backfill_profiles` selects from `dive_file`" - which
# stopped being how that query works when recordings arrived. The guard is on the *profile's*
# provenance now, so the constant and the rule that reads it belong together.

# How many digests one `IN` asks after: an archive can name thousands of files, and a
# statement's bind parameters are bounded.
_DIGESTS_PER_QUERY = 1000

_LATITUDE_LIMIT = 90.0
_LONGITUDE_LIMIT = 180.0

# **A column's width is not the bound where a derived column adds the values up.** Two
# imported numbers each inside `Integer` can sum past it, and the write that fails is then
# the *derived* one, in the middle of the apply transaction - a whole logbook refused over
# an arithmetic overflow in a tile. Two derivations do that here, and each gets a ceiling
# on its inputs rather than a check on its output, because the output is computed by code
# that knows nothing about import.
#
# A dive count: `next_due_at_dive_count` is `dive_count_at_start + interval_dives` (see
# `services/gear_service.py`). A million is past any logbook that has ever existed - the
# most prolific working divers log a few tens of thousands in a career - so a number
# beyond it is a unit error rather than a diver, and two of them still sum comfortably
# inside the column.
_MAX_DIVE_COUNT = 1_000_000

# A single dive's length: `user_dive_stats.total_time` is `SUM(dive.duration)` over every
# dive the account holds, including ones this import never touched. A year is far past
# saturation diving, which is where the longest logged excursions come from and which runs
# in weeks. The sum is `BigInteger` as well now - the ceiling here is what makes the
# overflow implausible, the width is what makes it impossible.
_MAX_DIVE_DURATION_SECONDS = 366 * 24 * 60 * 60


class Action(StrEnum):
    """What the import will do with one record of the document."""

    CREATE = "create"
    LINK = "link"
    RESTORE = "restore"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """A stored binary the archive path will write, planned but not yet read.

    `archive_path` is a member name inside the container and nothing else. `sha256` is the
    *document's* claim about the bytes, and the writer checks the member against it - which
    is what makes a restored file verified end to end rather than merely present.
    """

    archive_path: str
    sha256: str
    original_filename: str
    content_type: str
    parser_key: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedProfile:
    """A dive's samples, already normalized, capped and attributed.

    `duration` is carried beside the profile rather than taken from
    `NormalizedProfile.duration`, which is the largest sample time. The document's own
    `duration` may legitimately be larger - a computer that stops sampling at the surface
    can keep timing the dive (spec §6.4) - and that number is the denominator of the app's
    gas-coverage fraction, so losing it would make a restored dive read as less covered
    than the one it came from.
    """

    profile: NormalizedProfile
    duration: int


@dataclass(frozen=True, slots=True)
class PlannedRecording:
    """One device's record of a dive, planned but not yet written.

    `device` is keyed by **column** name, ready to spread into the insert - the members are
    already bounded to the widths `dive_recording` declares, and keeping the translation here
    means the writer never has to know that the format says `brand` and the column says
    `device_brand`.

    **`duration` and `max_depth` are derived from the profile's own samples**, which is what
    an imported recording has to do: a DiveJSON Recording carries no such scalars of its own,
    so the document offers nowhere else to read the two figures the strict gate compares
    from. `duration` is whole seconds, the profile's millisecond span divided. Left `None`
    where the recording has no profile - and a recording with no figures simply cannot be
    strict-matched, which is honest rather than lossy.
    """

    ordinal: int
    device: dict[str, Any] = field(default_factory=dict)
    # The recording's own settings, keyed by **column** name like `device` above and for the
    # same reason: the writer spreads them into the row and never has to know that the format
    # says `gf_low` and the column says `deco_gf_low`. `mode` is its own column rather than a
    # key in the dict because it is not part of the model.
    mode: str | None = None
    deco_model: dict[str, Any] = field(default_factory=dict)
    salinity: str | None = None
    # The device's readouts, keyed by column name like `device`, the absent ones left out.
    readouts: dict[str, float] = field(default_factory=dict)
    start_time: datetime | None = None
    utc_offset_minutes: int | None = None
    duration: int | None = None
    max_depth: float | None = None
    profile: PlannedProfile | None = None
    # The time in the water over the depth channel as the document states it, before the
    # point cap: what the dive takes for a duration or an average depth its document omits.
    in_water: InWater | None = None
    files: list[PlannedFile] = field(default_factory=list)
    # The imported file itself, where it is this recording's file: stored, and the recording
    # derived from its files as the dive form derives one, where `files` - an archive's -
    # keep the document's profile.
    kept: PlannedFile | None = None


@dataclass(frozen=True, slots=True)
class PlannedRecordingMatch:
    """An incoming recording that turned out to belong to a dive the caller already has.

    Two kinds, and the difference is which gate admitted it. **`fill`** passed the
    same-recording test: this is a second reading of a record that dive already holds - the
    same computer exported twice - so it fills that recording's blanks, and the dive's, and
    writes no new row. **`attach`** passed the strict same-dive test: a *different* computer
    recorded the same dive, so a new recording is appended to that dive.

    Neither creates a dive. Which is why a document dive whose every recording matched is not
    created either: there is nothing left of it that is not already in the logbook, and a
    second dive row would be the duplicate the gates exist to prevent.
    """

    kind: Literal["fill", "attach"]
    dive_id: int
    dive_uuid: uuid_pkg.UUID
    source_uuid: uuid_pkg.UUID
    recording_id: int | None
    recording: PlannedRecording
    # **The matched recording's position on its dive, and it decides what the writer may
    # touch beyond the recording itself.** A fill that brings no file writes the dive's entry
    # and exit fixes and fills its cylinders only through the *primary* recording: the dive's
    # columns come from its primary, and nothing tells a second reading without a file from a
    # logbook re-imported over a value the diver cleared. `None` on an `attach`, where no
    # stored recording is named and the writer computes the slot.
    ordinal: int | None
    # The incoming *dive's* values, carried for a `fill` only: a match that writes no dive
    # row can still supply readings the existing dive has none of. Ignored on `attach`, where
    # the recording is a second computer's and the dive's figures are the primary's.
    dive_values: dict[str, Any] = field(default_factory=dict)
    # The incoming dive's cylinders, carried on **both** kinds. On a `fill` without the file
    # they are what `fill_dive_mixtures` writes into the primary's paired rows' blanks; on an
    # `attach` without it they are the list this second computer's `gas_number`s are mapped
    # *from* onto the dive's own, and the values its paired rows' blanks take. Defaulting to
    # empty rather than being required is what let the attach case ship without them once,
    # with the whole relabelling unreachable.
    mixtures: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PlannedPortrait:
    """The archive's portrait, read, verified and rendered, and the account's as it stood.

    Planned whenever there is one to offer, preview and apply alike, so the apply's notes
    agree with the preview's; `take` is whether the apply writes it.
    """

    picture: ImportedPicture
    filename: str
    held: HeldPicture | None
    take: bool = False


@dataclass(frozen=True, slots=True)
class PlannedPersonReference:
    """One person on a dive, trip or course, or a card's instructor, as planned.

    Either a document record's `source_uuid` - resolved to a row by the writer, which writes
    people first - or, for a legacy instructor name matching a person the caller already
    has, that row's id.
    """

    role: str | None = None
    source_uuid: uuid_pkg.UUID | None = None
    row_id: int | None = None


@dataclass(slots=True)
class PlannedRecord:
    """One record of the document, and what will become of it.

    **Two identifiers, and the difference is the remap branch.** `source_uuid` is what the
    document calls this record and is how everything here addresses it - the collection
    dicts are keyed on it, references resolve to it, and the writer's id map uses it.
    `uuid` is what the *row* will carry, which is the same thing unless the document's
    identifier already belonged to another account, in which case a fresh one is minted and
    only this field moves.

    `canonical_source_uuid` is set only on the within-document link branch: two records of
    one collection that collide on a user-scoped uniqueness key are one row, and this says
    which of them owns it. References follow it, so both spellings reach that row.
    """

    action: Action
    source_uuid: uuid_pkg.UUID
    uuid: uuid_pkg.UUID
    row_id: int | None = None
    canonical_source_uuid: uuid_pkg.UUID | None = None
    values: dict[str, Any] = field(default_factory=dict)
    children: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ImportPlan:
    """Everything the writer needs, plus everything the diver is told."""

    is_archive: bool
    records: dict[str, dict[uuid_pkg.UUID, PlannedRecord]]
    # Incoming recordings that belong to dives the caller already has. Held beside the
    # records rather than inside them because they are not records of any collection: no dive
    # is created for them. A match never names a dive of its own file, whose dives are planned
    # against the logbook as it stood; in an import of several files it may name one an
    # earlier file wrote, and the writer walks this list after the dives either way.
    recording_matches: list[PlannedRecordingMatch]
    notes: list[ImportNote]
    notes_dropped: int
    files_referenced: int
    files_restored: int
    files_not_contained: int
    files_skipped: int
    # The preview's section, one entry per check-in detail the document carries, and the
    # account columns the apply writes from what the diver submitted - see `check_in.py`.
    check_in_details: list[ImportCheckInDetail] = field(default_factory=list)
    check_in_values: dict[str, Any] = field(default_factory=dict)
    # The archive's portrait, beside the check-in details and never counted among the files:
    # those are the logbook's, and the report's file counts say so.
    portrait: PlannedPortrait | None = None
    # The diver's tag list from this app's extension, trimmed: the writer makes a row of each
    # name no dive carries, as it does of every dive's tags, so the vocabulary survives.
    tags: list[str] = field(default_factory=list)
    # Whether the imported file itself is stored on the dive it becomes, and why not where
    # it is one recording's file and is not. See `LoadedImport.kept`.
    kept: bool = False
    not_kept: ImportMemberNotKept | None = None
    # The dives this file writes - creates or restores - whose document states no number, by
    # source uuid. Written holding `0`, and numbered by `import_batch` after its last file.
    unnumbered_dives: list[uuid_pkg.UUID] = field(default_factory=list)

    async def portrait_offer(self) -> ImportPortraitOffer | None:
        """The preview's portrait fields, the archive's drawn small enough to travel inline."""
        if self.portrait is None:
            return None
        held = self.portrait.held
        return ImportPortraitOffer(
            account_sha256=None if held is None else held.rendition_sha256,
            proposed=await preview_data_url(self.portrait.picture.rendition),
        )

    def collection_reports(self) -> list[ImportCollectionReport]:
        reports = []
        for name in COLLECTIONS:
            records = list(self.records.get(name, {}).values())
            reports.append(
                ImportCollectionReport(
                    collection=name,
                    created=sum(1 for record in records if record.action is Action.CREATE),
                    linked=sum(1 for record in records if record.action is Action.LINK),
                    restored=sum(1 for record in records if record.action is Action.RESTORE),
                    skipped=sum(1 for record in records if record.action is Action.SKIP),
                )
            )
        return reports

    def file_report(self) -> ImportFileReport:
        return ImportFileReport(
            referenced=self.files_referenced,
            restored=self.files_restored,
            not_contained=self.files_not_contained,
            skipped=self.files_skipped,
        )

    def writable(self, collection: str) -> list[PlannedRecord]:
        """The records of one collection the writer has work for, in document order."""
        return [
            record
            for record in self.records.get(collection, {}).values()
            if record.action in (Action.CREATE, Action.RESTORE)
        ]


@dataclass(frozen=True, slots=True)
class _ExistingRow:
    id: int
    user_id: int
    is_deleted: bool


# Every single-column bound on `dive` a document can reach. The pair rules
# (`ck_dive_avg_depth_within_max` and the two position pairs) are deliberately absent:
# there is no "the bad value" to drop in a pair, which is the same reason `parsed_dive.py`
# has no guard for them either. `_plan_dive` handles the depth pair on its own terms, and a
# position is all-or-nothing by shape.
_DIVE_BOUNDS: tuple[Bound, ...] = (
    Bound(
        "number",
        lambda value: 0 <= value <= _MAX_DIVE_COUNT,
        f"a dive number must be between 0 and {_MAX_DIVE_COUNT}",
    ),
    Bound(
        "duration",
        lambda value: 0 < value <= _MAX_DIVE_DURATION_SECONDS,
        "a dive's duration must be greater than zero and shorter than a year",
    ),
    Bound("max_depth", lambda value: value > 0, "a maximum depth must be greater than zero"),
    Bound("avg_depth", lambda value: value > 0, "an average depth must be greater than zero"),
    Bound("visibility", lambda value: 0 <= value <= INT32_MAX, "visibility must be a non-negative number of metres"),
    Bound("weight", lambda value: value >= 0, "ballast cannot be negative"),
    Bound("altitude", lambda value: -450 <= value <= 6500, "altitude must be between -450 and 6500 metres"),
    Bound("rating", lambda value: 1 <= value <= 5, "a rating must be between 1 and 5"),
)

# A site's single-column bounds, from `models/dive_site.py`. The depth pair's order is a pair
# rule, which `_plan_site` handles as `_plan_dive` handles a dive's: both ends go.
_SITE_BOUNDS: tuple[Bound, ...] = (
    Bound("depth_from", lambda value: value >= 0, "a depth cannot be negative"),
    Bound("depth_to", lambda value: value >= 0, "a depth cannot be negative"),
    Bound("altitude", lambda value: -450 <= value <= 6500, "altitude must be between -450 and 6500 metres"),
)

# A sighting's count, from `models/dive_species.py`, with the column's width as its ceiling:
# the format floors it above zero and caps it nowhere.
_SIGHTING_BOUNDS: tuple[Bound, ...] = (
    Bound(
        "count",
        lambda value: 1 <= value <= INT32_MAX,
        f"a sighting's count must be between 1 and {INT32_MAX}",
    ),
)

# The mixture bounds, same rules, from `models/dive_mixture.py`. `volume`, `oxygen` and
# `helium` are here rather than handled apart because they became nullable columns: they
# are bounded-and-droppable like every other member on this list, and a document that
# recorded none of them now produces a cylinder rather than a note - see `_plan_cylinders`.
_MIXTURE_BOUNDS: tuple[Bound, ...] = (
    Bound("volume", lambda value: value > 0, "a cylinder volume must be greater than zero"),
    Bound("oxygen", lambda value: 0 <= value <= 100, "an oxygen fraction must be between 0 and 100 percent"),
    Bound("helium", lambda value: 0 <= value <= 100, "a helium fraction must be between 0 and 100 percent"),
    Bound("start_pressure", lambda value: 0 < value <= 350, "a start pressure must be between 0 and 350 bar"),
    Bound("end_pressure", lambda value: 0 <= value <= 350, "an end pressure must be between 0 and 350 bar"),
    Bound("ppo2_limit", lambda value: 0.4 <= value <= 2.0, "a ppO2 limit must be between 0.4 and 2.0 bar"),
    Bound(
        "gas_number",
        lambda value: 0 <= value <= INT32_MAX,
        f"a gas number must be between 0 and {INT32_MAX}",
    ),
)


# The members the dive form rounds to the two places it shows (`dive_reader._mixture`).
_ROUNDED_CYLINDER_MEMBERS = ("volume", "oxygen", "helium", "start_pressure", "end_pressure", "ppo2_limit")


def _unpressurized(cylinder: ImportCylinder) -> list[str]:
    """The pressures a converted file writes as an absent-marker: 0 bar or below."""
    return [
        name
        for name in ("start_pressure", "end_pressure")
        if (value := getattr(cylinder, name)) is not None and finite(value) and value <= 0
    ]


def _site_external_ids(site: ImportDiveSite) -> tuple[list[ExternalId], list[str]]:
    """A site's registry entries as a write stores them, and why each refused one was.

    Held to the write schema's own rules - the producer-key pattern, the identifier's form
    under a registry the format names - with a repeated pair kept once. A refused entry is
    dropped and named, never the site: it is one link out of the logbook, not the record.
    """
    kept: list[ExternalId] = []
    refused: list[str] = []
    for entry in site.external_ids:
        try:
            kept.append(ExternalId(registry=entry.registry, identifier=entry.identifier))
        except ValidationError:
            refused.append(
                f"A registry entry ({entry.registry!r}, {entry.identifier!r}) was not in that registry's form, "
                "and was dropped"
            )
    return canonical_external_ids(kept), refused


def _key(*parts: str | None) -> tuple[str, ...]:
    """A user-scoped uniqueness key, spelled the way the index spells it.

    `lower()` and `coalesce(..., '')`, matching `ux_dive_site_user_id_name_location_lower`
    and its three siblings. Python's `str.lower` and Postgres's `lower()` agree on
    everything either is likely to meet here; where they could differ the index is still
    the authority, and the import surfaces the disagreement as a refusal of the document
    rather than as a duplicate row.
    """
    return tuple((part or "").lower() for part in parts)


class _Planner:
    """One plan, built once.

    A class rather than a pile of functions because every step needs the same five things -
    the session, the caller, the document, the container, and somewhere to put a note - and
    threading those through twenty helpers reads worse than holding them.
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        loaded: LoadedImport,
        resolution_ran: bool = False,
        newly_resolved_aphia_ids: frozenset[int] = frozenset(),
        check_in: ImportCheckInSubmission | None = None,
        portrait: ImportPortraitChoice | None = None,
        claim_links: bool = False,
        batch_dive_ids: frozenset[int] = frozenset(),
    ) -> None:
        self._db = db
        self._user_id = user_id
        self._loaded = loaded
        self._document = loaded.document
        # The dives earlier files of the same import wrote. A recording matched to one of
        # them raises no note: the logbook does not "already have" a recording the diver
        # dropped a moment ago, and that dive's own row names every file it came from.
        self._batch_dive_ids = batch_dive_ids
        self._kept = False
        self._not_kept: ImportMemberNotKept | None = loaded.not_kept
        # `resolution_ran` is what makes preview and apply tell the truth about species
        # without telling two different stories: before the pre-pass an unknown AphiaID is
        # something this import *will* look up, after it an unknown one is something WoRMS
        # could not answer for.
        self._resolution_ran = resolution_ran
        self._newly_resolved = newly_resolved_aphia_ids
        self._check_in = check_in
        self._check_in_details: list[ImportCheckInDetail] = []
        self._check_in_values: dict[str, Any] = {}
        self._portrait_choice = portrait
        self._portrait: PlannedPortrait | None = None
        self._records: dict[str, dict[uuid_pkg.UUID, PlannedRecord]] = {name: {} for name in COLLECTIONS}
        self._notes: list[ImportNote] = []
        self._notes_dropped = 0
        self._files_referenced = 0
        self._files_restored = 0
        self._files_not_contained = 0
        self._files_skipped = 0
        self._species_row_by_uuid: dict[uuid_pkg.UUID, int] = {}
        # The document's species records by uuid, once `_plan_species` has made each unique.
        self._species_by_uuid: dict[uuid_pkg.UUID, ImportSpecies] = {}
        # Of the digests this file could store, those the account already holds, against the
        # recording that holds each, plus the ones this file is about to add, against none
        # yet. `ux_dive_file_user_id_sha256` is per user, so a second recording carrying
        # identical bytes cannot have a row of its own - and one row names one
        # `recording_id`, so it cannot serve two either. The file is skipped and reported; the
        # dive is not.
        self._claimed_digests: dict[str, int | None] = {}
        # Incoming recordings that belong to dives this account already has - see
        # `PlannedRecordingMatch`. Filled by `_plan_dives`, walked by the writer.
        self._recording_matches: list[PlannedRecordingMatch] = []
        # The dives this file writes with no number of their own - see `ImportPlan`.
        self._unnumbered_dives: list[uuid_pkg.UUID] = []
        # `None` until asked. See `_has_recordings`.
        self._account_has_recordings: bool | None = None
        # The caller's contacts by name, and the names this document's contacts - and the
        # training centers of an export made before contacts existed - have claimed so far.
        # Filled by `_plan_contacts`, which runs before anything that references a contact.
        self._contact_index: dict[tuple[str, ...], int] = {}
        self._contact_aliases: dict[tuple[str, ...], uuid_pkg.UUID] = {}
        # The same for people by name, plus the accounts the caller's people link and the
        # ones this document's have linked so far - a link claims a person before a name
        # does. Filled by `_plan_people`, which runs before anything that references one.
        self._person_index: dict[tuple[str, ...], int] = {}
        self._person_aliases: dict[tuple[str, ...], uuid_pkg.UUID] = {}
        self._linked_rows: dict[int, int] = {}
        self._link_aliases: dict[int, uuid_pkg.UUID] = {}
        self._accounts: dict[uuid_pkg.UUID, Account] = {}
        # **Where a link's count is spent.** The apply claims one slot per link it makes, an
        # exhausted window dropping that link rather than the import; the preview reads what
        # the window has left and spends nothing, so the plan is made twice and counted once.
        # Read on the first link the preview meets, so an import that links nobody never asks.
        self._claim_links = claim_links
        self._link_budget: int | None = None
        # The caller's tags by `tag_key`, the names of this document's diver's tag list, and
        # the tags this import makes - by key, each its first spelling. Filled by
        # `_plan_dives` and `_plan_tag_list`.
        self._tag_index: dict[str, int] = {}
        self._tag_list: list[str] = []
        self._new_tags: dict[str, str] = {}

    # ------------------------------------------------------------------ notes

    def _note(self, code: ImportNoteCode, message: str, *, collection: str | None = None, uuid: Any = None) -> None:
        self._notes_dropped += add_note(
            self._notes, ImportNote(code=code, collection=collection, uuid=uuid, message=message)
        )

    def _claim_document_uuid(self, collection: str, record: Any) -> None:
        """Make a record whose uuid another record of this collection already claimed its own.

        Every collection is accumulated into a dict keyed on the document's uuid, so without
        this the second of two records sharing one would **overwrite** the first: the first
        never written, never counted, never noted. That is the one shape of loss this module
        has no other route to - "nothing is fatal, everything is reported" is its invariant,
        and a silent drop is neither of those - and the counts would stop summing to what the
        document carries, which `ImportCollectionReport` promises they do.

        A document like that is not conforming (§5.3 makes every uuid in a document unique,
        and the reference corpus carries an invalid fixture for it), which is exactly why it
        gets a reader's answer rather than a refusal: **two records claiming one uuid are two
        records**, and the second is remapped. The first keeps the identity, so every
        reference to it resolves where the document's own order says it should.

        Mutating the parsed record is what keeps the planning functions from each having to
        know about this; it is the reader's copy of the document and nothing else reads
        it afterwards.
        """
        if record.uuid not in self._records[collection]:
            return
        self._note(
            ImportNoteCode.RECORD_REMAPPED_REFERENCES_STAY,
            "Two records in this document claim the same identifier, so this one was imported under a new one. A "
            "reference to that identifier reaches the first of them.",
            collection=collection,
            uuid=record.uuid,
        )
        record.uuid = uuid7()

    def _skip(self, collection: str, record_uuid: uuid_pkg.UUID, reason: str) -> PlannedRecord:
        self._note(ImportNoteCode.RECORD_SKIPPED, reason, collection=collection, uuid=record_uuid)
        return PlannedRecord(action=Action.SKIP, source_uuid=record_uuid, uuid=record_uuid)

    def _dropped(self, collection: str, record_uuid: uuid_pkg.UUID, reason: str) -> None:
        self._note(ImportNoteCode.VALUE_DROPPED, reason, collection=collection, uuid=record_uuid)

    # ------------------------------------------------------------------ lookups

    async def _rows_by_uuid(self, model: Any, uuids: Sequence[uuid_pkg.UUID]) -> dict[uuid_pkg.UUID, _ExistingRow]:
        """Every row on the instance carrying one of these uuids, deleted ones included.

        **Branching on `is_deleted` rather than filtering it is the whole point.** The
        repo's habitual `is_deleted=False` lookup would report a soft-deleted husk's uuid
        as unclaimed, the import would create against it, and the *full* unique index on
        `uuid` would refuse the insert - a failure on the one path the restore branch exists
        to serve. No `user_id` filter either: a uuid owned by another account is what the
        remap branch is for, and a query that could not see it would collide instead.
        """
        if not uuids:
            return {}
        deleted = getattr(model, "is_deleted", None)
        columns = [model.uuid, model.id, model.user_id]
        if deleted is not None:
            columns.append(deleted)
        rows = await self._db.execute(select(*columns).where(model.uuid.in_(set(uuids))))
        return {
            row[0]: _ExistingRow(id=row[1], user_id=row[2], is_deleted=bool(row[3]) if deleted is not None else False)
            for row in rows
        }

    async def _existing_by_key(
        self, model: Any, columns: Sequence[Any], key: Callable[[Any], tuple[str, ...]]
    ) -> dict[tuple[str, ...], int]:
        """The caller's rows for one table, indexed by its user-scoped uniqueness key.

        Whole-table for this user rather than one lookup per record: a diver has hundreds
        of sites at the outside, and the alternative is a query per record of the document.
        The first row wins a duplicate key, which cannot happen while the index holds.
        """
        rows = await self._db.execute(select(model.id, *columns).where(model.user_id == self._user_id))
        index: dict[tuple[str, ...], int] = {}
        for row in rows:
            index.setdefault(key(row[1:]), row[0])
        return index

    # ------------------------------------------------------------------ uuid rules

    def _resolve(
        self, collection: str, source_uuid: uuid_pkg.UUID, existing: dict[uuid_pkg.UUID, _ExistingRow]
    ) -> PlannedRecord:
        """The uuid rules, in one place, for every caller-owned collection."""
        row = existing.get(source_uuid)
        if row is None:
            return PlannedRecord(action=Action.CREATE, source_uuid=source_uuid, uuid=source_uuid)
        if row.user_id != self._user_id:
            # Somebody else's row. Nothing about it is readable from here and nothing about
            # it changes: the import mints a fresh identity, and every reference to the
            # document's uuid follows it, which is what keeps the copy internally
            # consistent. Idempotence is impossible on this branch by construction - see
            # `DECISIONS.md` - and the preview saying so is the honest guard.
            self._note(
                ImportNoteCode.RECORD_REMAPPED_REFERENCES_FOLLOW,
                "That identifier already belongs to another account here, so this record was imported under a new "
                "one. Every reference to it was updated to match.",
                collection=collection,
                uuid=source_uuid,
            )
            return PlannedRecord(action=Action.CREATE, source_uuid=source_uuid, uuid=uuid7())
        if row.is_deleted:
            self._note(
                ImportNoteCode.RECORD_RESTORED,
                "This record was deleted here and is being restored from the document, under its original identifier.",
                collection=collection,
                uuid=source_uuid,
            )
            return PlannedRecord(action=Action.RESTORE, source_uuid=source_uuid, uuid=source_uuid, row_id=row.id)
        self._note(
            ImportNoteCode.RECORD_LINKED,
            "This record is already in your logbook under the same identifier, so nothing was written for it.",
            collection=collection,
            uuid=source_uuid,
        )
        return PlannedRecord(action=Action.LINK, source_uuid=source_uuid, uuid=source_uuid, row_id=row.id)

    def _claim_unique(
        self,
        collection: str,
        record: PlannedRecord,
        index: dict[tuple[str, ...], int],
        aliases: dict[tuple[str, ...], uuid_pkg.UUID],
        key: tuple[str, ...],
        label: str,
        *,
        index_key: tuple[str, ...] | None,
    ) -> PlannedRecord:
        """Turn a would-be duplicate into a link rather than an `IntegrityError`.

        **No uniqueness rule anywhere makes an import fail.** Every user-scoped unique index
        in this app is a "you already have one of these" rule rather than a data-integrity
        one, so the answer is to point at the row that is already there and say so. Both
        halves matter: `index` is what the caller already had, `aliases` is what earlier
        records of this same document already claimed - two sites named "Blue Hole" under
        different uuids are one site, and the second reference has to reach the first's row.

        **The two halves take separate keys, and one collection needs them to differ.** A
        service schedule's index is keyed on `gear_item_id`, which does not exist yet for a
        gear item this import is creating - so `index_key` is `None` there and the
        existing-row half is skipped, while `key` stays the document's own identity and the
        alias half runs regardless. Skipping *both* together is what let two schedules of one
        document collide on `ux_gear_service_schedule_item_kind_label` and take the whole
        import down with an `IntegrityError`. It is keyword-only and has no default for that
        reason: every caller has to say which key its index is on.
        """
        row_id = None if index_key is None else index.get(index_key)
        if row_id is not None:
            self._note(
                ImportNoteCode.RECORD_LINKED,
                f"You already have a {label} with this name, so this record was linked to it rather than duplicated.",
                collection=collection,
                uuid=record.source_uuid,
            )
            return PlannedRecord(
                action=Action.LINK, source_uuid=record.source_uuid, uuid=record.source_uuid, row_id=row_id
            )
        owner = aliases.get(key)
        if owner is not None:
            self._note(
                ImportNoteCode.RECORD_LINKED,
                f"This document carries two {label} records with the same name; they were imported as one.",
                collection=collection,
                uuid=record.source_uuid,
            )
            return PlannedRecord(
                action=Action.LINK,
                source_uuid=record.source_uuid,
                uuid=record.source_uuid,
                canonical_source_uuid=owner,
            )
        aliases[key] = record.source_uuid
        return record

    def _reference(
        self, collection: str, record_uuid: uuid_pkg.UUID, target: str, target_uuid: uuid_pkg.UUID | None
    ) -> uuid_pkg.UUID | None:
        """Resolve one `*_uuid` member to the uuid whose row will hold it.

        A dangling reference is a note, never a refusal: `divejson validate` rejects such a
        document because a *writer* must not produce one, but a reader salvaging a logbook
        has a strictly better answer available - import the record without the link, and say
        so.
        """
        if target_uuid is None:
            return None
        record = self._records[target].get(target_uuid)
        if record is None or record.action is Action.SKIP:
            self._note(
                ImportNoteCode.REFERENCE_UNRESOLVED,
                f"A reference to a record in `{target}` could not be resolved, so this record was imported without it.",
                collection=collection,
                uuid=record_uuid,
            )
            return None
        return record.canonical_source_uuid or record.source_uuid

    def _reference_list(
        self, collection: str, record_uuid: uuid_pkg.UUID, target: str, target_uuids: Sequence[uuid_pkg.UUID]
    ) -> list[uuid_pkg.UUID]:
        """A reference-list member, order preserved and duplicates collapsed.

        Order is meaningful (spec §5.3 - a dive's first site is its primary one), and the
        join tables refuse a repeat, so this is a stable dedupe rather than a set. Two
        distinct document uuids that resolved to one row collapse here too, which is what
        the alias branch above makes possible.
        """
        resolved: list[uuid_pkg.UUID] = []
        for target_uuid in target_uuids:
            canonical = self._reference(collection, record_uuid, target, target_uuid)
            if canonical is not None and canonical not in resolved:
                resolved.append(canonical)
        return resolved

    # ------------------------------------------------------------------ values

    def _bounded(
        self, collection: str, record_uuid: uuid_pkg.UUID, source: Any, bounds: Sequence[Bound]
    ) -> dict[str, Any]:
        """Every bounded member of one record, with the unstorable ones dropped and noted."""
        return bounded(source, bounds, self._drop_on(collection, record_uuid))

    def _drop_on(self, collection: str, record_uuid: uuid_pkg.UUID) -> Drop:
        """Where the session-free shaping says what it dropped: a note on this record."""
        return lambda reason: self._dropped(collection, record_uuid, reason)

    def _position(
        self, collection: str, record_uuid: uuid_pkg.UUID, position: Any, label: str
    ) -> tuple[float | None, float | None]:
        """A Position object as its column pair, or nothing at all.

        All or nothing by shape, which is what `ck_dive_entry_position_pair` and its
        siblings enforce: half a coordinate is a pin on the equator rather than a partial
        answer.
        """
        if position is None:
            return None, None
        latitude, longitude = position.latitude, position.longitude
        if not (finite(latitude) and finite(longitude)):
            self._dropped(collection, record_uuid, f"The {label} position was not a pair of numbers, and was dropped")
            return None, None
        if abs(latitude) > _LATITUDE_LIMIT or abs(longitude) > _LONGITUDE_LIMIT:
            self._dropped(collection, record_uuid, f"The {label} position was outside the world, and was dropped")
            return None, None
        return latitude, longitude

    def _place(
        self,
        collection: str,
        record_uuid: uuid_pkg.UUID,
        location: ImportLocation | None,
        *,
        label: str,
        centre_label: str,
        prefix: str = "",
    ) -> dict[str, Any]:
        """A Location object as its columns, for either host (spec §6.9).

        One reader, because the format defines one object: a trip part and a dive site's
        locality land in differently named columns and mean exactly the same thing, so a
        second copy of these rules is how the two would come to drop different things.

        Every column is present, `None` where there is no place: the trip parts go to
        Postgres as one executemany, which takes its column list from the first row.

        `name` is REQUIRED (§6.9) and a place without one cannot be called anything, so the
        whole place goes and its host keeps everything else. A bounding box needs its
        point - a rectangle with no centre frames nothing a reader can place, and the
        writer's own rule is the same one.

        Two labels, because the two notes name different things and a dive site has a
        position of its own for the locality's to be confused with: `label` names the place
        that was dropped, `centre_label` the position inside it.
        """
        place: dict[str, Any] = {f"{prefix}{field}": None for field in LOCATION_FIELDS}
        if location is not None and not (location.name or "").strip():
            self._dropped(collection, record_uuid, f"A {label} had no name, and the place was dropped")
            location = None
        if location is None:
            return place

        latitude, longitude = self._position(collection, record_uuid, location.position, centre_label)
        place |= {
            f"{prefix}name": location.name,
            f"{prefix}latitude": latitude,
            f"{prefix}longitude": longitude,
        }
        bbox = location.bbox
        if bbox is not None and latitude is not None:
            if bbox.south <= bbox.north and all(
                finite(corner) for corner in (bbox.south, bbox.north, bbox.west, bbox.east)
            ):
                place |= {
                    f"{prefix}bbox_south": bbox.south,
                    f"{prefix}bbox_north": bbox.north,
                    f"{prefix}bbox_west": bbox.west,
                    f"{prefix}bbox_east": bbox.east,
                }
            else:
                self._dropped(
                    collection, record_uuid, "A bounding box the geocoder could not have produced was dropped"
                )
        return place

    def _agency(self, collection: str, record_uuid: uuid_pkg.UUID, source: Any) -> tuple[str, str | None] | None:
        """The `agency`/`agency_other` pair, or `None` when the document names no agency
        this app can read.

        `None` covers three states the callers are free to answer differently: the member
        was absent, it carried a value outside the vocabulary (§5.6 reads that as absent
        too), or it claimed `other` without naming one. What each caller does with it is
        the member's requiredness, which is per-record: a certification's `agency` is
        REQUIRED and `_plan_certification` skips (spec §§6.16, 7 freeze that vocabulary
        precisely so a conforming document never lands here), a course's is OPTIONAL and
        `_course_agency` keeps the record.

        A stray `agency_other` beside a *named* agency is dropped rather than taken as an
        answer: the app refuses the pair (`validate_agency_pairing`), and the agency is the
        load-bearing half.
        """
        if source.agency is None:
            return None
        agency_other = source.agency_other
        if source.agency is CertificationAgency.OTHER:
            if not (agency_other or "").strip():
                return None
            return source.agency.value, agency_other
        if agency_other is not None:
            self._dropped(collection, record_uuid, f"`agency_other` was dropped: {AGENCY_OTHER_NOT_ALLOWED_MESSAGE}")
        return source.agency.value, None

    def _course_agency(self, record_uuid: uuid_pkg.UUID, course: ImportCourse) -> tuple[str | None, str | None]:
        """A course's agency pair, where no readable agency costs the pair and not the
        course.

        The mirror of `_course_agency` in `services/export/envelope.py`, and the half of
        `_agency`'s `None` that differs from the certification's: a diver does not lose a
        course, or every dive's link to it, over the agency member. `agency` is OPTIONAL
        (spec §6.17), so absent is a reading rather than a failure - and a value outside the
        vocabulary is absent too, `_unknown_is_absent` having applied §5.6 before this sees
        the document.

        What is reported is the pair that still holds something after the agency is gone:
        `other` with nothing to name it, and an `agency_other` with no agency beside it.
        Both are states the schema refuses, so a conforming document has neither, and
        dropping one silently would leave the diver reading an import report that says
        their course arrived intact.
        """
        pair = self._agency("courses", record_uuid, course)
        if pair is not None:
            return pair
        if course.agency is not None or course.agency_other is not None:
            self._dropped("courses", record_uuid, "The agency this course claimed named nobody, and was dropped")
        return None, None

    def _count(self, collection: str, record_uuid: uuid_pkg.UUID, value: int | None) -> int:
        """A lifetime dive-count snapshot, as an `Integer` column can hold it.

        Absent reads as 0, which is the column's own default and means "count from the
        beginning". A negative or implausibly large one is not a count at all, so it is
        dropped to 0 and reported rather than taking the record with it.

        The ceiling is `_MAX_DIVE_COUNT` rather than the column's width, because
        `recalculate_service_schedule` adds this to a schedule's `interval_dives` and writes
        the sum into a column no wider than either of them.
        """
        if value is None:
            return 0
        if not 0 <= value <= _MAX_DIVE_COUNT:
            self._dropped(collection, record_uuid, "A dive count this app cannot store was dropped")
            return 0
        return value

    @staticmethod
    def _created_at(value: datetime | None) -> datetime:
        """`created_at` is logbook history and imports as recorded (spec §5.7).

        Restore means restore: a dive logged in 2019 that comes back out of a backup is
        still a 2019 record, and stamping it with today's date would have the log claim the
        diver entered it this morning. `updated_at` is left to the write, which is the
        honest reading of "when this instance last touched the row".
        """
        return value if value is not None else datetime.now(UTC)

    @staticmethod
    def _text(value: str | None) -> str:
        """A free-text column, whose `NOT NULL` default *is* the app's spelling of absent.

        The inverse of the writer's `_text`: it omits `""` because the app cannot tell a
        blank note from no note, so an absent `notes` reads back as `""` and the round trip
        is exact. This is the one shape of default the "nothing invented" rule admits -
        the column's own value for "the diver wrote nothing" - and the reason it is safe is
        that every row in this table already carries it.
        """
        return value or ""

    def _notes_text(self, collection: str, record_uuid: uuid_pkg.UUID, value: str | None) -> str:
        """A notes column: `_text`, cut at this app's own cap with a note.

        The format caps no note and this reader reads any length; the app's read shapes carry
        `NOTES_MAX_LENGTH`, so a longer one stored whole would make its record unreadable.
        """
        text = self._text(value)
        if len(text) <= NOTES_MAX_LENGTH:
            return text
        self._dropped(
            collection,
            record_uuid,
            f"The notes ran past {NOTES_MAX_LENGTH:,} characters, this app's limit, and the rest was dropped",
        )
        return text[:NOTES_MAX_LENGTH]

    # ------------------------------------------------------------------ collections

    async def plan(self) -> ImportPlan:
        for note in self._loaded.read_as_written:
            self._note(note.code, note.message, collection=note.collection, uuid=note.uuid)
        await self._plan_diver()
        await self._plan_portrait()
        await self._plan_contacts()
        await self._plan_people()
        await self._plan_trips()
        await self._plan_courses()
        await self._load_tag_index()
        await self._plan_sites()
        await self._plan_species()
        await self._plan_gear()
        await self._plan_gear_sets()
        await self._plan_schedules()
        await self._plan_service_records()
        await self._plan_certifications()
        await self._plan_dives()
        self._plan_tag_list()
        self._note_new_tags()
        return ImportPlan(
            is_archive=self._loaded.is_archive,
            records=self._records,
            recording_matches=self._recording_matches,
            unnumbered_dives=self._unnumbered_dives,
            notes=self._notes,
            notes_dropped=self._notes_dropped,
            files_referenced=self._files_referenced,
            files_restored=self._files_restored,
            files_not_contained=self._files_not_contained,
            files_skipped=self._files_skipped,
            check_in_details=self._check_in_details,
            check_in_values=self._check_in_values,
            portrait=self._portrait,
            tags=self._tag_list,
            kept=self._kept,
            not_kept=self._not_kept,
        )

    async def _plan_diver(self) -> None:
        """The document's own identity and settings are never applied; its check-in details,
        its email among them, are offered, and written as the diver submitted them
        (`check_in.py`)."""
        diver = self._document.diver
        if diver is None:
            return
        if diver.name or diver.username or _carries_settings(diver.extensions):
            self._note(
                ImportNoteCode.DIVER_NOT_APPLIED,
                "The document's own name and settings are not applied: this account keeps its own.",
            )
        sign_in = (await self._db.execute(select(User.email).where(User.id == self._user_id))).scalar_one()
        account = await read_checkin_details(self._db, user_id=self._user_id)
        self._check_in_details = propose(diver, account, sign_in, self._note)
        self._check_in_values = to_write(self._check_in_details, self._check_in, account, self._note)

    async def _plan_portrait(self) -> None:
        """The archive's portrait, offered beside the account's and taken only as chosen.

        Read, verified and rendered here, where the preview runs, so a file the pipeline
        refuses is noted before the diver chooses anything and the apply, re-running this,
        agrees. Never counted among the files. An archive portrait that is the account's own
        original is offered only where its crop differs, and then as a re-crop.
        """
        diver = self._document.diver
        stored = None if diver is None else diver.portrait_file
        if stored is None:
            return
        if not self._loaded.is_archive or stored.archive_path is None:
            self._note(
                ImportNoteCode.FILE_NOT_CONTAINED,
                "The portrait is named by the document but not contained in it. Import the archive to be offered it.",
            )
            return
        data = self._portrait_bytes(stored, stored.archive_path)
        if data is None:
            return
        try:
            picture = await process_imported_original(data, PORTRAIT_FRAME, _carried_crop(stored))
        except UnsupportedPictureError, InvalidCropError:
            self._note(
                ImportNoteCode.FILE_SKIPPED,
                "The portrait is not a JPEG or PNG this app can store, so it is not offered.",
            )
            return

        held = await get_held_picture(self._db, user_id=self._user_id, frame=PORTRAIT_FRAME)
        if held is not None and held.original_sha256 == picture.original_sha256 and held.crop == picture.crop:
            return
        planned = PlannedPortrait(
            picture=picture, filename=safe_filename(stored.original_filename, default="portrait"), held=held
        )
        choice = self._portrait_choice
        if choice is not None and choice.choice == "take":
            if choice.account_sha256 == (None if held is None else held.rendition_sha256):
                planned = replace(planned, take=True)
            else:
                self._note(
                    ImportNoteCode.PORTRAIT_KEPT,
                    "This account's portrait was kept: it changed after the preview the archive's was chosen in.",
                )
        self._portrait = planned

    def _portrait_bytes(self, stored: ImportStoredFile, archive_path: str) -> bytes | None:
        """The archive's portrait, checked against its digest and the upload ceiling, or `None`
        with a note saying why not - `_plan_card_file`'s checks, made here rather than by the
        writer because the preview renders it."""
        size = self._loaded.member_size(archive_path)
        if stored.sha256 is None:
            reason = "The portrait carries no digest to verify it against, so it is not offered."
        elif size is None:
            reason = "The portrait is named by the document but missing from the archive, so it is not offered."
        elif size > MAX_PICTURE_UPLOAD_SIZE:
            reason = (
                f"The portrait is larger than the {MAX_PICTURE_UPLOAD_SIZE // (1024 * 1024)} MB this app stores, so "
                "it is not offered."
            )
        elif (data := self._loaded.read_member(archive_path)) is None or (
            hashlib.sha256(data).hexdigest() != stored.sha256
        ):
            reason = (
                "The portrait could not be read out of the archive or does not match the digest the document "
                "recorded, so it is not offered."
            )
        else:
            return data
        self._note(ImportNoteCode.FILE_SKIPPED, reason)
        return None

    async def _plan_contacts(self) -> None:
        existing = await self._rows_by_uuid(Contact, [contact.uuid for contact in self._document.contacts])
        self._contact_index = await self._existing_by_key(Contact, (Contact.name,), lambda row: _key(row[0]))
        for contact in self._document.contacts:
            self._claim_document_uuid("contacts", contact)
            self._records["contacts"][contact.uuid] = self._plan_contact(contact, existing)

    def _plan_contact(self, contact: ImportContact, existing: dict[uuid_pkg.UUID, _ExistingRow]) -> PlannedRecord:
        collection = "contacts"
        if not (contact.name or "").strip():
            return self._skip(collection, contact.uuid, "A contact needs a name, and this one has none.")
        record = self._resolve(collection, contact.uuid, existing)
        if record.action is Action.CREATE:
            contact_key = _key(contact.name)
            record = self._claim_unique(
                collection,
                record,
                self._contact_index,
                self._contact_aliases,
                contact_key,
                "contact",
                index_key=contact_key,
            )
        if record.action not in (Action.CREATE, Action.RESTORE):
            return record

        record.values = {
            "user_id": self._user_id,
            "name": contact.name,
            # Already stripped of values outside the vocabulary by the reader (§5.6); a set,
            # so written in vocabulary order with repeats folded, as the write schema does.
            "roles": [role.value for role in canonical_roles(contact.roles or [])],
            "phone": contact.phone,
            "email": self._contact_email(contact),
            "website": self._contact_website(contact),
            **self._address(contact),
            "notes": self._notes_text(collection, contact.uuid, contact.notes),
            "created_at": self._created_at(contact.created_at),
        }
        return record

    def _contact_email(self, contact: ImportContact) -> str | None:
        return self._email("contacts", contact.uuid, contact.email)

    def _contact_website(self, contact: ImportContact) -> str | None:
        """The website as an absolute `http(s)` URL, or `None` and a note - no scheme is
        guessed, a bare host being as likely a typo as a site."""
        try:
            return check_website(contact.website)
        except ValueError:
            self._dropped(
                "contacts", contact.uuid, "The website was not an absolute http or https address, and was dropped"
            )
            return None

    def _address(self, contact: ImportContact) -> dict[str, Any]:
        """The address as its five columns, every one named, or none of it without a country
        - the anchor both formats require and `ck_contact_address_has_country` enforces."""
        columns: dict[str, Any] = {f"{CONTACT_ADDRESS_PREFIX}{field}": None for field in ADDRESS_FIELDS}
        address = contact.address
        if address is None:
            return columns
        if not (address.country or "").strip():
            if any(getattr(address, field) for field in ADDRESS_FIELDS):
                self._dropped("contacts", contact.uuid, "The address named no country, and was dropped")
            return columns
        return {f"{CONTACT_ADDRESS_PREFIX}{field}": getattr(address, field) for field in ADDRESS_FIELDS}

    def _contact_link(
        self,
        collection: str,
        record_uuid: uuid_pkg.UUID,
        contact_uuid: uuid_pkg.UUID | None,
        training_center: str | None,
    ) -> dict[str, Any]:
        """A course's or certification's contact, from its reference or - in an export made
        before contacts were records - from its training-center string.

        The reference wins where both are present. A legacy string names a contact by its
        trimmed name, claimed against the caller's contacts and this document's own as any
        contact is: one the caller already has is linked by row id (`contact_id`), and any
        other is planned as a new contact with the `school` role, which the next record naming
        the same string then shares. Planned here, never created: a preview predicts. So the
        contacts collection's counts include the contacts an old export's training centers
        make, beyond the records its `contacts[]` carries.
        """
        if contact_uuid is not None:
            return {"contact_uuid": self._reference(collection, record_uuid, "contacts", contact_uuid)}
        name = (training_center or "").strip()
        if not name:
            return {}
        legacy_key = _key(name)
        row_id = self._contact_index.get(legacy_key)
        if row_id is not None:
            return {"contact_id": row_id}
        owner = self._contact_aliases.get(legacy_key)
        if owner is not None:
            return {"contact_uuid": owner}
        source_uuid = uuid7()
        self._contact_aliases[legacy_key] = source_uuid
        self._records["contacts"][source_uuid] = PlannedRecord(
            action=Action.CREATE,
            source_uuid=source_uuid,
            uuid=source_uuid,
            values={
                "user_id": self._user_id,
                "name": name,
                "roles": ["school"],
                **{f"{CONTACT_ADDRESS_PREFIX}{field}": None for field in ADDRESS_FIELDS},
                "phone": None,
                "email": None,
                "website": None,
                "notes": "",
                "created_at": datetime.now(UTC),
            },
        )
        return {"contact_uuid": source_uuid}

    async def _plan_people(self) -> None:
        people = self._document.people
        existing = await self._rows_by_uuid(Person, [person.uuid for person in people])
        self._person_index = await self._existing_by_key(Person, (Person.name,), lambda row: _key(row[0]))
        linked = await self._db.execute(
            select(Person.linked_user_id, Person.id).where(
                Person.user_id == self._user_id, Person.linked_user_id.is_not(None)
            )
        )
        self._linked_rows = {row.linked_user_id: row.id for row in linked}
        self._accounts = await accounts_by_uuid(
            self._db, [account for person in people if (account := _person_account(person)) is not None]
        )
        for person in people:
            self._claim_document_uuid("people", person)
            self._records["people"][person.uuid] = await self._plan_person(person, existing)

    async def _plan_person(self, person: ImportPerson, existing: dict[uuid_pkg.UUID, _ExistingRow]) -> PlannedRecord:
        """One person: the uuid rules, then - for a record this import would create - a claim
        by link before a claim by name. A person whose entry names an account another of the
        caller's people already links is that person, whatever its name says."""
        collection = "people"
        name = (person.name or "").strip()
        if not name:
            return self._skip(collection, person.uuid, "A person needs a name, and this one has none.")
        record = self._resolve(collection, person.uuid, existing)
        account = self._accounts.get(account_uuid) if (account_uuid := _person_account(person)) else None
        if record.action is Action.CREATE and account is not None:
            row_id = self._linked_rows.get(account.id)
            if row_id is not None:
                self._note(
                    ImportNoteCode.RECORD_LINKED,
                    "You already have a person linked to the account this one names, so this record was linked to "
                    "them rather than duplicated.",
                    collection=collection,
                    uuid=person.uuid,
                )
                return PlannedRecord(action=Action.LINK, source_uuid=person.uuid, uuid=person.uuid, row_id=row_id)
            owner = self._link_aliases.get(account.id)
            if owner is not None:
                self._note(
                    ImportNoteCode.RECORD_LINKED,
                    "This document carries two people linked to one account; they were imported as one.",
                    collection=collection,
                    uuid=person.uuid,
                )
                return PlannedRecord(
                    action=Action.LINK, source_uuid=person.uuid, uuid=person.uuid, canonical_source_uuid=owner
                )
        if record.action is Action.CREATE:
            person_key = _key(name)
            record = self._claim_unique(
                collection,
                record,
                self._person_index,
                self._person_aliases,
                person_key,
                "person",
                index_key=person_key,
            )
        if record.action is not Action.CREATE:
            return record

        record.values = {
            "user_id": self._user_id,
            "name": name,
            "email": self._email(collection, person.uuid, person.email),
            "phone": person.phone,
            "notes": self._notes_text(collection, person.uuid, person.notes),
            "linked_user_id": await self._account_link(person, account_uuid, account),
            "created_at": self._created_at(person.created_at),
        }
        return record

    async def _account_link(
        self, person: ImportPerson, account_uuid: uuid_pkg.UUID | None, account: Account | None
    ) -> int | None:
        """The account a new person links to, from its entry under this producer's key - under
        the caller's link limit, as a typed username is, and named in the report.

        Only an account the username resolver would take: one on this instance (a deleted one
        in its grace period included), not the caller's, not linked by another of their people.
        Anything else is dropped with a note and nothing of it is kept.
        """
        if account_uuid is None:
            return None
        if account is None:
            self._dropped(
                "people",
                person.uuid,
                "The account this person was linked to is not on this instance, so the person arrives unlinked",
            )
            return None
        if account.id == self._user_id:
            self._dropped(
                "people", person.uuid, "This person was linked to your own account, so the person arrives unlinked"
            )
            return None
        if account.id in self._linked_rows or account.id in self._link_aliases:
            self._dropped(
                "people",
                person.uuid,
                "Another of your people is linked to the account this one names, so this one arrives unlinked",
            )
            return None
        if self._claim_links:
            if not await claim_link_slot(self._user_id):
                self._dropped(
                    "people",
                    person.uuid,
                    "This import is past the limit on linking people, so this one arrives unlinked",
                )
                return None
        else:
            if self._link_budget is None:
                self._link_budget = await link_budget_remaining(self._user_id)
            if self._link_budget <= 0:
                self._dropped(
                    "people",
                    person.uuid,
                    "This import is past the limit on linking people, so this one would arrive unlinked",
                )
                return None
            self._link_budget -= 1
        self._link_aliases[account.id] = person.uuid
        self._note(
            ImportNoteCode.ACCOUNT_LINKED,
            f"This person {'was' if self._claim_links else 'will be'} linked to @{account.username}, the account on "
            "this instance the document names.",
            collection="people",
            uuid=person.uuid,
        )
        return account.id

    def _email(self, collection: str, record_uuid: uuid_pkg.UUID, value: str | None) -> str | None:
        """The email as `EmailStr` accepts it, or `None` and a note. The format checks for an
        `@` and nothing more; the app's write schemas check the address, and a value this app
        could not have written itself is dropped rather than stored."""
        if value is None:
            return None
        try:
            return str(_EMAIL.validate_python(value))
        except ValidationError:
            self._dropped(collection, record_uuid, "The email address was not one this app can store, and was dropped")
            return None

    def _person_references(
        self, collection: str, record_uuid: uuid_pkg.UUID, references: Sequence[ImportPersonReference]
    ) -> list[PlannedPersonReference]:
        """A host's people, order kept and a person named twice kept once, at its first
        position - the join tables refuse a repeat, as `_reference_list` says."""
        planned: list[PlannedPersonReference] = []
        for reference in references:
            target = self._reference(collection, record_uuid, "people", reference.person_uuid)
            if target is None or any(existing.source_uuid == target for existing in planned):
                continue
            planned.append(
                PlannedPersonReference(
                    source_uuid=target, role=None if reference.role is None else reference.role.value
                )
            )
        return planned

    def _legacy_instructor(self, instructor_name: str | None) -> PlannedPersonReference | None:
        """The person an export made before people were records names as an instructor.

        Claimed by trimmed name against the caller's people and this document's own, as any
        person is: one the caller already has is referenced by row id, and any other is
        planned as a new person, which the next record naming the same string then shares.
        Planned here, never created - a preview predicts - so the people collection's counts
        include the people an old export's instructors make.
        """
        name = (instructor_name or "").strip()
        if not name:
            return None
        role = PersonRole.INSTRUCTOR.value
        legacy_key = _key(name)
        row_id = self._person_index.get(legacy_key)
        if row_id is not None:
            return PlannedPersonReference(role=role, row_id=row_id)
        owner = self._person_aliases.get(legacy_key)
        if owner is not None:
            return PlannedPersonReference(role=role, source_uuid=owner)
        source_uuid = uuid7()
        self._person_aliases[legacy_key] = source_uuid
        self._records["people"][source_uuid] = PlannedRecord(
            action=Action.CREATE,
            source_uuid=source_uuid,
            uuid=source_uuid,
            values={
                "user_id": self._user_id,
                "name": name,
                "email": None,
                "phone": None,
                "notes": "",
                "linked_user_id": None,
                "created_at": datetime.now(UTC),
            },
        )
        return PlannedPersonReference(role=role, source_uuid=source_uuid)

    def _certification_instructor(self, certification: ImportCertification) -> PlannedPersonReference | None:
        """A card's instructor: its reference where it has one, else a legacy name's person."""
        if certification.instructor_uuid is not None:
            target = self._reference("certifications", certification.uuid, "people", certification.instructor_uuid)
            return None if target is None else PlannedPersonReference(source_uuid=target)
        return self._legacy_instructor(certification.instructor_name)

    async def _plan_trips(self) -> None:
        existing = await self._rows_by_uuid(Trip, [trip.uuid for trip in self._document.trips])
        index = await self._existing_by_key(Trip, (Trip.name,), lambda row: _key(row[0]))
        aliases: dict[tuple[str, ...], uuid_pkg.UUID] = {}
        for trip in self._document.trips:
            self._claim_document_uuid("trips", trip)
            self._records["trips"][trip.uuid] = self._plan_trip(trip, existing, index, aliases)

    def _plan_trip(
        self,
        trip: ImportTrip,
        existing: dict[uuid_pkg.UUID, _ExistingRow],
        index: dict[tuple[str, ...], int],
        aliases: dict[tuple[str, ...], uuid_pkg.UUID],
    ) -> PlannedRecord:
        if not (trip.name or "").strip():
            return self._skip("trips", trip.uuid, "A trip needs a name, and this one has none.")
        record = self._resolve("trips", trip.uuid, existing)
        if record.action is Action.CREATE:
            trip_key = _key(trip.name)
            record = self._claim_unique("trips", record, index, aliases, trip_key, "trip", index_key=trip_key)
        if record.action not in (Action.CREATE, Action.RESTORE):
            return record

        record.values = {
            "user_id": self._user_id,
            "name": trip.name,
            "notes": self._notes_text("trips", trip.uuid, trip.notes),
            "created_at": self._created_at(trip.created_at),
        }
        record.children = {
            "parts": self._plan_trip_parts(trip),
            "people": self._person_references("trips", trip.uuid, trip.people),
        }
        return record

    def _plan_trip_parts(self, trip: ImportTrip) -> list[dict[str, Any]]:
        """A trip's parts, as `trip_part` rows - one per part the document carries.

        Value objects with no uuid of their own (spec §6.9a), replaced wholesale with the
        trip, so `position` is the list index rather than anything the document carries.
        Every part becomes a row, including one carrying neither a date nor a place: the
        diver recorded a stretch, and dropping it would renumber the ones after it.

        A part's own date range is checked here because nothing below catches it -
        `trip_part` has no check constraint, and a reversed range is what the format's
        §3 rule 2 forbids a writer rather than something a reader refuses. The place goes
        through `_place`, which is the dive site's reader too.
        """
        rows: list[dict[str, Any]] = []
        for part in trip.parts:
            ends_on = part.ends_on
            if ends_on is not None and part.starts_on is not None and ends_on < part.starts_on:
                self._dropped("trips", trip.uuid, "A part's end date preceded its start date, and was dropped")
                ends_on = None
            place = self._place(
                "trips", trip.uuid, part.location, label="trip part's location", centre_label="trip part's"
            )
            rows.append(
                {
                    "position": len(rows),
                    "start_date": part.starts_on,
                    "end_date": ends_on,
                    **place,
                    # Resolved to a row id by the writer, which writes contacts first.
                    "accommodation_uuid": self._reference("trips", trip.uuid, "contacts", part.accommodation_uuid),
                }
            )
        return rows

    async def _plan_courses(self) -> None:
        existing = await self._rows_by_uuid(Course, [course.uuid for course in self._document.courses])
        for course in self._document.courses:
            self._claim_document_uuid("courses", course)
            self._records["courses"][course.uuid] = self._plan_course(course, existing)

    def _plan_course(self, course: ImportCourse, existing: dict[uuid_pkg.UUID, _ExistingRow]) -> PlannedRecord:
        # No name dedupe, deliberately: a course failed once and retaken later is
        # legitimately the same name twice, and `course` carries no uniqueness beyond its
        # uuid to say otherwise.
        if not (course.name or "").strip():
            return self._skip("courses", course.uuid, "A course needs a name, and this one has none.")
        agency, agency_other = self._course_agency(course.uuid, course)
        if course.status is None:
            # `course.status` is `NOT NULL` here and OPTIONAL in the format, and §6.17 says
            # in as many words that a reader must not assume `completed`. There is no
            # honest default, so the record goes rather than the fact.
            return self._skip(
                "courses",
                course.uuid,
                "This course records no status, and this app cannot store a course without one. Nothing was "
                "invented for it.",
            )
        record = self._resolve("courses", course.uuid, existing)
        if record.action not in (Action.CREATE, Action.RESTORE):
            return record

        end_date = course.ends_on
        if end_date is not None and course.starts_on is not None and end_date < course.starts_on:
            self._dropped("courses", course.uuid, "The end date preceded the start date, and was dropped")
            end_date = None
        record.values = {
            "user_id": self._user_id,
            "name": course.name,
            "agency": agency,
            "agency_other": agency_other,
            "status": course.status.value,
            "start_date": course.starts_on,
            "end_date": end_date,
            "instructor_number": course.instructor_number,
            "notes": self._notes_text("courses", course.uuid, course.notes),
            "created_at": self._created_at(course.created_at),
        }
        people = self._person_references("courses", course.uuid, course.people)
        legacy = self._legacy_instructor(course.instructor_name)
        if legacy is not None and not any(reference.role == PersonRole.INSTRUCTOR for reference in people):
            people = [legacy, *(reference for reference in people if not _same_person(reference, legacy))]
        record.children = {
            **self._contact_link("courses", course.uuid, course.contact_uuid, course.training_center),
            "people": people,
        }
        return record

    async def _plan_sites(self) -> None:
        """Every site, matched by uuid, then by a registry entry, then by name and locality.

        The middle step reads the one key that crosses logbooks, and only where it singles a
        site out on both sides: an entry that exactly one of the caller's sites carries and
        exactly one of the document's does. Two sites may share an entry - a registry's
        object can be coarser than a diver's sites - so an entry two of either side's carry
        says nothing about which one is meant, and the import falls through to the name.
        """
        existing = await self._rows_by_uuid(DiveSite, [site.uuid for site in self._document.sites])
        index = await self._existing_by_key(
            DiveSite, (DiveSite.name, DiveSite.location_name), lambda row: _key(row[0], row[1])
        )
        held = await sites_by_external_id(self._db, user_id=self._user_id)
        entries = {site.uuid: _site_external_ids(site)[0] for site in self._document.sites}
        on_document_sites: dict[tuple[str, str], int] = {}
        for kept in entries.values():
            for entry in kept:
                on_document_sites[entry.pair] = on_document_sites.get(entry.pair, 0) + 1
        aliases: dict[tuple[str, ...], uuid_pkg.UUID] = {}
        for site in self._document.sites:
            self._claim_document_uuid("sites", site)
            single = {
                holders[0].id
                for entry in entries[site.uuid]
                if on_document_sites[entry.pair] == 1 and len(holders := held.get(entry.pair, [])) == 1
            }
            self._records["sites"][site.uuid] = self._plan_site(site, existing, index, aliases, single)

    def _plan_site(
        self,
        site: ImportDiveSite,
        existing: dict[uuid_pkg.UUID, _ExistingRow],
        index: dict[tuple[str, ...], int],
        aliases: dict[tuple[str, ...], uuid_pkg.UUID],
        held_by_entry: set[int],
    ) -> PlannedRecord:
        if not (site.name or "").strip():
            return self._skip("sites", site.uuid, "A dive site needs a name, and this one has none.")
        # The locality is read before the uniqueness claim, because the index keys on its
        # *name* and a place the reader drops takes the key with it.
        place = self._place(
            "sites",
            site.uuid,
            site.location,
            label="dive site's locality",
            centre_label="dive site locality's",
            prefix=DIVE_SITE_LOCATION_PREFIX,
        )
        record = self._resolve("sites", site.uuid, existing)
        if record.action is Action.CREATE and len(held_by_entry) == 1:
            # Every entry that singles a site out names the same one of the caller's. A match
            # writes nothing, as a match by uuid or by name writes nothing.
            self._note(
                ImportNoteCode.RECORD_LINKED,
                "You already have a dive site carrying the same registry entry, so this record was linked to it "
                "rather than duplicated.",
                collection="sites",
                uuid=site.uuid,
            )
            return PlannedRecord(
                action=Action.LINK, source_uuid=site.uuid, uuid=site.uuid, row_id=next(iter(held_by_entry))
            )
        if record.action is Action.CREATE:
            site_key = _key(site.name, place[f"{DIVE_SITE_LOCATION_PREFIX}name"])
            record = self._claim_unique("sites", record, index, aliases, site_key, "dive site", index_key=site_key)
        if record.action not in (Action.CREATE, Action.RESTORE):
            return record

        latitude, longitude = self._position("sites", site.uuid, site.position, "site's")
        bounded = self._bounded("sites", site.uuid, site, _SITE_BOUNDS)
        depth_from, depth_to = bounded.get("depth_from"), bounded.get("depth_to")
        if depth_from is not None and depth_to is not None and depth_from > depth_to:
            # No "the bad value" in a pair: both go, as half a position does.
            self._dropped("sites", site.uuid, "The depth range was shallower at its deep end, and was dropped")
            depth_from = depth_to = None
        external_ids, refused = _site_external_ids(site)
        for reason in refused:
            self._dropped("sites", site.uuid, reason)
        record.values = {
            "user_id": self._user_id,
            "name": site.name,
            # Trimmed as a tag is, a blank one dropped, and one the name or an earlier one
            # already says dropped too: it says nothing the record does not.
            "other_names": canonical_other_names(
                site.name, [trimmed for raw in site.other_names if (trimmed := trim_tag(raw))]
            ),
            "latitude": latitude,
            "longitude": longitude,
            "external_ids": [entry.model_dump() for entry in external_ids],
            "depth_from": depth_from,
            "depth_to": depth_to,
            "water_type": None if site.water_type is None else site.water_type.value,
            "altitude": bounded.get("altitude"),
            "entry_types": [entry.value for entry in canonical_entry_types(site.entry_types or [])],
            "notes": self._notes_text("sites", site.uuid, site.notes),
            "created_at": self._created_at(site.created_at),
            **place,
        }
        record.children = {"tags": self._tag_names(site.tags, collection="sites", record_uuid=site.uuid)}
        return record

    async def _plan_species(self) -> None:
        """The one collection that never becomes rows of the caller's.

        `Species` is a global, ownerless catalog filled one WoRMS pick at a time, so import
        matches it by AphiaID (spec §6.11) and creates nothing from the document's own
        snapshot - which it could not do honestly anyway: `status` is `NOT NULL` and the
        format has no member for it, so creation would have to invent one, in direct
        contradiction of §5.4. A species the catalog does not hold goes to the pre-pass,
        and one WoRMS cannot answer for is skipped with the sighting links that named it.
        """
        wanted = {species.aphia_id for species in self._document.species if species.aphia_id is not None}
        catalog: dict[int, int] = {}
        if wanted:
            rows = await self._db.execute(select(Species.aphia_id, Species.id).where(Species.aphia_id.in_(wanted)))
            catalog = {row[0]: row[1] for row in rows}

        for species in self._document.species:
            self._claim_document_uuid("species", species)
            self._records["species"][species.uuid] = self._plan_one_species(species, catalog)
            self._species_by_uuid[species.uuid] = species

    def _plan_one_species(self, species: ImportSpecies, catalog: dict[int, int]) -> PlannedRecord:
        if species.aphia_id is None:
            return self._skip(
                "species",
                species.uuid,
                "This sighting carries no AphiaID, which is the only identity this app can match a species on, so "
                "it could not be linked to the catalog.",
            )
        row_id = catalog.get(species.aphia_id)
        if row_id is not None:
            self._species_row_by_uuid[species.uuid] = row_id
            action = Action.CREATE if species.aphia_id in self._newly_resolved else Action.LINK
            return PlannedRecord(action=action, source_uuid=species.uuid, uuid=species.uuid, row_id=row_id)

        if self._resolution_ran:
            return self._skip(
                "species",
                species.uuid,
                "The World Register of Marine Species could not be reached for this species, so the sightings "
                "naming it were imported without it.",
            )
        # Preview. Nothing has been looked up yet, and saying "skipped" here would be a
        # worse prediction than saying "will be added": the catalog is filled from WoRMS on
        # demand and the ordinary answer is that it resolves.
        self._note(
            ImportNoteCode.SPECIES_UNRESOLVED,
            "This species is not in this instance's catalog yet and will be looked up in the World Register of "
            "Marine Species when you import. If it cannot be reached, the sightings naming it import without it.",
            collection="species",
            uuid=species.uuid,
        )
        return PlannedRecord(action=Action.CREATE, source_uuid=species.uuid, uuid=species.uuid)

    async def _plan_gear(self) -> None:
        existing = await self._rows_by_uuid(GearItem, [item.uuid for item in self._document.gear])
        index = await self._existing_by_key(GearItem, (GearItem.brand, GearItem.name), lambda row: _key(row[0], row[1]))
        aliases: dict[tuple[str, ...], uuid_pkg.UUID] = {}
        for item in self._document.gear:
            self._claim_document_uuid("gear", item)
            self._records["gear"][item.uuid] = self._plan_gear_item(item, existing, index, aliases)

    def _plan_gear_item(
        self,
        item: ImportGearItem,
        existing: dict[uuid_pkg.UUID, _ExistingRow],
        index: dict[tuple[str, ...], int],
        aliases: dict[tuple[str, ...], uuid_pkg.UUID],
    ) -> PlannedRecord:
        if not (item.name or "").strip():
            return self._skip("gear", item.uuid, "A gear item needs a name, and this one has none.")
        record = self._resolve("gear", item.uuid, existing)
        if record.action is Action.CREATE:
            item_key = _key(item.brand, item.name)
            record = self._claim_unique("gear", record, index, aliases, item_key, "gear item", index_key=item_key)
        if record.action not in (Action.CREATE, Action.RESTORE):
            return record

        gear_type = item.type
        if gear_type is not None and gear_type not in {member.value for member in GearType}:
            # §5.6's unknown-value rule for an OPTIONAL member: read it as not recorded.
            # The vocabulary is value-for-value the format's, so this is a document from a
            # later minor version rather than a mistake.
            self._dropped("gear", item.uuid, f"The gear type {gear_type!r} is not one this app knows, and was dropped")
            gear_type = None
        record.values = {
            "user_id": self._user_id,
            "name": item.name,
            "brand": item.brand,
            "type": gear_type,
            "notes": self._notes_text("gear", item.uuid, item.notes),
            "rented": bool(item.rented),
            "is_archived": bool(item.archived),
            "archived_at": item.archived_at if item.archived else None,
            # `dive_count` is derived (spec §5.7) and recomputed after the write, never
            # imported: the destination's view of which dives used this item is the only
            # thing that can make it true.
            "dive_count": 0,
            "created_at": self._created_at(item.created_at),
        }
        return record

    async def _plan_gear_sets(self) -> None:
        existing = await self._rows_by_uuid(GearSet, [gear_set.uuid for gear_set in self._document.gear_sets])
        index = await self._existing_by_key(GearSet, (GearSet.name,), lambda row: _key(row[0]))
        aliases: dict[tuple[str, ...], uuid_pkg.UUID] = {}
        for gear_set in self._document.gear_sets:
            self._claim_document_uuid("gear_sets", gear_set)
            self._records["gear_sets"][gear_set.uuid] = self._plan_gear_set(gear_set, existing, index, aliases)

    def _plan_gear_set(
        self,
        gear_set: ImportGearSet,
        existing: dict[uuid_pkg.UUID, _ExistingRow],
        index: dict[tuple[str, ...], int],
        aliases: dict[tuple[str, ...], uuid_pkg.UUID],
    ) -> PlannedRecord:
        if not (gear_set.name or "").strip():
            return self._skip("gear_sets", gear_set.uuid, "A gear set needs a name, and this one has none.")
        record = self._resolve("gear_sets", gear_set.uuid, existing)
        if record.action is Action.CREATE:
            set_key = _key(gear_set.name)
            record = self._claim_unique("gear_sets", record, index, aliases, set_key, "gear set", index_key=set_key)
        if record.action not in (Action.CREATE, Action.RESTORE):
            return record

        weight = gear_set.weight
        if weight is not None and not (finite(weight) and weight >= 0):
            self._dropped("gear_sets", gear_set.uuid, "A negative ballast weight was dropped")
            weight = None
        record.values = {
            "user_id": self._user_id,
            "name": gear_set.name,
            "weight": weight,
            "created_at": self._created_at(gear_set.created_at),
        }
        record.children = {"gear_uuids": self._reference_list("gear_sets", gear_set.uuid, "gear", gear_set.gear_uuids)}
        return record

    async def _plan_schedules(self) -> None:
        existing = await self._rows_by_uuid(
            GearServiceSchedule, [schedule.uuid for schedule in self._document.gear_service_schedules]
        )
        # The index is keyed on `(gear_item_id, kind, lower(label))`, so it is built after
        # gear has been planned and the ids are known - which is what `_RESOLUTION_ORDER`
        # buys. A schedule whose gear is only being created now cannot collide with an
        # existing row, because there is no existing row under a gear item that does not
        # exist yet.
        index = await self._existing_by_key(
            GearServiceSchedule,
            (GearServiceSchedule.gear_item_id, GearServiceSchedule.kind, GearServiceSchedule.label),
            lambda row: (str(row[0]), *_key(row[1], row[2])),
        )
        aliases: dict[tuple[str, ...], uuid_pkg.UUID] = {}
        for schedule in self._document.gear_service_schedules:
            self._claim_document_uuid("gear_service_schedules", schedule)
            self._records["gear_service_schedules"][schedule.uuid] = self._plan_schedule(
                schedule, existing, index, aliases
            )

    def _plan_schedule(
        self,
        schedule: ImportGearServiceSchedule,
        existing: dict[uuid_pkg.UUID, _ExistingRow],
        index: dict[tuple[str, ...], int],
        aliases: dict[tuple[str, ...], uuid_pkg.UUID],
    ) -> PlannedRecord:
        collection = "gear_service_schedules"
        gear_uuid = self._reference(collection, schedule.uuid, "gear", schedule.gear_uuid)
        if gear_uuid is None:
            return self._skip(collection, schedule.uuid, "A service schedule with no gear item to hang on was skipped.")
        if schedule.type is None:
            return self._skip(collection, schedule.uuid, "A service schedule needs a kind this app recognizes.")
        if schedule.starts_on is None:
            return self._skip(collection, schedule.uuid, "A service schedule needs the date its clock starts from.")
        months, dives = schedule.interval_months, schedule.interval_dives
        if months is not None and not 0 < months <= INT32_MAX:
            self._dropped(collection, schedule.uuid, "A month interval this app cannot store was dropped")
            months = None
        # Capped at `_MAX_DIVE_COUNT` rather than at the column's width, because
        # `recalculate_service_schedule` adds it to `dive_count_at_start` and writes the sum
        # into a column of the same width.
        if dives is not None and not 0 < dives <= _MAX_DIVE_COUNT:
            self._dropped(collection, schedule.uuid, "A dive interval this app cannot store was dropped")
            dives = None
        if months is None and dives is None:
            # `ck_gear_service_schedule_has_an_interval`: a rule with no interval can never
            # become due, so it would sit in the table generating nothing forever.
            return self._skip(collection, schedule.uuid, "A service schedule needs at least one interval to be due on.")

        record = self._resolve(collection, schedule.uuid, existing)
        gear_record = self._records["gear"][gear_uuid]
        if record.action is Action.CREATE:
            # **Both halves key on the gear *row* this schedule will hang on**, which is
            # what the unique index is on - and that row has two spellings depending on
            # where it came from. An existing row has an id, and two document gear records
            # can both link to it (each takes `_claim_unique`'s index branch, so neither
            # carries a `canonical_source_uuid` and `_reference` hands back two different
            # uuids for one row). A row this import is *creating* has no id yet, and there
            # the document's own canonical uuid is the identity - one per row, because the
            # alias branch already collapsed any duplicates.
            #
            # Keying the alias half on either one alone misses the other case, and both
            # misses end the same way: two inserts against
            # `ux_gear_service_schedule_item_kind_label` and the whole import refused.
            rule = _key(schedule.type.value, schedule.label)
            gear_identity = str(gear_uuid) if gear_record.row_id is None else str(gear_record.row_id)
            record = self._claim_unique(
                collection,
                record,
                index,
                aliases,
                (gear_identity, *rule),
                "service schedule",
                index_key=None if gear_record.row_id is None else (str(gear_record.row_id), *rule),
            )
        if record.action not in (Action.CREATE, Action.RESTORE):
            return record

        record.values = {
            "user_id": self._user_id,
            "kind": schedule.type.value,
            "label": schedule.label,
            "starts_on": schedule.starts_on,
            "interval_months": months,
            "interval_dives": dives,
            # A *snapshot*, not a derived member (spec §5.7): "the item's lifetime count
            # when this rule began" has no recomputation procedure, and recomputing it
            # against the destination's live counter is the reset-every-baseline failure the
            # distinction exists to prevent. Zero when absent, which is the column's own
            # default and means "count from the beginning".
            "dive_count_at_start": self._count(collection, schedule.uuid, schedule.dive_count_at_start),
            "is_active": True if schedule.active is None else bool(schedule.active),
            "created_at": self._created_at(schedule.created_at),
        }
        # `last_service_on`, `next_due_on` and `next_due_at_dive_count` are derived (spec
        # §5.7) and deliberately absent here: `recalculate_service_schedule` writes them
        # from the records this import just created, which is the only view of them that can
        # be true on this instance.
        record.children = {"gear_uuid": gear_uuid}
        return record

    async def _plan_service_records(self) -> None:
        existing = await self._rows_by_uuid(
            GearServiceRecord, [record.uuid for record in self._document.gear_service_records]
        )
        for service_record in self._document.gear_service_records:
            self._claim_document_uuid("gear_service_records", service_record)
            self._records["gear_service_records"][service_record.uuid] = self._plan_service_record(
                service_record, existing
            )

    def _plan_service_record(
        self, service: ImportGearServiceRecord, existing: dict[uuid_pkg.UUID, _ExistingRow]
    ) -> PlannedRecord:
        collection = "gear_service_records"
        gear_uuid = self._reference(collection, service.uuid, "gear", service.gear_uuid)
        if gear_uuid is None:
            return self._skip(collection, service.uuid, "A service record with no gear item to hang on was skipped.")
        if service.type is None:
            return self._skip(collection, service.uuid, "A service record needs a kind this app recognizes.")
        if service.serviced_on is None:
            return self._skip(collection, service.uuid, "A service record needs the date the work was done.")
        if service.dive_count_at_service is None:
            # `NOT NULL` with no default, and a snapshot with no recomputation procedure -
            # the same shape as a course with no status, and the same answer.
            return self._skip(
                collection,
                service.uuid,
                "A service record needs the item's dive count at the time, and this one does not carry it. Nothing "
                "was invented for it.",
            )

        record = self._resolve(collection, service.uuid, existing)
        if record.action not in (Action.CREATE, Action.RESTORE):
            return record
        record.values = {
            "user_id": self._user_id,
            "kind": service.type.value,
            "serviced_on": service.serviced_on,
            "dive_count_at_service": self._count(collection, service.uuid, service.dive_count_at_service),
            "label": service.label,
            "performed_by": service.performed_by,
            "notes": self._notes_text("gear_service_records", service.uuid, service.notes),
            "created_at": self._created_at(service.created_at),
        }
        record.children = {
            "gear_uuid": gear_uuid,
            "contact_uuid": self._reference(collection, service.uuid, "contacts", service.contact_uuid),
            # Absent when the record predates its rule or the rule was deleted - history
            # outlives the rule (spec §6.15), which is why the FK is `ON DELETE SET NULL`.
            "schedule_uuid": self._reference(
                collection, service.uuid, "gear_service_schedules", service.gear_service_schedule_uuid
            ),
        }
        return record

    async def _plan_certifications(self) -> None:
        existing = await self._rows_by_uuid(
            Certification, [certification.uuid for certification in self._document.certifications]
        )
        for certification in self._document.certifications:
            self._claim_document_uuid("certifications", certification)
            self._records["certifications"][certification.uuid] = self._plan_certification(certification, existing)

    def _plan_certification(
        self, certification: ImportCertification, existing: dict[uuid_pkg.UUID, _ExistingRow]
    ) -> PlannedRecord:
        collection = "certifications"
        if not (certification.name or "").strip():
            return self._skip(collection, certification.uuid, "A certification needs a name, and this one has none.")
        agency = self._agency(collection, certification.uuid, certification)
        if agency is None:
            return self._skip(
                collection,
                certification.uuid,
                "A certification needs an agency this app recognizes, and this one does not name one.",
            )
        record = self._resolve(collection, certification.uuid, existing)
        if record.action not in (Action.CREATE, Action.RESTORE):
            self._count_uncontained_cards(certification.front_file, certification.back_file)
            return record

        record.values = {
            "user_id": self._user_id,
            "agency": agency[0],
            "agency_other": agency[1],
            "name": certification.name,
            "certification_number": certification.number,
            "certified_on": certification.certified_on,
            "expires_on": certification.expires_on,
            "instructor_number": certification.instructor_number,
            "notes": self._notes_text("certifications", certification.uuid, certification.notes),
            "created_at": self._created_at(certification.created_at),
        }
        record.children = {
            **self._contact_link(
                collection, certification.uuid, certification.contact_uuid, certification.training_center
            ),
            "course_uuid": self._reference(collection, certification.uuid, "courses", certification.course_uuid),
            "instructor": self._certification_instructor(certification),
            CertificationSide.FRONT.value: self._plan_card_file(
                collection, certification.uuid, certification.front_file
            ),
            CertificationSide.BACK.value: self._plan_card_file(collection, certification.uuid, certification.back_file),
        }
        return record

    async def _load_tag_index(self) -> None:
        """The caller's tags by `tag_key`, before the first collection that names one - a
        site's tags and a dive's are one vocabulary."""
        tags = await self._db.execute(select(Tag.name, Tag.id).where(Tag.user_id == self._user_id))
        self._tag_index = {tag_key(row.name): row.id for row in tags}

    async def _plan_dives(self) -> None:
        existing = await self._rows_by_uuid(Dive, [dive.uuid for dive in self._document.dives])
        self._claimed_digests = await self._stored_digests()
        for dive in self._document.dives:
            self._claim_document_uuid("dives", dive)
            self._records["dives"][dive.uuid] = await self._plan_dive(dive, existing)

    async def _stored_digests(self) -> dict[str, int | None]:
        """Which of the digests this file could store the account holds already, and where.

        The kept file's own and an archive's members' - asked by digest rather than read for
        the whole account, because an import of many files asks once per file, against a
        logbook each earlier file has just grown.
        """
        wanted = [] if self._loaded.kept is None else [self._loaded.kept.sha256]
        if self._loaded.is_archive:
            wanted.extend(
                stored.sha256
                for dive in self._document.dives
                for recording in dive.recordings
                for stored in recording.source_files
                if stored is not None and stored.sha256 is not None
            )
        held: dict[str, int | None] = {}
        unique = sorted(set(wanted))
        for start in range(0, len(unique), _DIGESTS_PER_QUERY):
            rows = await self._db.execute(
                select(DiveFile.sha256, DiveFile.recording_id).where(
                    DiveFile.user_id == self._user_id,
                    DiveFile.sha256.in_(unique[start : start + _DIGESTS_PER_QUERY]),
                )
            )
            held.update({row.sha256: row.recording_id for row in rows})
        return held

    async def _load_candidates(self, around: datetime) -> list[RecordingCandidate]:
        """This account's recordings near one incoming start.

        **One indexed range scan per incoming recording, and there is no batching to be had.**
        The window is anchored on each recording's own start, and a logbook spans years, so a
        single read covering the whole document would be the whole table - which is the query
        the index exists to avoid. What bounds the cost instead is `_has_recordings`: an
        account with none answers every gate without a query at all, which is what an import
        into a fresh account is and what makes restoring a whole archive cost nothing here.
        """
        if not await self._has_recordings():
            return []
        return await load_candidates(self._db, user_id=self._user_id, around=around)

    async def _has_recordings(self) -> bool:
        """Whether this account has any recording at all, asked once per document.

        The guard that keeps the gates off the hot path for the case they can never fire on:
        an import into an empty account has nothing to match against, and asking per
        recording would be one range scan per dive to learn that the table is empty.
        """
        if self._account_has_recordings is None:
            self._account_has_recordings = (
                await self._db.execute(select(DiveRecording.id).where(DiveRecording.user_id == self._user_id).limit(1))
            ).scalar_one_or_none() is not None
        return self._account_has_recordings

    async def _match_recordings(
        self,
        dive: ImportDive,
        recordings: list[PlannedRecording],
        values: dict[str, Any],
        mixtures: list[dict[str, Any]],
    ) -> tuple[list[PlannedRecording], int]:
        """Attach what belongs to a dive the caller already has; return what is left and how
        many were taken.

        **The count is not `len(recordings) - len(remaining)`**, and that is the whole reason
        it is returned rather than derived: a recording can also leave this list by having
        been dropped upstream for describing nothing, and a caller reading the difference
        would take "one unusable recording" for "one recording already in the logbook" and
        skip a dive it should have created.

        **Same-recording first, then same-dive-strict**, and the order is the whole of the
        rule: a file that is a second reading of a record the logbook already holds must fill
        that record rather than be appended beside it as though a second computer had
        recorded it. The loose gate is not used here at all - it is a form's, where a diver
        decides - because an import that attached on a start window alone would silently fold
        a repetitive dive into the one before it.

        The dive's own *values* ride along on a `fill` only, so a match can still supply
        readings the stored dive has none of; on an `attach` they are dropped, the recording
        being a second computer's and the dive's figures the primary recording's. Its
        **cylinders** ride along on both: on either they fill the blanks of the dive's rows
        they pair with, and on an attach they are also what the second computer's
        `gas_number`s are mapped *from*.
        """
        remaining: list[PlannedRecording] = []
        taken = 0
        for recording in recordings:
            if recording.start_time is None:
                remaining.append(recording)
                continue

            incoming = RecordingFacts(
                device=DeviceIdentity(
                    brand=recording.device.get("device_brand"),
                    model=recording.device.get("device_model"),
                    serial=recording.device.get("device_serial"),
                    dive_number=recording.device.get("device_dive_number"),
                ),
                start_time=recording.start_time,
                utc_offset_minutes=recording.utc_offset_minutes,
                duration=recording.duration,
                max_depth=recording.max_depth,
                sampled_span=None if recording.profile is None else recording.profile.profile.duration,
            )
            candidates = await self._load_candidates(recording.start_time)

            filled = next((candidate for candidate in candidates if is_same_recording(incoming, candidate.facts)), None)
            if filled is not None:
                self._recording_matches.append(
                    PlannedRecordingMatch(
                        kind="fill",
                        dive_id=filled.dive_id,
                        dive_uuid=filled.dive_uuid,
                        source_uuid=dive.uuid,
                        recording_id=filled.id,
                        recording=recording,
                        ordinal=filled.ordinal,
                        dive_values=values,
                        mixtures=mixtures,
                    )
                )
                if filled.dive_id not in self._batch_dive_ids:
                    self._note(
                        ImportNoteCode.RECORDING_FILLED,
                        "A recording of this dive is one your logbook already has - the same device, the same start - "
                        "so it filled in what that record was missing rather than being added again. Dive "
                        f"{filled.dive_number} kept everything it already recorded.",
                        collection="dives",
                        uuid=dive.uuid,
                    )
                taken += 1
                continue

            attached = next(
                (candidate for candidate in candidates if is_same_dive_strict(incoming, candidate.facts)), None
            )
            if attached is not None:
                self._recording_matches.append(
                    PlannedRecordingMatch(
                        kind="attach",
                        dive_id=attached.dive_id,
                        dive_uuid=attached.dive_uuid,
                        source_uuid=dive.uuid,
                        recording_id=None,
                        recording=recording,
                        # No stored recording to have a position: the writer appends this one
                        # and computes the slot with `next_ordinal`.
                        ordinal=None,
                        # **Carried on an attach as well as on a fill**, and with one more
                        # job: the writer maps this second computer's `gas_number`s from them
                        # onto the ones the dive already has, as well as filling the blanks of
                        # the rows they pair with. Without them the mapping has nothing to map
                        # from and the recording's pressure channels land naming another
                        # computer's tanks - the misattribution `relabel_gas_numbers` exists
                        # to prevent.
                        mixtures=mixtures,
                    )
                )
                if attached.dive_id not in self._batch_dive_ids:
                    self._note(
                        ImportNoteCode.RECORDING_ATTACHED,
                        "A different computer recorded a dive your logbook already has, so this recording was added "
                        f"to dive {attached.dive_number} rather than a second dive being created for it.",
                        collection="dives",
                        uuid=dive.uuid,
                    )
                taken += 1
                continue

            remaining.append(recording)
        return remaining, taken

    async def _plan_dive(self, dive: ImportDive, existing: dict[uuid_pkg.UUID, _ExistingRow]) -> PlannedRecord:
        collection = "dives"
        if dive.started_at is None:
            self._not_writing_the_file()
            return self._skip(collection, dive.uuid, "A dive needs a start time, and this one has none.")

        record = self._resolve(collection, dive.uuid, existing)
        if record.action not in (Action.CREATE, Action.RESTORE):
            self._count_uncontained_files(dive)
            self._not_writing_the_file()
            return record

        bounded = self._bounded(collection, dive.uuid, dive, _DIVE_BOUNDS)
        recordings = self._plan_recordings(dive)
        # The primary recording's profile, for the dive-level jobs a profile still has: standing
        # in for an unrecorded duration, average depth and bottom temperature below, and keeping
        # the skip message honest. Capped, which keeps every channel's extremes
        # (`_downsample_series`).
        profile = recordings[0].profile if recordings else None
        in_water = recordings[0].in_water if recordings else None

        # Derived, and reported as derived - which §5.4 permits and inventing does not. The
        # profile is the recording of this very dive, so its time in the water is what a
        # reader with no stated figure takes (`divejson.in_water`), and its span - in
        # milliseconds, where a dive's duration is whole seconds - stands in only where no
        # sample was in the water.
        duration = bounded.get("duration")
        if duration is None and in_water is not None and in_water.duration > 0:
            duration = in_water.duration
            self._note(
                ImportNoteCode.VALUE_DERIVED,
                "This dive records no duration, so its length was taken as the time its own profile spends deeper "
                f"than {IN_WATER_DEPTH} m.",
                collection=collection,
                uuid=dive.uuid,
            )
        elif duration is None and profile is not None:
            duration = round(profile.duration / MILLISECONDS_PER_SECOND)
            self._note(
                ImportNoteCode.VALUE_DERIVED,
                "This dive records no duration, so its length was taken from the span of its own profile.",
                collection=collection,
                uuid=dive.uuid,
            )
        if duration is None or duration <= 0:
            self._not_writing_the_file()
            return self._skip(
                collection,
                dive.uuid,
                "A dive needs a duration, and this one carries neither a duration nor a profile to take one from. "
                "Nothing was invented for it.",
            )

        max_depth, avg_depth = bounded.get("max_depth"), bounded.get("avg_depth")
        if avg_depth is None and dive.avg_depth is None and in_water is not None:
            avg_depth = float(in_water.avg_depth)
            self._note(
                ImportNoteCode.VALUE_DERIVED,
                "This dive records no average depth, so it was taken as the mean depth of its own profile over the "
                f"time it spends deeper than {IN_WATER_DEPTH} m.",
                collection=collection,
                uuid=dive.uuid,
            )
        if max_depth is not None and avg_depth is not None and avg_depth > max_depth:
            # A pair rule, so there is no "the bad value": `avg_depth` is the half that goes
            # for the same reason revision `c4d81e6b3f57` clears that one - `max_depth`
            # feeds the dive list, the diver's stats and UDDF's mandatory `<greatestdepth>`,
            # while `avg_depth` feeds only gas arithmetic, which declines to compute rather
            # than compute wrongly when it is absent.
            self._dropped(collection, dive.uuid, "The average depth was deeper than the maximum, and was dropped")
            avg_depth = None

        visibility = bounded.get("visibility")
        if visibility is not None and visibility != int(visibility):
            # The format has visibility as a number and this app has it as whole metres.
            # Rounding would be an invented precision in the other direction, so the value
            # goes and the dive stays.
            self._dropped(
                collection,
                dive.uuid,
                "Visibility was recorded in fractions of a metre, which this app stores in whole metres, and was "
                "dropped",
            )
            visibility = None

        stated_temperature = dive.bottom_temperature if finite(dive.bottom_temperature) else None
        temperature = bottom_temperature(stated_temperature, None if profile is None else profile.profile)

        entry = self._position(collection, dive.uuid, dive.entry_position, "entry")
        exit_ = self._position(collection, dive.uuid, dive.exit_position, "exit")
        # A bare date is the date-only state (spec §5.2): stored as its day with no clock,
        # never as a midnight somebody would read as the time the dive began.
        start_time, offset_minutes, date_only = split_dive_start_time(dive.started_at)
        number = bounded.get("number")
        record.values = {
            "user_id": self._user_id,
            # A dive number is the diver's own numbering and `NOT NULL` here. Absent - or
            # dropped by the bound above - the dive takes the number the dive form would
            # suggest, given once the whole import is written (`import_batch`), because the
            # import's unnumbered dives are numbered in date order across all its files and
            # no one file's plan sees the others. `0` holds the column until then. The
            # suggestion is given even where another dive holds it: duplicate dive numbers
            # are legal by design (see `DiveNumberingSummary`, which counts them rather than
            # refusing them), so a clash costs nothing a diver cannot fix, while dropping the
            # dive would lose everything else it carries.
            "dive_number": 0 if number is None else number,
            "start_time": start_time,
            "utc_offset_minutes": offset_minutes,
            "start_date_only": date_only,
            "duration": int(duration),
            "notes": self._notes_text("dives", dive.uuid, dive.notes),
            "max_depth": max_depth,
            "avg_depth": avg_depth,
            "bottom_temperature": temperature,
            "visibility": None if visibility is None else int(visibility),
            "weight": bounded.get("weight"),
            "water_type": None if dive.water_type is None else dive.water_type.value,
            "altitude": bounded.get("altitude"),
            "type": None if dive.type is None else dive.type.value,
            "rating": bounded.get("rating"),
            "air_temperature": dive.air_temperature if finite(dive.air_temperature) else None,
            "current": None if dive.current is None else dive.current.value,
            "waves": None if dive.waves is None else dive.waves.value,
            "weather": None if dive.weather is None else dive.weather.value,
            "entry_type": None if dive.entry_type is None else dive.entry_type.value,
            # Stored trimmed and never blank, as a dive write's `BoatName` stores it.
            "boat_name": (dive.boat_name or "").strip() or None,
            "entry_latitude": entry[0],
            "entry_longitude": entry[1],
            "exit_latitude": exit_[0],
            "exit_longitude": exit_[1],
            "created_at": self._created_at(dive.created_at),
        }
        mixtures = self._plan_cylinders(dive)

        # **The gates run before a dive is created, and only for a uuid the caller does not
        # hold.** A `RESTORE` is the caller's own deleted dive coming back under its own
        # identity, and a `LINK` is a dive they already have - matching either against the
        # logbook would be asking whether a dive is itself. `CREATE` covers the genuinely new
        # dive, the remapped one and one whose identity the file's own bytes gave it, and the
        # remapped case is where this matters most: another account's export carries uuids
        # that mean nothing here, so uuid matching has nothing to work with and the device and
        # the clock are all there is.
        if record.action is Action.CREATE:
            already = len(self._recording_matches)
            recordings, matched = await self._match_recordings(dive, recordings, record.values, mixtures)
            if matched and not recordings:
                # Every recording of this dive is now on a dive the caller already has, so
                # there is nothing left for a dive row to hold. Creating one would be the
                # duplicate the gates exist to prevent. The notes above already say which
                # dive each recording reached.
                #
                # `matched` rather than "the document listed recordings": a dive whose only
                # recording was *dropped* for describing nothing still has everything else
                # the document says about it, and skipping it would lose a real dive over an
                # unusable object inside it.
                self._keep_on_match(already)
                return PlannedRecord(action=Action.SKIP, source_uuid=dive.uuid, uuid=record.uuid)
        if stated_temperature is None and temperature is not None:
            # The dive form's default, derived and reported as the duration is - here, where the
            # dive is known to be written, since a match writes no dive's temperature.
            self._note(
                ImportNoteCode.VALUE_DERIVED,
                "This dive records no bottom temperature, so it was taken from the coldest reading of its own profile.",
                collection=collection,
                uuid=dive.uuid,
            )
        recordings = self._keep_on_dive(dive, recordings)

        record.children = {
            "trip_uuid": self._reference(collection, dive.uuid, "trips", dive.trip_uuid),
            "course_uuid": self._reference(collection, dive.uuid, "courses", dive.course_uuid),
            "contact_uuid": self._reference(collection, dive.uuid, "contacts", dive.contact_uuid),
            "site_uuids": self._reference_list(collection, dive.uuid, "sites", dive.site_uuids),
            "gear_uuids": self._reference_list(collection, dive.uuid, "gear", dive.gear_uuids),
            "sightings": self._plan_sightings(dive),
            "people": self._person_references(collection, dive.uuid, dive.people),
            "tags": self._tag_names(dive.tags, collection=collection, record_uuid=dive.uuid),
            "mixtures": mixtures,
            "recordings": recordings,
        }
        if number is None:
            self._unnumbered_dives.append(dive.uuid)
        return record

    def _tag_names(
        self, names: Sequence[str], *, collection: str | None = None, record_uuid: uuid_pkg.UUID | None = None
    ) -> list[str]:
        """Tags as a dive or site write stores them: each trimmed, a blank one dropped, one longer than
        the column dropped and noted, and two that fold to one kept once, at the first -
        what the read model would refuse is restated here, since the import validates no
        schema of its own. Each the caller lacks is one this import makes."""
        kept: dict[str, str] = {}
        for raw in names:
            name = trim_tag(raw)
            if not name:
                continue
            if len(name) > TAG_NAME_MAX:
                self._note(
                    ImportNoteCode.VALUE_DROPPED,
                    f"A tag longer than {TAG_NAME_MAX} characters, this app's limit, was dropped",
                    collection=collection,
                    uuid=record_uuid,
                )
                continue
            key = tag_key(name)
            kept.setdefault(key, name)
            if key not in self._tag_index:
                self._new_tags.setdefault(key, name)
        return list(kept.values())

    def _plan_tag_list(self) -> None:
        """The diver's whole tag list, from this app's extension - the one value the import
        reads from that block, so a tag on no dive or site survives a round trip. Only strings count:
        §5.5 lets anything sit under the key."""
        diver = self._document.diver
        listed = None if diver is None else _producer_entry(diver, "tags")
        if isinstance(listed, list):
            self._tag_list = self._tag_names([name for name in listed if isinstance(name, str)])

    def _note_new_tags(self) -> None:
        """Name the tags the import makes, in one note. Not a report line: a tag is a member of
        a dive, not a collection the document carries - but a preview says what it creates."""
        if not self._new_tags:
            return
        names = ", ".join(f'"{name}"' for name in self._new_tags.values())
        self._note(ImportNoteCode.TAGS_CREATED, f"This import adds these tags to your list: {names}.")

    def _plan_sightings(self, dive: ImportDive) -> list[StoredSighting]:
        """A dive's sightings, each naming its catalog row, with the count bounded and the
        note cut at the cap.

        The one reference kind that resolves to something the caller does not own, and the
        one that can legitimately come back short: a species the catalog cannot be made to
        hold is skipped, and the dive imports without it. Never the other way round.

        **The first sighting of a species is kept and any other is reported.** A document
        naming one species twice on a dive, or two species records sharing an AphiaID, breaks
        the format's one-sighting-per-species rule; folding the two, as a merge does, would
        invent a sighting neither record holds and hide the defect. Keyed on the AphiaID,
        which every sighting reaching that check carries and which preview can see before
        the pre-pass has found a row.
        """
        sightings: list[StoredSighting] = []
        listed: set[int | None] = set()
        for sighting in dive.sightings:
            record = self._records["species"].get(sighting.species_uuid)
            if record is None or record.action is Action.SKIP:
                self._note(
                    ImportNoteCode.SPECIES_UNRESOLVED,
                    "A species this dive records could not be matched to this instance's catalog, so the sighting "
                    "was not imported.",
                    collection="dives",
                    uuid=dive.uuid,
                )
                continue
            species = self._species_by_uuid[sighting.species_uuid]
            if species.aphia_id in listed:
                self._dropped(
                    "dives",
                    dive.uuid,
                    f"A second sighting of {species.scientific_name or f'species {species.uuid}'} was dropped: a "
                    "dive records each species once, and the first was kept",
                )
                continue
            listed.add(species.aphia_id)
            count = self._bounded("dives", dive.uuid, sighting, _SIGHTING_BOUNDS).get("count")
            notes = self._notes_text("dives", dive.uuid, sighting.notes)
            row_id = self._species_row_by_uuid.get(sighting.species_uuid)
            if row_id is None:
                # Preview, and this species is one the pre-pass has not looked up yet. The
                # species collection's own note already says it will be, and saying "the
                # sighting was not imported" here would contradict that in the same report -
                # while every dive naming a new species spent a note against the cap.
                continue
            sightings.append(StoredSighting(species_id=row_id, count=count, notes=notes))
        return sightings

    def _plan_cylinders(self, dive: ImportDive) -> list[dict[str, Any]]:
        """A dive's gas supplies, as `dive_mixture` rows.

        **A cylinder the document did not fully describe is stored as it was recorded, not
        filled in and no longer skipped.** `volume`, `oxygen` and `helium` are OPTIONAL in
        the format - §6.3 blesses a cylinder converted from a mix-only source with its
        vessel members absent, and says of `oxygen` in as many words that absent means not
        recorded, not 21 - and they are nullable columns here now, so absence has somewhere
        to land. Divers plan gas off these numbers, which is why nothing is guessed; the
        cylinder itself is real either way, and dropping it lost the pressures, the role
        and the gas number it *did* carry along with the member it didn't.

        A member the document records but this app cannot store is a different case and
        still goes: `_MIXTURE_BOUNDS` drops it with a note, exactly as it does an
        out-of-range pressure or a `NaN`, and the cylinder keeps everything else. So does
        each of the two cross-field rules, which is what the pressure pair below has always
        done - an oxygen and a helium summing past 100 cannot both be right and neither
        says which is wrong, so both go and the row stays. Nothing in here skips a cylinder
        any more.

        **A converted file's pressure at or below 0 bar is absent**, as the dive form reads the
        same file (`DiveMixtureSchema._drop_unpressurized`): a Shearwater UDDF writes 0 bar for
        every tank slot its transmitters never read, and stored as an end pressure that 0 would
        read as set to every later fill. Dropped without a note, since it is an absent-marker
        rather than a value. A DiveJSON document keeps the bounds a diver's own record takes,
        where an end pressure of 0 is an out-of-gas ascent (spec §6.3).

        **The measured members are first rounded as the dive form rounds them**, so one file
        stores one cylinder through either door, and a value the rounding takes to 0 meets the
        bounds and the rule above as the 0 it would be stored as.
        """
        converted = self._loaded.conversion is not None
        rows: list[dict[str, Any]] = []
        for index, source in enumerate(dive.cylinders):
            cylinder = source.model_copy(
                update={name: two_places(getattr(source, name)) for name in _ROUNDED_CYLINDER_MEMBERS}
            )
            if converted:
                cylinder = cylinder.model_copy(update=dict.fromkeys(_unpressurized(cylinder), None))
            bounded = self._bounded("dives", dive.uuid, cylinder, _MIXTURE_BOUNDS)
            volume = bounded.get("volume")
            oxygen = bounded.get("oxygen")
            helium = bounded.get("helium")
            if oxygen is not None and helium is not None and oxygen + helium > 100:
                self._dropped(
                    "dives",
                    dive.uuid,
                    f"Cylinder {index + 1}'s oxygen and helium add up to more than 100 percent, so both went",
                )
                oxygen = helium = None
            start_pressure = bounded.get("start_pressure")
            end_pressure = bounded.get("end_pressure")
            if start_pressure is not None and end_pressure is not None and end_pressure > start_pressure:
                self._dropped(
                    "dives", dive.uuid, f"Cylinder {index + 1} ended at a higher pressure than it started, so both went"
                )
                start_pressure = end_pressure = None
            rows.append(
                {
                    "volume": volume,
                    "oxygen": oxygen,
                    "helium": helium,
                    "start_pressure": start_pressure,
                    "end_pressure": end_pressure,
                    # The column keeps the app's own spelling; only the format's changed.
                    "po2_limit": bounded.get("ppo2_limit"),
                    "gas_number": bounded.get("gas_number"),
                    "role": None if cylinder.role is None else cylinder.role.value,
                    "usage": None if cylinder.usage is None else cylinder.usage.value,
                }
            )
        return rows

    def _plan_profile(
        self, dive_uuid: uuid_pkg.UUID, source: ImportProfile, shaped: NormalizedProfile
    ) -> PlannedProfile:
        """A recording's shaped samples, attributed and capped, with the span they cover.

        `recording_shape.shape_profile` has already checked every channel under §6.5's rules;
        what the import adds is the one thing only a document has, its declared `duration`.
        The attribution runs before the cap, because it reads a mean depth off the
        full-resolution channel.
        """
        capped = downsample(replace(shaped, gas_attribution=derive_gas_attribution(shaped)))
        # The document's own `duration` when it covers the samples, which is the case §6.4
        # blesses: a computer that stops sampling at the surface can keep timing the dive,
        # and that span is what the app's gas-coverage fraction is a fraction *of*. A
        # `duration` that fails to cover its own samples is incoherent, so the samples win.
        declared = source.duration if source.duration is not None else 0
        if not 0 <= declared <= INT32_MAX:
            self._dropped(
                "dives", dive_uuid, "The profile declared a span this app cannot store, so its samples' own was used"
            )
            declared = 0
        return PlannedProfile(profile=capped, duration=max(declared, capped.duration))

    # ------------------------------------------------------------------ recordings

    def _plan_recordings(self, dive: ImportDive) -> list[PlannedRecording]:
        """A dive's recordings, in the document's order - the first primary.

        Each is shaped by `recording_shape.shape_recording`, the function the attach path
        shapes a dive-computer file's recording with, so the import and the attach store one
        recording for one file; what this adds is the archive's files and the document's
        declared span. A recording that describes nothing is dropped there, with its note.
        """
        planned: list[PlannedRecording] = []
        for source in dive.recordings:
            shaped = shape_recording(dive, source, self._drop_on("dives", dive.uuid))
            if shaped is None:
                continue
            files = [
                planned_file
                for stored in source.source_files
                if (planned_file := self._plan_dive_file(dive, stored)) is not None
            ]
            profile = (
                None
                if shaped.profile is None or source.profile is None
                else self._plan_profile(dive.uuid, source.profile, shaped.profile)
            )
            duration, max_depth = gate_figures(None if profile is None else profile.profile)
            planned.append(
                PlannedRecording(
                    ordinal=len(planned),
                    device=shaped.device,
                    mode=shaped.mode,
                    deco_model=shaped.deco_model,
                    salinity=shaped.salinity,
                    readouts=shaped.readouts,
                    start_time=shaped.start_time,
                    utc_offset_minutes=shaped.utc_offset_minutes,
                    duration=duration,
                    max_depth=max_depth,
                    profile=profile,
                    in_water=None if profile is None else in_water_of(shaped.profile),
                    files=files,
                )
            )
        return planned

    # ------------------------------------------------------------------ files

    def _kept_file(self, *, reaches: int | None) -> PlannedFile | None:
        """The imported file as the file of the recording it reaches, or `None` where it is
        not stored - the rule the dive form applies to a file it is handed.

        `reaches` is the stored recording a match names, or `None` for a recording this
        import creates. A file whose bytes the recording it reaches already holds is a repeat
        and adds nothing, without a note; one whose bytes another recording of the account
        holds is skipped with the note an archive's file gets, since a file belongs to one
        recording.
        """
        kept = self._loaded.kept
        if kept is None:
            return None
        if kept.sha256 in self._claimed_digests:
            self._not_kept = ImportMemberNotKept.ALREADY_STORED
            if reaches is None or self._claimed_digests[kept.sha256] != reaches:
                self._note(
                    ImportNoteCode.FILE_SKIPPED,
                    "You already store an identical dive-computer file against another recording, and a file belongs "
                    "to one recording, so this one was not kept.",
                    collection="dives",
                    uuid=self._document.dives[0].uuid,
                )
            return None
        self._claimed_digests[kept.sha256] = None
        self._kept, self._not_kept = True, None
        return PlannedFile(
            archive_path=kept.key,
            sha256=kept.sha256,
            original_filename=kept.filename,
            content_type=content_type_of(kept.format),
            parser_key=kept.format,
        )

    def _keep_on_match(self, already: int) -> None:
        """The imported file on the recording its dive's one recording matched."""
        if self._loaded.kept is None:
            return
        for index in range(already, len(self._recording_matches)):
            match = self._recording_matches[index]
            planned = self._kept_file(reaches=match.recording_id)
            if planned is not None:
                self._recording_matches[index] = replace(match, recording=replace(match.recording, kept=planned))

    def _keep_on_dive(self, dive: ImportDive, recordings: list[PlannedRecording]) -> list[PlannedRecording]:
        """The imported file on the recording of the dive this import writes.

        Where the document gives the dive no recording - a logbook entry with no computer
        behind it - one is created to hold the file, stating nothing but the start the file
        is read from, as the dive form's attach creates one.
        """
        kept = self._loaded.kept
        if kept is None:
            return recordings
        planned = self._kept_file(reaches=None)
        if planned is None:
            return recordings
        if recordings:
            return [replace(recordings[0], kept=planned), *recordings[1:]]
        start = kept.extraction.start
        return [
            PlannedRecording(
                ordinal=0,
                start_time=None if start is None else start[0],
                utc_offset_minutes=None if start is None else start[1],
                kept=planned,
            )
        ]

    def _not_writing_the_file(self) -> None:
        """The imported file's dive is linked or skipped, so the file is not stored."""
        kept = self._loaded.kept
        if kept is not None:
            self._not_kept = (
                ImportMemberNotKept.ALREADY_STORED
                if kept.sha256 in self._claimed_digests
                else ImportMemberNotKept.NOT_WRITTEN
            )

    def _count_uncontained_files(self, dive: ImportDive) -> None:
        """Count the binaries of a dive the import is not writing.

        A linked or skipped dive still *references* its files, and the report's `referenced`
        count is about the document rather than about what got written - so they are counted,
        and counted as not restored.
        """
        for recording in dive.recordings:
            for stored in recording.source_files:
                if stored is not None:
                    self._files_referenced += 1
                    self._files_not_contained += 1

    def _count_uncontained_cards(self, *files: ImportStoredFile | None) -> None:
        """`_count_uncontained_files` for a certification's two card images."""
        for stored in files:
            if stored is not None:
                self._files_referenced += 1
                self._files_not_contained += 1

    def _plan_dive_file(self, dive: ImportDive, stored: ImportStoredFile | None) -> PlannedFile | None:
        """One dive-computer export behind a recording, when the container carries its bytes.

        **A bare document never creates a file row.** It carries the metadata and none of
        the bytes, and in this repo a file row's existence is the claim that the bytes
        exist: `BlobMissingError` treats a row without its blob as data loss, the download
        route 500s on it, and `storage_key` is `NOT NULL` and minted per write - so a
        byteless row would need an invented key as well as an invented promise. The dive
        imports, the file is reported, and the archive is what puts it back.
        """
        if stored is None:
            return None
        self._files_referenced += 1
        if not self._loaded.is_archive or stored.archive_path is None:
            self._note(
                ImportNoteCode.FILE_NOT_CONTAINED,
                "A dive-computer file of this dive is named by the document but not contained in it. Import the "
                "archive to restore it.",
                collection="dives",
                uuid=dive.uuid,
            )
            self._files_not_contained += 1
            return None
        if stored.sha256 is None:
            self._files_skipped += 1
            self._note(
                ImportNoteCode.FILE_SKIPPED,
                "A dive-computer file of this dive carries no digest to verify it against, so it was not restored.",
                collection="dives",
                uuid=dive.uuid,
            )
            return None
        size = self._loaded.member_size(stored.archive_path)
        if size is None:
            self._files_skipped += 1
            self._note(
                ImportNoteCode.FILE_SKIPPED,
                "A dive-computer file of this dive is named by the document but missing from the archive.",
                collection="dives",
                uuid=dive.uuid,
            )
            return None
        if size > MAX_DIVE_FILE_SIZE:
            self._files_skipped += 1
            self._note(
                ImportNoteCode.FILE_SKIPPED,
                f"A dive-computer file of this dive is larger than the {MAX_DIVE_FILE_SIZE // (1024 * 1024)} MB this "
                "app stores, so it was not restored.",
                collection="dives",
                uuid=dive.uuid,
            )
            return None
        if stored.sha256 in self._claimed_digests:
            # `ux_dive_file_user_id_sha256`. Link-to-existing is impossible here: one row
            # names one `recording_id`, so one set of bytes cannot serve two recordings. The
            # recording keeps everything else and simply has that file missing.
            self._files_skipped += 1
            self._note(
                ImportNoteCode.FILE_SKIPPED,
                "You already store an identical dive-computer file against another recording, and a file belongs to "
                "one recording, so this copy was not restored.",
                collection="dives",
                uuid=dive.uuid,
            )
            return None

        self._claimed_digests[stored.sha256] = None
        self._files_restored += 1
        # The format the document says read the file, when this build's reader reads it: a
        # backfill can then re-read the restored file, and the content type comes from this
        # app's own table rather than from a document that could name anything, since that
        # value ends up in a response header on download. The import's own key otherwise,
        # which is the truth about where the row came from.
        parser_key = _producer_entry(stored, "parser_key")
        readable = isinstance(parser_key, str) and reads(parser_key)
        return PlannedFile(
            archive_path=stored.archive_path,
            sha256=stored.sha256,
            original_filename=stored.original_filename or "dive-file",
            content_type=content_type_of(parser_key) if readable else FALLBACK_CONTENT_TYPE,
            parser_key=parser_key if readable else IMPORT_PARSER_KEY,
        )

    def _plan_card_file(
        self, collection: str, record_uuid: uuid_pkg.UUID, stored: ImportStoredFile | None
    ) -> PlannedFile | None:
        """One side of a c-card, when the container carries its bytes.

        `content_type` is deliberately not taken from the document: `store_certification_file`
        sniffs it from the leading bytes precisely so an uploader cannot have the app serve
        arbitrary bytes as a type of their choosing, and a document is no more trustworthy
        than an upload. The writer sniffs, and a file whose bytes are not an image or a PDF
        is skipped there.
        """
        if stored is None:
            return None
        self._files_referenced += 1
        if not self._loaded.is_archive or stored.archive_path is None:
            self._note(
                ImportNoteCode.FILE_NOT_CONTAINED,
                "A card image is named by the document but not contained in it. Import the archive to restore it.",
                collection=collection,
                uuid=record_uuid,
            )
            self._files_not_contained += 1
            return None
        if stored.sha256 is None:
            self._files_skipped += 1
            self._note(
                ImportNoteCode.FILE_SKIPPED,
                "A card image carries no digest to verify it against, so it was not restored.",
                collection=collection,
                uuid=record_uuid,
            )
            return None
        size = self._loaded.member_size(stored.archive_path)
        if size is None or size > MAX_CARD_FILE_SIZE:
            self._files_skipped += 1
            self._note(
                ImportNoteCode.FILE_SKIPPED,
                "A card image is missing from the archive or larger than this app stores, so it was not restored.",
                collection=collection,
                uuid=record_uuid,
            )
            return None
        self._files_restored += 1
        return PlannedFile(
            archive_path=stored.archive_path,
            sha256=stored.sha256,
            original_filename=stored.original_filename or "card",
            content_type="",
        )


def _carried_crop(stored: ImportStoredFile) -> PictureCrop | None:
    """The crop this app wrote beside a portrait, or `None` for anything else under the key."""
    entry = _producer_entry(stored, "crop")
    if not isinstance(entry, dict):
        return None
    try:
        return PictureCrop.model_validate(entry)
    except ValidationError:
        return None


def _carries_settings(extensions: dict[str, Any] | None) -> bool:
    """Whether a diver's `extensions` hold anything but this app's tag list, which the import
    reads - the settings `DIVER_NOT_APPLIED` says it does not apply."""
    for key, entry in (extensions or {}).items():
        if key != DIVEJSON_PRODUCER_KEY or not isinstance(entry, dict) or set(entry) - {"tags"}:
            return True
    return False


def _producer_entry(record: ImportStoredFile | ImportPerson | ImportDiver, member: str) -> Any:
    """One value out of this producer's extension entry (spec §5.5).

    Defensive about the shape all the way down: `extensions` is typed as "any JSON value
    under a producer key", so another implementation's entry under our key is well-formed
    and must not raise here - §5.5 says a reader MUST NOT fail on any well-formed
    `extensions` content.
    """
    extensions = record.extensions or {}
    entry = extensions.get(DIVEJSON_PRODUCER_KEY)
    return entry.get(member) if isinstance(entry, dict) else None


def _person_account(person: ImportPerson) -> uuid_pkg.UUID | None:
    """The account this app's writer linked the person to, as its public id, or `None` for
    anything else under the key."""
    entry = _producer_entry(person, "user_uuid")
    if not isinstance(entry, str):
        return None
    try:
        return uuid_pkg.UUID(entry)
    except ValueError:
        return None


def _same_person(one: PlannedPersonReference, other: PlannedPersonReference) -> bool:
    return (one.source_uuid, one.row_id) == (other.source_uuid, other.row_id)


async def plan_import(
    db: AsyncSession,
    *,
    user_id: int,
    loaded: LoadedImport,
    resolution_ran: bool = False,
    newly_resolved_aphia_ids: frozenset[int] = frozenset(),
    check_in: ImportCheckInSubmission | None = None,
    portrait: ImportPortraitChoice | None = None,
    claim_links: bool = False,
    batch_dive_ids: frozenset[int] = frozenset(),
) -> ImportPlan:
    """Plan an import of `loaded` into `user_id`'s logbook. Writes nothing to the database.

    `check_in` and `portrait` are what the diver confirmed in the preview; the apply passes
    them and the preview, which has nothing submitted yet, does not. `claim_links` is the
    apply's too: it spends the link limit for each person it links, where the preview only
    reads what is left of it. `batch_dive_ids` are the dives the files before this one in the
    same import wrote.
    """
    planner = _Planner(
        db,
        user_id=user_id,
        loaded=loaded,
        resolution_ran=resolution_ran,
        newly_resolved_aphia_ids=newly_resolved_aphia_ids,
        check_in=check_in,
        portrait=portrait,
        claim_links=claim_links,
        batch_dive_ids=batch_dive_ids,
    )
    return await planner.plan()


def portrait_change(plan: ImportPlan) -> tuple[int, int]:
    """What taking the archive's portrait adds and retires, as `(incoming, retired)`.

    Nothing unless the plan takes it, so a preview - which has no choice yet - measures the
    import with the account's own portrait kept, the apply's default. Where the account
    already holds this original only the rendition is written, as `write_imported_portrait`
    does.
    """
    portrait = plan.portrait
    if portrait is None or not portrait.take:
        return 0, 0
    picture, held = portrait.picture, portrait.held
    if held is not None and held.original_sha256 == picture.original_sha256:
        return len(picture.rendition), held.rendition_byte_size or 0
    retired = 0 if held is None else (held.original_byte_size or 0) + (held.rendition_byte_size or 0)
    return len(picture.original) + len(picture.rendition), retired


def unresolved_aphia_ids(document_species: Iterable[ImportSpecies]) -> list[int]:
    """The AphiaIDs a document names, in document order and without repeats.

    What the species pre-pass is handed. Derived from the document rather than from a plan
    because the pre-pass runs *before* the plan that apply writes from - resolving first is
    what lets that plan see the catalog rows this import created.
    """
    seen: list[int] = []
    for species in document_species:
        if species.aphia_id is not None and species.aphia_id not in seen:
            seen.append(species.aphia_id)
    return seen
