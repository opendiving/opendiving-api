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
  that cannot be built is skipped and reported, and the import carries on. The only refusals
  of a whole upload live in `reader.py`.
"""

import math
import uuid as uuid_pkg
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ...core.utils.datetime_offset import split_local_start_time
from ...models.certification import Certification
from ...models.course import Course
from ...models.dive import Dive
from ...models.dive_file import DiveFile
from ...models.dive_site import DiveSite
from ...models.gear_item import GearItem
from ...models.gear_service_record import GearServiceRecord
from ...models.gear_service_schedule import GearServiceSchedule
from ...models.gear_set import GearSet
from ...models.species import Species
from ...models.trip import Trip
from ...schemas.certification import AGENCY_OTHER_NOT_ALLOWED_MESSAGE, CertificationAgency, CertificationSide
from ...schemas.dive_profile import ProfileEventType
from ...schemas.export import DIVEJSON_PRODUCER_KEY
from ...schemas.gear_item import GearType
from ...schemas.logbook_import import (
    ImportCertification,
    ImportCollectionReport,
    ImportCourse,
    ImportDive,
    ImportDiveSite,
    ImportFileReport,
    ImportGearItem,
    ImportGearServiceRecord,
    ImportGearServiceSchedule,
    ImportGearSet,
    ImportNote,
    ImportNoteCode,
    ImportProfile,
    ImportSpecies,
    ImportStoredFile,
    ImportTrip,
)
from ..certification_files import MAX_CARD_FILE_SIZE
from ..dive_files import MAX_DIVE_FILE_SIZE
from ..dive_parsers import PARSER_BY_KEY
from ..dive_profiles import (
    MAX_LABEL_CHARS,
    NormalizedProfile,
    ProfileEvent,
    ProfilePressureSeries,
    ProfileSeries,
    derive_gas_attribution,
    downsample,
)
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
)

# References only ever point *backwards* along this order, so one pass resolves everything:
# a dive names a trip, a course, sites, gear and species; a gear set, a schedule and a
# service record name gear; a certification names a course. Nothing here is recursive,
# which is what makes a fixed order enough rather than a graph walk - and it is the order
# `writer.py` writes in, so a reference always resolves to a row that already exists.
_RESOLUTION_ORDER: tuple[str, ...] = (
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

# How many notes a report carries. A logbook whose every record has something to say about
# it would otherwise produce a response larger than the document it describes; the counts
# stay complete either way, and `notes_truncated` says how many are missing. Well past any
# real import - a clean round trip of the demo logbook produces a handful.
MAX_NOTES = 500

# What `dive_profile.parser_key` records for a profile that arrived through import with no
# file behind it. Deliberately not a key in `PARSER_BY_KEY`: there is no parser that would
# re-read it, and `backfill_profiles` selects its candidates from `dive_file`, so a
# bare-imported dive is never one. On the archive path the dive *does* get a file, and the
# profile records that file's own parser key instead - the file is real and a later
# backfill can legitimately re-read it.
IMPORT_PARSER_KEY = "divejson_import"

# What a restored dive-computer file is recorded as when the document does not say which
# parser read it, or names one this build no longer has. `dive_file.content_type` is
# `String(32)`, and this is what the download route will serve it as.
_FALLBACK_FILE_CONTENT_TYPE = "application/octet-stream"

_LATITUDE_LIMIT = 90.0
_LONGITUDE_LIMIT = 180.0

# Postgres `Integer` is 32-bit, and **the format puts no ceiling on any of its integer
# members** - `dive_number` is a bare `{"type": "integer"}` in the published schema, and
# `duration`, `visibility`, the profile's own `duration` and every `values` entry carry
# only a minimum. So a *conforming* document can hold a number this app's columns cannot,
# and a converter with a unit bug (a duration in microseconds, a depth in micrometres) is
# exactly how one arrives. Unbounded, that is SQLSTATE 22003 raised from the middle of the
# apply transaction: the whole logbook refused over one number, which is the failure every
# other bound here exists to prevent. A `CheckConstraint` census cannot see this, because
# the limit is the column's *width* rather than a rule written on it.
_INT32_MAX = 2**31 - 1
_INT32_MIN = -(2**31)

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
    notes: list[ImportNote]
    notes_dropped: int
    files_referenced: int
    files_restored: int
    files_not_contained: int
    files_skipped: int

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


@dataclass(frozen=True, slots=True)
class _Bound:
    """One column's bound, mirrored from the `CheckConstraint` that enforces it.

    Mirrored rather than derived, on the same terms as `schemas/parsed_dive.py`'s
    validators and for the same reason: a value the database refuses must not take the
    write it rode in on with it. `tests/test_logbook_import.py` counts the single-column
    `ck_dive_*` and `ck_dive_mixture_*` constraints against these tables, so a new one
    cannot be added without a guard here.
    """

    field: str
    ok: Callable[[float], bool]
    message: str


# Every single-column bound on `dive` a document can reach. The pair rules
# (`ck_dive_avg_depth_within_max` and the two position pairs) are deliberately absent:
# there is no "the bad value" to drop in a pair, which is the same reason `parsed_dive.py`
# has no guard for them either. `_plan_dive` handles the depth pair on its own terms, and a
# position is all-or-nothing by shape.
_DIVE_BOUNDS: tuple[_Bound, ...] = (
    _Bound(
        "dive_number",
        lambda value: 0 <= value <= _MAX_DIVE_COUNT,
        f"a dive number must be between 0 and {_MAX_DIVE_COUNT}",
    ),
    _Bound(
        "duration",
        lambda value: 0 < value <= _MAX_DIVE_DURATION_SECONDS,
        "a dive's duration must be greater than zero and shorter than a year",
    ),
    _Bound("max_depth", lambda value: value > 0, "a maximum depth must be greater than zero"),
    _Bound("avg_depth", lambda value: value > 0, "an average depth must be greater than zero"),
    _Bound("visibility", lambda value: 0 <= value <= _INT32_MAX, "visibility must be a non-negative number of metres"),
    _Bound("weight", lambda value: value >= 0, "ballast cannot be negative"),
    _Bound("altitude", lambda value: -450 <= value <= 6500, "altitude must be between -450 and 6500 metres"),
    _Bound("cns_start", lambda value: value >= 0, "a CNS reading cannot be negative"),
    _Bound("cns_end", lambda value: value >= 0, "a CNS reading cannot be negative"),
    _Bound("otu_start", lambda value: value >= 0, "an OTU reading cannot be negative"),
    _Bound("otu_end", lambda value: value >= 0, "an OTU reading cannot be negative"),
    _Bound("surface_pressure", lambda value: 0.4 <= value <= 1.2, "surface pressure must be between 0.4 and 1.2 bar"),
)

# The mixture bounds, same rules, from `models/dive_mixture.py`. `volume`, `oxygen` and
# `helium` are absent because they are required rather than bounded-and-droppable - see
# `_plan_cylinder`.
_MIXTURE_BOUNDS: tuple[_Bound, ...] = (
    _Bound("start_pressure", lambda value: 0 < value <= 350, "a start pressure must be between 0 and 350 bar"),
    _Bound("end_pressure", lambda value: 0 <= value <= 350, "an end pressure must be between 0 and 350 bar"),
    _Bound("po2_limit", lambda value: 0.4 <= value <= 2.0, "a ppO2 limit must be between 0.4 and 2.0 bar"),
    _Bound(
        "gas_number",
        lambda value: 0 <= value <= _INT32_MAX,
        f"a gas number must be between 0 and {_INT32_MAX}",
    ),
)


def _finite(value: float | None) -> bool:
    """`NaN` and `inf` are not readings, whatever the column's bound says.

    Checked before every comparison below rather than folded into one, because a `NaN`
    compares `False` against `<` and `>` alike and so slips through any one-sided guard -
    the exact lesson `_ParserOutput._drop_non_finite` is written up for. Python's `json`
    accepts a bare `NaN` token, so a document really can carry one.
    """
    return value is None or math.isfinite(value)


def _within_int32(values: Sequence[int]) -> bool:
    """Every element storable in a Postgres `Integer` column."""
    return all(_INT32_MIN <= value <= _INT32_MAX for value in values)


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
    ) -> None:
        self._db = db
        self._user_id = user_id
        self._loaded = loaded
        self._document = loaded.document
        # `resolution_ran` is what makes preview and apply tell the truth about species
        # without telling two different stories: before the pre-pass an unknown AphiaID is
        # something this import *will* look up, after it an unknown one is something WoRMS
        # could not answer for.
        self._resolution_ran = resolution_ran
        self._newly_resolved = newly_resolved_aphia_ids
        self._records: dict[str, dict[uuid_pkg.UUID, PlannedRecord]] = {name: {} for name in COLLECTIONS}
        self._notes: list[ImportNote] = []
        self._notes_dropped = 0
        self._files_referenced = 0
        self._files_restored = 0
        self._files_not_contained = 0
        self._files_skipped = 0
        self._species_row_by_uuid: dict[uuid_pkg.UUID, int] = {}
        # Digests this account already stores, plus the ones this import is about to add.
        # `ux_dive_file_user_id_sha256` is per user, so a second dive carrying identical
        # bytes cannot have a row of its own - and `dive_id` being NOT NULL under
        # `ux_dive_file_dive_id` means one row cannot serve two dives either. The file is
        # skipped and reported; the dive is not.
        self._claimed_digests: set[str] = set()

    # ------------------------------------------------------------------ notes

    def _note(self, code: ImportNoteCode, message: str, *, collection: str | None = None, uuid: Any = None) -> None:
        if len(self._notes) >= MAX_NOTES:
            self._notes_dropped += 1
            return
        self._notes.append(ImportNote(code=code, collection=collection, uuid=uuid, message=message))

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

        Mutating the parsed record is what keeps the ten planning functions from each having
        to know about this; it is the reader's copy of the document and nothing else reads
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
        self, collection: str, record_uuid: uuid_pkg.UUID, source: Any, bounds: Sequence[_Bound]
    ) -> dict[str, Any]:
        """Every bounded member of one record, with the unstorable ones dropped."""
        kept: dict[str, Any] = {}
        for bound in bounds:
            value = getattr(source, bound.field)
            if value is None:
                continue
            if not _finite(value):
                self._dropped(collection, record_uuid, f"A value for `{bound.field}` was not a number, and was dropped")
                continue
            if not bound.ok(value):
                self._dropped(collection, record_uuid, f"A value for `{bound.field}` was dropped: {bound.message}")
                continue
            kept[bound.field] = value
        return kept

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
        if not (_finite(latitude) and _finite(longitude)):
            self._dropped(collection, record_uuid, f"The {label} position was not a pair of numbers, and was dropped")
            return None, None
        if abs(latitude) > _LATITUDE_LIMIT or abs(longitude) > _LONGITUDE_LIMIT:
            self._dropped(collection, record_uuid, f"The {label} position was outside the world, and was dropped")
            return None, None
        return latitude, longitude

    def _agency(self, collection: str, record_uuid: uuid_pkg.UUID, source: Any) -> tuple[str, str | None] | None:
        """The `agency`/`agency_other` pair, or `None` when the record cannot carry one.

        `agency` is a REQUIRED member of a vocabulary the format freezes at 1.0 (spec §§6.16,
        7), so a value outside it has already read as absent under §5.6 and the record is
        uninterpretable - skipping it is that rule followed through rather than a second
        one. A stray `agency_other` beside a *named* agency is dropped instead: the app
        refuses the pair (`validate_agency_pairing`), and the agency is the load-bearing
        half.
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

    # ------------------------------------------------------------------ collections

    async def plan(self) -> ImportPlan:
        if self._document.diver is not None:
            self._note(
                ImportNoteCode.DIVER_NOT_APPLIED,
                "The document names its own diver, with a display name, email and unit preference. None of that is "
                "applied: this account keeps its own identity and settings.",
            )
        await self._plan_trips()
        await self._plan_courses()
        await self._plan_sites()
        await self._plan_species()
        await self._plan_gear()
        await self._plan_gear_sets()
        await self._plan_schedules()
        await self._plan_service_records()
        await self._plan_certifications()
        await self._plan_dives()
        return ImportPlan(
            is_archive=self._loaded.is_archive,
            records=self._records,
            notes=self._notes,
            notes_dropped=self._notes_dropped,
            files_referenced=self._files_referenced,
            files_restored=self._files_restored,
            files_not_contained=self._files_not_contained,
            files_skipped=self._files_skipped,
        )

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
        if not (trip.name or "").strip() or trip.starts_on is None:
            return self._skip("trips", trip.uuid, "A trip needs both a name and a start date, and this one does not.")
        record = self._resolve("trips", trip.uuid, existing)
        if record.action is Action.CREATE:
            trip_key = _key(trip.name)
            record = self._claim_unique("trips", record, index, aliases, trip_key, "trip", index_key=trip_key)
        if record.action not in (Action.CREATE, Action.RESTORE):
            return record

        ends_on = trip.ends_on
        if ends_on is not None and ends_on < trip.starts_on:
            self._dropped("trips", trip.uuid, "The end date preceded the start date, and was dropped")
            ends_on = None
        record.values = {
            "user_id": self._user_id,
            "name": trip.name,
            "start_date": trip.starts_on,
            "end_date": ends_on,
            "notes": self._text(trip.notes),
            "created_at": self._created_at(trip.created_at),
        }
        record.children = {"locations": self._plan_trip_locations(trip)}
        return record

    def _plan_trip_locations(self, trip: ImportTrip) -> list[dict[str, Any]]:
        """A trip's places, as `trip_location` rows.

        Value objects with no uuid of their own (spec §6.9), replaced wholesale with the
        trip - so `position` is the list index rather than anything the document carries. A
        bounding box needs its point: a rectangle with no centre frames nothing a reader can
        place, and the writer's own rule is the same one.
        """
        rows: list[dict[str, Any]] = []
        for location in trip.locations:
            if not (location.name or "").strip():
                self._dropped("trips", trip.uuid, "A location with no name was dropped")
                continue
            latitude, longitude = self._position("trips", trip.uuid, location.position, "trip location's")
            bbox = location.bbox
            corners: dict[str, float | None] = dict.fromkeys(
                ("bbox_south", "bbox_north", "bbox_west", "bbox_east"), None
            )
            if bbox is not None and latitude is not None:
                if bbox.south <= bbox.north and all(
                    _finite(corner) for corner in (bbox.south, bbox.north, bbox.west, bbox.east)
                ):
                    corners = {
                        "bbox_south": bbox.south,
                        "bbox_north": bbox.north,
                        "bbox_west": bbox.west,
                        "bbox_east": bbox.east,
                    }
                else:
                    self._dropped("trips", trip.uuid, "A bounding box the geocoder could not have produced was dropped")
            rows.append(
                {
                    "name": location.name,
                    "display_name": location.display_name,
                    "position": len(rows),
                    "latitude": latitude,
                    "longitude": longitude,
                    **corners,
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
        agency = self._agency("courses", course.uuid, course)
        if agency is None:
            return self._skip(
                "courses",
                course.uuid,
                "A course needs an agency this app recognizes, and this one does not name one.",
            )
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
            "agency": agency[0],
            "agency_other": agency[1],
            "status": course.status.value,
            "start_date": course.starts_on,
            "end_date": end_date,
            "instructor_name": course.instructor_name,
            "instructor_number": course.instructor_number,
            "training_center": course.training_center,
            "notes": self._text(course.notes),
            "created_at": self._created_at(course.created_at),
        }
        return record

    async def _plan_sites(self) -> None:
        existing = await self._rows_by_uuid(DiveSite, [site.uuid for site in self._document.sites])
        index = await self._existing_by_key(
            DiveSite, (DiveSite.name, DiveSite.location), lambda row: _key(row[0], row[1])
        )
        aliases: dict[tuple[str, ...], uuid_pkg.UUID] = {}
        for site in self._document.sites:
            self._claim_document_uuid("sites", site)
            self._records["sites"][site.uuid] = self._plan_site(site, existing, index, aliases)

    def _plan_site(
        self,
        site: ImportDiveSite,
        existing: dict[uuid_pkg.UUID, _ExistingRow],
        index: dict[tuple[str, ...], int],
        aliases: dict[tuple[str, ...], uuid_pkg.UUID],
    ) -> PlannedRecord:
        if not (site.name or "").strip():
            return self._skip("sites", site.uuid, "A dive site needs a name, and this one has none.")
        record = self._resolve("sites", site.uuid, existing)
        if record.action is Action.CREATE:
            site_key = _key(site.name, site.location)
            record = self._claim_unique("sites", record, index, aliases, site_key, "dive site", index_key=site_key)
        if record.action not in (Action.CREATE, Action.RESTORE):
            return record

        latitude, longitude = self._position("sites", site.uuid, site.position, "site's")
        record.values = {
            "user_id": self._user_id,
            "name": site.name,
            "location": site.location,
            "latitude": latitude,
            "longitude": longitude,
            "notes": self._text(site.notes),
            "created_at": self._created_at(site.created_at),
        }
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
            "notes": self._text(item.notes),
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
        if weight is not None and not (_finite(weight) and weight >= 0):
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
        if months is not None and not 0 < months <= _INT32_MAX:
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
            "notes": self._text(service.notes),
            "created_at": self._created_at(service.created_at),
        }
        record.children = {
            "gear_uuid": gear_uuid,
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
            self._count_uncontained_files(certification.front_file, certification.back_file)
            return record

        record.values = {
            "user_id": self._user_id,
            "agency": agency[0],
            "agency_other": agency[1],
            "name": certification.name,
            "certification_number": certification.certification_number,
            "certified_on": certification.certified_on,
            "expires_on": certification.expires_on,
            "instructor_name": certification.instructor_name,
            "instructor_number": certification.instructor_number,
            "training_center": certification.training_center,
            "notes": self._text(certification.notes),
            "created_at": self._created_at(certification.created_at),
        }
        record.children = {
            "course_uuid": self._reference(collection, certification.uuid, "courses", certification.course_uuid),
            CertificationSide.FRONT.value: self._plan_card_file(
                collection, certification.uuid, certification.front_file
            ),
            CertificationSide.BACK.value: self._plan_card_file(collection, certification.uuid, certification.back_file),
        }
        return record

    async def _plan_dives(self) -> None:
        existing = await self._rows_by_uuid(Dive, [dive.uuid for dive in self._document.dives])
        self._claimed_digests = set(
            (await self._db.execute(select(DiveFile.sha256).where(DiveFile.user_id == self._user_id))).scalars()
        )
        for dive in self._document.dives:
            self._claim_document_uuid("dives", dive)
            self._records["dives"][dive.uuid] = self._plan_dive(dive, existing)

    def _plan_dive(self, dive: ImportDive, existing: dict[uuid_pkg.UUID, _ExistingRow]) -> PlannedRecord:
        collection = "dives"
        if dive.started_at is None:
            return self._skip(collection, dive.uuid, "A dive needs a start time, and this one has none.")

        record = self._resolve(collection, dive.uuid, existing)
        if record.action not in (Action.CREATE, Action.RESTORE):
            self._count_uncontained_files(dive.source_file)
            return record

        bounded = self._bounded(collection, dive.uuid, dive, _DIVE_BOUNDS)
        profile = self._plan_profile(dive)

        duration = bounded.get("duration")
        if duration is None and profile is not None:
            # Derived, and reported as derived - which §5.4 permits and inventing does not.
            # The profile is the recording of this very dive, so its span is the one number
            # in the document that can honestly stand for a duration the source never wrote.
            duration = profile.duration
            self._note(
                ImportNoteCode.VALUE_DROPPED,
                "This dive records no duration, so its length was taken from the span of its own profile.",
                collection=collection,
                uuid=dive.uuid,
            )
        if duration is None or duration <= 0:
            return self._skip(
                collection,
                dive.uuid,
                "A dive needs a duration, and this one carries neither a duration nor a profile to take one from. "
                "Nothing was invented for it.",
            )

        max_depth, avg_depth = bounded.get("max_depth"), bounded.get("avg_depth")
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

        entry = self._position(collection, dive.uuid, dive.entry_position, "entry")
        exit_ = self._position(collection, dive.uuid, dive.exit_position, "exit")
        start_time, offset_minutes = split_local_start_time(dive.started_at)
        record.values = {
            "user_id": self._user_id,
            # A dive number is the diver's own numbering and `NOT NULL` here. Absent - or
            # dropped by the bound above - it falls back to the placeholder `0` rather than
            # the record being dropped: duplicate dive numbers are legal by design (see
            # `DiveNumberingSummary`, which counts them rather than refusing them), so a
            # placeholder costs nothing a diver cannot fix, while dropping the dive would
            # lose everything else it carries. Every unnumbered dive in one document
            # therefore lands on 0, not on 1, 2, 3.
            "dive_number": bounded.get("dive_number", 0),
            "start_time": start_time,
            "utc_offset_minutes": offset_minutes,
            "duration": int(duration),
            "notes": self._text(dive.notes),
            "max_depth": max_depth,
            "avg_depth": avg_depth,
            "bottom_temperature": dive.bottom_temperature if _finite(dive.bottom_temperature) else None,
            "visibility": None if visibility is None else int(visibility),
            "weight": bounded.get("weight"),
            "water_type": None if dive.water_type is None else dive.water_type.value,
            "altitude": bounded.get("altitude"),
            "cns_start": bounded.get("cns_start"),
            "cns_end": bounded.get("cns_end"),
            "otu_start": bounded.get("otu_start"),
            "otu_end": bounded.get("otu_end"),
            "surface_pressure_bar": bounded.get("surface_pressure"),
            "entry_latitude": entry[0],
            "entry_longitude": entry[1],
            "exit_latitude": exit_[0],
            "exit_longitude": exit_[1],
            "created_at": self._created_at(dive.created_at),
        }
        record.children = {
            "trip_uuid": self._reference(collection, dive.uuid, "trips", dive.trip_uuid),
            "course_uuid": self._reference(collection, dive.uuid, "courses", dive.course_uuid),
            "site_uuids": self._reference_list(collection, dive.uuid, "sites", dive.site_uuids),
            "gear_uuids": self._reference_list(collection, dive.uuid, "gear", dive.gear_uuids),
            "species_ids": self._plan_species_links(dive),
            "mixtures": self._plan_cylinders(dive),
            "profile": profile,
            "source_file": self._plan_dive_file(dive),
        }
        return record

    def _plan_species_links(self, dive: ImportDive) -> list[int]:
        """A dive's sightings, as catalog row ids.

        The one reference kind that resolves to something the caller does not own, and the
        one that can legitimately come back short: a species the catalog cannot be made to
        hold is skipped, and the dive imports without it. Never the other way round.
        """
        ids: list[int] = []
        for species_uuid in dive.species_uuids:
            record = self._records["species"].get(species_uuid)
            row_id = self._species_row_by_uuid.get(species_uuid)
            if record is not None and record.action is not Action.SKIP and row_id is None:
                # Preview, and this species is one the pre-pass has not looked up yet. The
                # species collection's own note already says it will be, and saying "the
                # sighting was not imported" here would contradict that in the same report -
                # while every dive naming a new species spent a note against the cap.
                continue
            if record is None or record.action is Action.SKIP or row_id is None:
                self._note(
                    ImportNoteCode.SPECIES_UNRESOLVED,
                    "A species this dive records could not be matched to this instance's catalog, so the sighting "
                    "was not imported.",
                    collection="dives",
                    uuid=dive.uuid,
                )
                continue
            if row_id not in ids:
                ids.append(row_id)
        return ids

    def _plan_cylinders(self, dive: ImportDive) -> list[dict[str, Any]]:
        """A dive's gas supplies, as `dive_mixture` rows.

        **A cylinder the app cannot represent is skipped, not filled in.** `volume`,
        `oxygen` and `helium` are all `NOT NULL` here and all OPTIONAL in the format, whose
        §6.3 blesses a cylinder converted from a mix-only source with its vessel members
        absent - and §6.3 says of `oxygen` in as many words that absent means not recorded,
        not 21. Divers plan gas off these numbers, so a supply whose mix or size this app
        would have to guess at is reported rather than guessed. `DECISIONS.md` records the
        cost, which falls on the UDDF converter's flagship path.
        """
        rows: list[dict[str, Any]] = []
        for index, cylinder in enumerate(dive.cylinders):
            volume, oxygen, helium = cylinder.volume, cylinder.oxygen, cylinder.helium
            missing = [
                name
                for name, value in (("volume", volume), ("oxygen", oxygen), ("helium", helium))
                if value is None or not _finite(value)
            ]
            if missing or volume is None or oxygen is None or helium is None:
                self._note(
                    ImportNoteCode.RECORD_SKIPPED,
                    f"Cylinder {index + 1} records no {', '.join(missing)}, which this app cannot store without "
                    "assuming a value the document does not carry, so it was skipped.",
                    collection="dives",
                    uuid=dive.uuid,
                )
                continue
            if not (volume > 0 and 0 <= oxygen <= 100 and 0 <= helium <= 100):
                self._note(
                    ImportNoteCode.RECORD_SKIPPED,
                    f"Cylinder {index + 1} records a size or a gas fraction outside what this app can store, "
                    "so it was skipped.",
                    collection="dives",
                    uuid=dive.uuid,
                )
                continue
            if oxygen + helium > 100:
                self._note(
                    ImportNoteCode.RECORD_SKIPPED,
                    f"Cylinder {index + 1}'s oxygen and helium add up to more than 100 percent, so it was skipped.",
                    collection="dives",
                    uuid=dive.uuid,
                )
                continue
            bounded = self._bounded("dives", dive.uuid, cylinder, _MIXTURE_BOUNDS)
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
                    "po2_limit": bounded.get("po2_limit"),
                    "gas_number": bounded.get("gas_number"),
                    "role": None if cylinder.role is None else cylinder.role.value,
                    "usage": None if cylinder.usage is None else cylinder.usage.value,
                }
            )
        return rows

    def _plan_profile(self, dive: ImportDive) -> PlannedProfile | None:
        """A dive's samples, as the stored shape - or nothing, with a note.

        Built directly rather than through `normalize()`, and that is deliberate:
        `normalize` rebases a parser's raw axis onto the earliest reading, which is exactly
        right for a dive-computer file and exactly wrong here. A document's `times` are
        already elapsed seconds from the start of the dive (spec §6.5), so shifting a
        profile whose first sample is at five seconds would move every marker on it. What
        the import does reuse is everything downstream of that: `derive_gas_attribution`
        and `downsample`, in that order, because attribution reads a mean depth off the
        full-resolution channel.
        """
        if dive.profile is None:
            return None
        source = dive.profile
        depth = self._series("dives", dive.uuid, source.depth, "depth")
        ceiling = self._series("dives", dive.uuid, source.ceiling, "ceiling")
        temperature = self._series("dives", dive.uuid, source.temperature, "temperature")
        pressures: list[ProfilePressureSeries] = []
        for series in source.pressures:
            if series.gas_number is None or series.gas_number < 0:
                self._dropped("dives", dive.uuid, "A pressure channel with no gas number was dropped")
                continue
            checked = self._series("dives", dive.uuid, series, f"pressure (gas {series.gas_number})")
            if checked is not None:
                pressures.append(ProfilePressureSeries(t=checked.t, v=checked.v, gas_number=series.gas_number))

        if depth is None and ceiling is None and temperature is None and not pressures:
            if any((source.depth, source.ceiling, source.temperature, source.pressures)):
                self._dropped("dives", dive.uuid, "This dive's profile carried no usable channel, and was dropped")
            return None

        profile = NormalizedProfile(
            depth=depth,
            ceiling=ceiling,
            temperature=temperature,
            pressure=pressures,
            events=self._events(dive.uuid, source),
        )
        attributed = replace(profile, gas_attribution=derive_gas_attribution(profile))
        capped = downsample(attributed)
        # The document's own `duration` when it covers the samples, which is the case §6.4
        # blesses: a computer that stops sampling at the surface can keep timing the dive,
        # and that span is what the app's gas-coverage fraction is a fraction *of*. A
        # `duration` that fails to cover its own samples is incoherent, so the samples win.
        declared = source.duration if source.duration is not None else 0
        if not 0 <= declared <= _INT32_MAX:
            self._dropped(
                "dives", dive.uuid, "The profile declared a span this app cannot store, so its samples' own was used"
            )
            declared = 0
        return PlannedProfile(profile=capped, duration=max(declared, capped.duration))

    def _series(self, collection: str, record_uuid: uuid_pkg.UUID, series: Any, label: str) -> ProfileSeries | None:
        """One channel, or `None` with a note. Spec §6.5's rules, exactly."""
        if series is None:
            return None
        times, values = series.times, series.values
        if len(times) != len(values):
            self._dropped(
                collection, record_uuid, f"The {label} channel had mismatched times and values, and was dropped"
            )
            return None
        if not times:
            # "A channel with no readings must be omitted, not empty" - the same rule
            # `_validate_series` states on the parser side.
            return None
        if times[0] < 0 or any(later <= earlier for earlier, later in zip(times, times[1:], strict=False)):
            self._dropped(
                collection, record_uuid, f"The {label} channel's times were not increasing from zero, and was dropped"
            )
            return None
        # The stored `data` payload is JSONB and holds any integer, but the summary columns
        # `store_profile` derives from these - `max_depth_cm` and the five extremes beside
        # it, and the span - are `Integer`. A channel carrying a value outside that width
        # goes whole, because there is no half of a series to keep.
        if not (_within_int32(times) and _within_int32(values)):
            self._dropped(
                collection, record_uuid, f"The {label} channel carried readings this app cannot store, and was dropped"
            )
            return None
        return ProfileSeries(t=list(times), v=list(values))

    def _events(self, record_uuid: uuid_pkg.UUID, source: ImportProfile) -> list[ProfileEvent]:
        """The markers, sorted, deduped and capped - `_rebase_events` minus the rebasing.

        Sorted here because `derive_gas_attribution` walks them in time order and nothing
        upstream guarantees it. An `other` with no label is dropped rather than kept: the
        format makes the label REQUIRED there (spec §6.6) precisely because an unclassified
        marker with no wording carries no information at all.
        """
        usable: list[tuple[int, ProfileEventType, int | None, str | None]] = []
        for event in source.events:
            if event.time is None or event.type is None:
                # `time` and `type` are both REQUIRED (spec §6.6), and an unknown `type` has
                # already read as absent under §5.6 - either way there is no marker left to
                # draw.
                self._dropped("dives", record_uuid, "A profile event with no time or no recognized type was dropped")
                continue
            label = event.label[:MAX_LABEL_CHARS] if event.label is not None else None
            if event.type is ProfileEventType.OTHER and not (label or "").strip():
                self._dropped("dives", record_uuid, "An unlabelled `other` event was dropped")
                continue
            usable.append((max(0, event.time), event.type, event.gas_number, label))

        seen: set[tuple[int, ProfileEventType, int | None, str | None]] = set()
        ordered: list[ProfileEvent] = []
        for key in sorted(usable, key=lambda entry: entry[0]):
            if key in seen:
                continue
            seen.add(key)
            ordered.append(ProfileEvent(t=key[0], type=key[1], gas_number=key[2], label=key[3]))
        return ordered

    # ------------------------------------------------------------------ files

    def _count_uncontained_files(self, *files: ImportStoredFile | None) -> None:
        """Count the binaries of a record the import is not writing.

        A linked or skipped record still *references* its files, and the report's
        `referenced` count is about the document rather than about what got written - so
        they are counted, and counted as not restored.
        """
        for stored in files:
            if stored is not None:
                self._files_referenced += 1
                self._files_not_contained += 1

    def _plan_dive_file(self, dive: ImportDive) -> PlannedFile | None:
        """The dive-computer export behind a dive, when the container carries its bytes.

        **A bare document never creates a file row.** It carries the metadata and none of
        the bytes, and in this repo a file row's existence is the claim that the bytes
        exist: `BlobMissingError` treats a row without its blob as data loss, the download
        route 500s on it, and `storage_key` is `NOT NULL` and minted per write - so a
        byteless row would need an invented key as well as an invented promise. The dive
        imports, the file is reported, and the archive is what puts it back.
        """
        stored = dive.source_file
        if stored is None:
            return None
        self._files_referenced += 1
        if not self._loaded.is_archive or stored.archive_path is None:
            self._note(
                ImportNoteCode.FILE_NOT_CONTAINED,
                "This dive's original dive-computer file is named by the document but not contained in it. Import the "
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
                "This dive's original file carries no digest to verify it against, so it was not restored.",
                collection="dives",
                uuid=dive.uuid,
            )
            return None
        size = self._loaded.member_size(stored.archive_path)
        if size is None:
            self._files_skipped += 1
            self._note(
                ImportNoteCode.FILE_SKIPPED,
                "This dive's original file is named by the document but missing from the archive.",
                collection="dives",
                uuid=dive.uuid,
            )
            return None
        if size > MAX_DIVE_FILE_SIZE:
            self._files_skipped += 1
            self._note(
                ImportNoteCode.FILE_SKIPPED,
                f"This dive's original file is larger than the {MAX_DIVE_FILE_SIZE // (1024 * 1024)} MB this app "
                "stores, so it was not restored.",
                collection="dives",
                uuid=dive.uuid,
            )
            return None
        if stored.sha256 in self._claimed_digests:
            # `ux_dive_file_user_id_sha256`. Link-to-existing is impossible here: `dive_id`
            # is `NOT NULL` under the full-unique `ux_dive_file_dive_id`, so one row cannot
            # serve two dives. The dive keeps everything else and simply has no source file.
            self._files_skipped += 1
            self._note(
                ImportNoteCode.FILE_SKIPPED,
                "You already store an identical dive-computer file against another dive, and a file belongs to one "
                "dive, so this copy was not restored.",
                collection="dives",
                uuid=dive.uuid,
            )
            return None

        self._claimed_digests.add(stored.sha256)
        self._files_restored += 1
        # The parser registry decides the content type, exactly as `store_dive_file` does -
        # that value ends up in a response header on download, so it is resolved here rather
        # than taken from a document that could name anything.
        parser_key = _producer_entry(stored, "parser_key")
        parser = PARSER_BY_KEY.get(parser_key) if isinstance(parser_key, str) else None
        return PlannedFile(
            archive_path=stored.archive_path,
            sha256=stored.sha256,
            original_filename=stored.original_filename or "dive-file",
            content_type=parser.content_type if parser is not None else _FALLBACK_FILE_CONTENT_TYPE,
            # The document's key when this build still has that parser, so a backfill can
            # re-read the restored file; this import's own otherwise, which is the truth
            # about where the row came from.
            parser_key=parser.key if parser is not None else IMPORT_PARSER_KEY,
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


def _producer_entry(stored: ImportStoredFile, member: str) -> Any:
    """One value out of this producer's extension entry (spec §5.5).

    Defensive about the shape all the way down: `extensions` is typed as "any JSON value
    under a producer key", so another implementation's entry under our key is well-formed
    and must not raise here - §5.5 says a reader MUST NOT fail on any well-formed
    `extensions` content.
    """
    extensions = stored.extensions or {}
    entry = extensions.get(DIVEJSON_PRODUCER_KEY)
    return entry.get(member) if isinstance(entry, dict) else None


async def plan_import(
    db: AsyncSession,
    *,
    user_id: int,
    loaded: LoadedImport,
    resolution_ran: bool = False,
    newly_resolved_aphia_ids: frozenset[int] = frozenset(),
) -> ImportPlan:
    """Plan an import of `loaded` into `user_id`'s logbook. Writes nothing."""
    planner = _Planner(
        db,
        user_id=user_id,
        loaded=loaded,
        resolution_ran=resolution_ran,
        newly_resolved_aphia_ids=newly_resolved_aphia_ids,
    )
    return await planner.plan()


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
