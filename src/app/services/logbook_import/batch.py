"""An import of many files: each imported in turn, inside one transaction, with one report.

**A batch writes what its files would write imported one at a time, in the batch's order** -
each as an import of its own, reviewed and applied - and that is the whole of what two files
make of each other but for dive numbers, given after the last file in date order
(`assign_suggested_numbers`). Each file is planned against the logbook as the files before it
left it, and written before the next is planned, so every rule the import has about a
recording the account stores applies unchanged to one an earlier file of the same batch
brought: the same-recording gate fills it, the strict gate attaches a second computer's to its
dive, and a file whose bytes already gave a dive its identity links. The FIT and the JSON of
one dive are one dive with one recording holding both files, whether they arrive together or
on two days, and the same batch twice writes nothing the second time.

**The upload runs the same pass and rolls it back**, which is what makes its review the
apply's rather than a prediction of it: a later file's outcome depends on what the earlier
ones wrote, and nothing short of writing them says what that is. No object is written by that
pass, and none by the apply's until the whole batch has fitted the account's storage limit
(`staging.py`).

**Each dive's outcome is read off the dive**: the pass reads a stored dive before the first file
writes to it and every dive it touched or created after the last, and `changes.py` says what
differs, so "unchanged" is the writer's fact rather than a prediction.

`DECISIONS.md`, *"An import is its files imported one at a time"*, has the reasoning.
"""

import uuid as uuid_pkg
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.utils.datetime_offset import combine_dive_start_time, split_dive_start_time
from ...models.dive import Dive
from ...models.dive_recording import DiveRecording
from ...models.user import User
from ...schemas.dive import RecordingDevice
from ...schemas.logbook_import import (
    ImportCheckInDetail,
    ImportCheckInSubmission,
    ImportCollectionReport,
    ImportDevice,
    ImportDive,
    ImportDiveNote,
    ImportDiveOutcome,
    ImportFileReport,
    ImportMemberReport,
    ImportNote,
    ImportNoteCode,
    ImportNoteGroup,
    ImportPortraitChoice,
    ImportPortraitOffer,
    ImportSpecies,
)
from ..dive_detail import load_dive_detail
from ..dive_numbering import assign_suggested_numbers
from ..dive_recordings import DEVICE_COLUMNS
from ..storage_usage import ensure_room, get_storage_usage, storage_limit_bytes
from .changes import dive_changes
from .notes import (
    MAX_DIVE_NOTES,
    DiveSource,
    Finding,
    LineKey,
    StoredDive,
    finding_note,
    findings,
    grouped,
    import_note,
    inferred_paths,
    left_out,
    line_key,
    stored_dives,
)
from .planner import COLLECTIONS, Action, ImportPlan, MintedUuids, plan_import, portrait_change
from .reader import LoadedBatch, LoadedImport
from .staging import StagedFiles
from .writer import WrittenImport, write_import

# A document dive, as the batch member it came from and the uuid that document gives it: two
# files' documents can give one uuid to two dives, and a stored dive's uuid can be neither.
type _Source = tuple[int, uuid_pkg.UUID]


@dataclass(slots=True)
class _DiveRow:
    dive_id: int | None
    members: list[int]
    created: bool = False
    reason: str | None = None
    # A skipped dive's own figures, where there is no stored dive to read them from.
    source: ImportDive | None = None
    # The document dives whose notes this row carries, each as (member, the document's uuid).
    sources: list[_Source] = field(default_factory=list)
    # Every document dive that reached this row. A dive whose recordings partly matched dives
    # the caller has reaches several, and its notes go on the first alone.
    reached_by: list[_Source] = field(default_factory=list)


def _reason(plan: ImportPlan, source_uuid: Any) -> str | None:
    """Why a plan skipped one document dive."""
    return next(
        (
            note.message
            for note in plan.notes
            if note.code is ImportNoteCode.RECORD_SKIPPED and note.uuid == source_uuid
        ),
        None,
    )


@dataclass(slots=True)
class BatchDive:
    """One dive the batch's files hold, and what the pass did to it. `dive` is the dive after
    the pass as a dive read gives it, and `changes` what differs from the dive before it."""

    outcome: ImportDiveOutcome
    uuid: uuid_pkg.UUID | None = None
    dive: dict[str, Any] | None = None
    changes: list[dict[str, Any]] = field(default_factory=list)
    start_time: datetime | date | None = None
    device: RecordingDevice | None = None
    members: list[int] = field(default_factory=list)
    reason: str | None = None
    notes: list[ImportDiveNote] = field(default_factory=list)
    notes_truncated: int = 0
    # The document dives that reached this row, each as (the batch row, the document's uuid).
    sources: list[_Source] = field(default_factory=list)


class _DiveDetails:
    """The pass's reads of dives, in the detail route's shape, as JSON."""

    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self._db = db
        self._user_id = user_id
        self._owner_uuid: uuid_pkg.UUID | None = None
        self.before: dict[uuid_pkg.UUID, dict[str, Any]] = {}

    async def read(self, uuid: uuid_pkg.UUID) -> dict[str, Any]:
        if self._owner_uuid is None:
            self._owner_uuid = (await self._db.execute(select(User.uuid).where(User.id == self._user_id))).scalar_one()
        detail = await load_dive_detail(self._db, user_id=self._user_id, owner_uuid=self._owner_uuid, uuid=uuid)
        return detail.model_dump(mode="json", by_alias=True)

    async def note_before(self, uuid: uuid_pkg.UUID) -> None:
        """Read a stored dive before the first file writes to it."""
        if uuid not in self.before:
            self.before[uuid] = await self.read(uuid)


@dataclass(slots=True)
class _File:
    """One file of the batch as the report reads it: what it planned and found, each indexed
    by the document dive it is about once, since a logbook's rows each look theirs up."""

    loaded: LoadedImport
    plan: ImportPlan
    findings: list[Finding]
    # By the document dive's uuid: the import's own notes about it, its findings, its index
    # in the document - the first, where two dives give one uuid - and where its recordings
    # landed, by their index in the document -> (the stored dive, its ordinal there).
    notes_about: dict[uuid_pkg.UUID, list[ImportNote]]
    found_about: dict[uuid_pkg.UUID, list[Finding]]
    indexes: dict[uuid_pkg.UUID, int]
    inferred: frozenset[str]
    recordings: dict[uuid_pkg.UUID, dict[int, tuple[int, int]]]


@dataclass(slots=True)
class _Dives:
    """One row per dive the batch creates or touches, the most that happens to it winning."""

    rows: list[_DiveRow] = field(default_factory=list)
    _by_dive: dict[int, _DiveRow] = field(default_factory=dict)
    # Which document dives already have a row for their notes. A dive whose recordings partly
    # matched dives the caller has is its own row and theirs, and its notes go on the first:
    # the dive it became, rather than one it only added a recording to.
    _claimed: set[_Source] = field(default_factory=set)
    _files: dict[int, _File] = field(default_factory=dict)
    # The findings a row's dive no longer bears out, by identity: on no row and in no group.
    _left_out: set[int] = field(default_factory=set)

    def _claim(self, row: _DiveRow, source: _Source) -> None:
        if source not in self._claimed:
            self._claimed.add(source)
            row.sources.append(source)

    def _touch(self, dive_id: int, source: _Source, *, created: bool = False) -> None:
        member = source[0]
        row = self._by_dive.get(dive_id)
        if row is None:
            row = self._by_dive[dive_id] = _DiveRow(dive_id=dive_id, members=[])
            self.rows.append(row)
        row.created = row.created or created
        if member not in row.members:
            row.members.append(member)
        if source not in row.reached_by:
            row.reached_by.append(source)
        self._claim(row, source)

    def add(self, member: int, loaded: LoadedImport, plan: ImportPlan, written: WrittenImport) -> None:
        """What one file's plan and write did to dives, in the document's order."""
        found = findings(loaded.document, loaded.conversion)
        notes_about: dict[uuid_pkg.UUID, list[ImportNote]] = defaultdict(list)
        for note in plan.notes:
            # The remap names the uuid the first of the two records kept, not this one's.
            if (
                note.collection == "dives"
                and note.uuid is not None
                and note.code is not ImportNoteCode.RECORD_REMAPPED_REFERENCES_STAY
            ):
                notes_about[note.uuid].append(note)
        found_about: dict[uuid_pkg.UUID, list[Finding]] = defaultdict(list)
        for one in found:
            if one.owner is not None:
                found_about[one.owner].append(one)
        indexes: dict[uuid_pkg.UUID, int] = {}
        for index, dive in enumerate(loaded.document.dives):
            indexes.setdefault(dive.uuid, index)
        self._files[member] = _File(
            loaded=loaded,
            plan=plan,
            findings=found,
            notes_about=notes_about,
            found_about=found_about,
            indexes=indexes,
            inferred=inferred_paths(loaded.document),
            recordings=_placed_recordings(plan, written),
        )
        matched = {match.source_uuid for match in plan.recording_matches}
        documented = {dive.uuid: dive for dive in loaded.document.dives}
        for record in plan.records.get("dives", {}).values():
            source = (member, record.source_uuid)
            if record.action is Action.CREATE:
                self._touch(written.dive_ids[record.source_uuid], source, created=True)
            elif record.action is Action.LINK and record.row_id is not None:
                self._touch(record.row_id, source)
            elif record.action is Action.SKIP and record.source_uuid not in matched:
                row = _DiveRow(
                    dive_id=None,
                    members=[member],
                    reason=_reason(plan, record.source_uuid),
                    source=documented.get(record.source_uuid),
                    reached_by=[source],
                )
                self.rows.append(row)
                self._claim(row, source)
        for match in plan.recording_matches:
            self._touch(match.dive_id, (member, match.source_uuid))

    def _own_notes(self, source: _Source) -> list[ImportNote]:
        """The import's own notes about one document dive, in its plan's order."""
        member, uuid = source
        return self._files[member].notes_about.get(uuid, [])

    def _found(self, source: _Source) -> list[Finding]:
        member, uuid = source
        return self._files[member].found_about.get(uuid, [])

    def _dive_source(self, source: _Source) -> DiveSource:
        member, uuid = source
        file = self._files[member]
        index = file.indexes[uuid]
        return DiveSource(
            dive=file.loaded.document.dives[index],
            index=index,
            converted=file.loaded.conversion is not None,
            inferred=file.inferred,
            recordings=file.recordings.get(uuid, {}),
        )

    def _shown(self, row: _DiveRow, stored: StoredDive | None) -> list[tuple[list[object], ImportDiveNote]]:
        """Everything a row says, in the import's order, each line beside the notes it was made
        from: the import's own about its dives, then its files' findings the dive still bears out.

        A file's line that an earlier file of the row already gave is that line again: the
        n-th time one file says a sentence joins the n-th line saying it, if there is one."""
        shown: list[tuple[list[object], ImportDiveNote]] = []
        lines: dict[LineKey, list[int]] = defaultdict(list)
        said: Counter[tuple[_Source, LineKey]] = Counter()

        def say(source: _Source, note: object, line: ImportDiveNote) -> None:
            key = line_key(line)
            nth = said[source, key]
            said[source, key] += 1
            if nth < len(lines[key]):
                shown[lines[key][nth]][0].append(note)
            else:
                lines[key].append(len(shown))
                shown.append(([note], line))

        for source in row.sources:
            for note in self._own_notes(source):
                say(source, note, import_note(note))
        sources = {source: self._dive_source(source) for source in row.sources}
        for source in row.sources:
            dive = sources[source]
            others = [other for key, other in sources.items() if key != source]
            for found in self._found(source):
                if (
                    stored is not None
                    and row.dive_id is not None
                    and left_out(found.note, dive, others, row.dive_id, stored)
                ):
                    self._left_out.add(id(found.note))
                    continue
                say(source, found.note, finding_note(found))
        return shown

    async def reports(
        self, db: AsyncSession, details: _DiveDetails, *, serialise_new: bool
    ) -> tuple[list[BatchDive], list[ImportNoteGroup], int]:
        """The rows, each with its outcome and changes read off the dive as the dive read gives
        it, with its notes - and the groups of every note on no row, a row's overflow among them."""
        ids = [row.dive_id for row in self.rows if row.dive_id is not None]
        dives = (
            {
                one.id: one
                for one in await db.execute(
                    select(Dive.id, Dive.uuid, Dive.start_time, Dive.utc_offset_minutes, Dive.start_date_only).where(
                        Dive.id.in_(ids)
                    )
                )
            }
            if ids
            else {}
        )
        devices = (
            {
                one.dive_id: _stored_device(one)
                for one in await db.execute(
                    select(
                        DiveRecording.dive_id, *(getattr(DiveRecording, column) for column in DEVICE_COLUMNS.values())
                    ).where(DiveRecording.dive_id.in_(ids), DiveRecording.ordinal == 0)
                )
            }
            if ids
            else {}
        )
        compared = await stored_dives(
            db,
            [
                row.dive_id
                for row in self.rows
                if row.dive_id is not None and any(self._found(source) for source in row.sources)
            ],
        )

        on_rows: set[int] = set()
        reports = []
        for row in self.rows:
            shown = self._shown(row, None if row.dive_id is None else compared.get(row.dive_id))
            if row.dive_id is None:
                # The reason is said once, as the row's outcome, rather than again as its note.
                reason = [entry for entry in shown if entry[1].code == ImportNoteCode.RECORD_SKIPPED.value]
                on_rows.update(id(note) for notes, _ in reason for note in notes)
                shown = [entry for entry in shown if entry not in reason]
            listed = shown[:MAX_DIVE_NOTES]
            on_rows.update(id(note) for notes, _ in listed for note in notes)
            stored = None if row.dive_id is None else dives.get(row.dive_id)
            report = BatchDive(
                outcome=ImportDiveOutcome.SKIPPED,
                reason=row.reason,
                members=row.members,
                notes=[note for _, note in listed],
                notes_truncated=len(shown) - len(listed),
                sources=row.reached_by,
            )
            if stored is not None:
                report.reason = None
                report.uuid = stored.uuid
                report.start_time = combine_dive_start_time(
                    stored.start_time, stored.utc_offset_minutes, stored.start_date_only
                )
                report.device = devices.get(stored.id)
                await _settle_outcome(report, row, stored.uuid, details, serialise_new=serialise_new)
            elif row.source is not None:
                report.start_time = row.source.started_at
                report.device = _documented_device(row.source)
            reports.append(report)
        reports.sort(key=_logbook_order, reverse=True)
        groups, truncated = grouped(self._unlisted(on_rows | self._left_out))
        return reports, groups, truncated

    def _unlisted(self, skipped: set[int]) -> Iterator[tuple[ImportDiveNote, str | None]]:
        """Every note on no row, in the batch's order: each file's own notes, then its findings."""
        for file in self._files.values():
            for note in file.plan.notes:
                if id(note) not in skipped:
                    yield import_note(note), None
            for found in file.findings:
                if id(found.note) not in skipped:
                    yield finding_note(found), found.note.where


def _placed_recordings(plan: ImportPlan, written: WrittenImport) -> dict[uuid_pkg.UUID, dict[int, tuple[int, int]]]:
    """Where each document dive's recordings landed, by the dive's uuid and the recording's
    index in the document: a written dive's on it at their planned ordinals, a matched one on
    the dive it matched at the ordinal the write reports."""
    placed: dict[uuid_pkg.UUID, dict[int, tuple[int, int]]] = defaultdict(dict)
    for record in plan.writable("dives"):
        dive_id = written.dive_ids[record.source_uuid]
        for recording in record.children.get("recordings") or []:
            if recording.source_index is not None:
                placed[record.source_uuid][recording.source_index] = (dive_id, recording.ordinal)
    for match, ordinal in zip(plan.recording_matches, written.match_ordinals, strict=True):
        if match.recording.source_index is not None:
            placed[match.source_uuid][match.recording.source_index] = (match.dive_id, ordinal)
    return placed


async def _settle_outcome(
    report: BatchDive, row: _DiveRow, uuid: uuid_pkg.UUID, details: _DiveDetails, *, serialise_new: bool
) -> None:
    """A stored row's outcome: new where the pass created the dive, and otherwise what differs
    between the dive before the pass and after it."""
    if row.created:
        report.outcome = ImportDiveOutcome.NEW
        if serialise_new:
            report.dive = await details.read(uuid)
        return
    report.outcome = ImportDiveOutcome.UNCHANGED
    before = details.before.get(uuid)
    if before is None:
        return
    after = await details.read(uuid)
    report.changes = dive_changes(before, after)
    if report.changes:
        report.outcome = ImportDiveOutcome.MODIFIED
        report.dive = after


def _logbook_order(row: BatchDive) -> tuple[bool, datetime]:
    """Where a row's dive sits in the logbook's newest-first list: by the value its start is
    stored as, so a start with no offset sorts as its wall clock read as UTC and a bare date as
    its midnight. Rows with no start go last, and `reverse` keeps ties in the import's order."""
    if row.start_time is None:
        return False, datetime.min.replace(tzinfo=UTC)
    return True, split_dive_start_time(row.start_time)[0]


def _stored_device(row: Any) -> RecordingDevice | None:
    members = {member: getattr(row, column) for member, column in DEVICE_COLUMNS.items()}
    return None if all(value is None for value in members.values()) else RecordingDevice(**members)


def _documented_device(dive: ImportDive) -> RecordingDevice | None:
    device: ImportDevice | None = dive.recordings[0].device if dive.recordings else None
    if device is None:
        return None
    members = device.model_dump(include=set(DEVICE_COLUMNS))
    return None if all(value is None for value in members.values()) else RecordingDevice(**members)


@dataclass(slots=True)
class BatchReport:
    """Everything the pass has to say, over the whole batch."""

    collections: list[ImportCollectionReport]
    files: ImportFileReport
    notes: list[ImportNoteGroup]
    notes_truncated: int
    members: list[ImportMemberReport]
    dives: list[BatchDive]
    check_in_details: list[ImportCheckInDetail]
    # The archive's portrait beside the account's, on the upload's pass.
    portrait: ImportPortraitOffer | None
    archive: bool
    # What this pass minted, by `file_key`, for the apply's to take again.
    minted: dict[str, MintedUuids]


def file_key(name: str, sha256: str) -> str:
    """One file across the two passes. Never its place in the batch: a refused file holds a row
    in the batch the upload reads and none in the batch the apply reads from what was stored."""
    return f"{sha256}/{name}"


def batch_species(batch: LoadedBatch) -> list[ImportSpecies]:
    """Every species the batch's documents carry, for the catalog's pre-pass."""
    return [species for loaded in batch.documents for species in loaded.document.species]


def _collections(plans: Sequence[ImportPlan]) -> list[ImportCollectionReport]:
    """Each collection's counts summed over the batch's files, each counted as the import of
    it alone would count it, after the files before it."""
    totals = {name: [0, 0, 0] for name in COLLECTIONS}
    for plan in plans:
        for report in plan.collection_reports():
            counts = totals[report.collection]
            counts[0] += report.created
            counts[1] += report.linked
            counts[2] += report.skipped
    return [
        ImportCollectionReport(collection=name, created=c, linked=li, skipped=s) for name, (c, li, s) in totals.items()
    ]


def _files(plans: Sequence[ImportPlan]) -> ImportFileReport:
    reports = [plan.file_report() for plan in plans]
    return ImportFileReport(
        referenced=sum(report.referenced for report in reports),
        restored=sum(report.restored for report in reports),
        not_contained=sum(report.not_contained for report in reports),
        skipped=sum(report.skipped for report in reports),
    )


def _check_in_details(plans: Sequence[ImportPlan]) -> list[ImportCheckInDetail]:
    """Each check-in detail the batch's documents propose, as the first of them proposes it."""
    seen: dict[str, ImportCheckInDetail] = {}
    for plan in plans:
        for detail in plan.check_in_details:
            seen.setdefault(detail.detail, detail)
    return list(seen.values())


async def import_batch(
    db: AsyncSession,
    *,
    user_id: int,
    batch: LoadedBatch,
    apply: bool,
    resolution_ran: bool = False,
    newly_resolved_aphia_ids: frozenset[int] = frozenset(),
    check_in: ImportCheckInSubmission | None = None,
    portrait: ImportPortraitChoice | None = None,
    gone_dives: Mapping[str, frozenset[uuid_pkg.UUID]] | None = None,
    minted: Mapping[str, Mapping[str, Mapping[str, list[str]]]] | None = None,
    serialise_new: bool | None = None,
) -> BatchReport:
    """Import every file of the batch in turn, and report the whole.

    **Without `apply` it writes inside a savepoint and rolls that back**, so whatever the caller
    holds in its transaction stands; with it, it writes and leaves the commit to the caller, who
    commits once. Either way, the files the batch stores are checked against the account's
    storage limit after the last file is planned and before any object is written, and an
    import that would cross it is refused whole with the 413 every other write gives.

    `resolution_ran` says the species pre-pass has run, so a species the catalog lacks is one
    the register could not answer for. `check_in` and `portrait` are what the diver confirmed in
    the review, handed to every file: a detail a file does not carry, or one already written,
    writes nothing, and only an archive carries a portrait. A writing pass - an apply, or an upload
    applied at once - spends the limit on linking people as it links them; the upload's review
    pass reads it. `gone_dives` and `minted` are the apply's, each by `file_key`: the document
    dives to skip, and what the upload's pass minted. `serialise_new` says whether a `new` row
    carries its dive: by default the upload's pass does and an apply's does not, its review
    already holding them.
    """
    # What the account holds before the batch writes a row, which is the figure the limit is
    # checked against: only an archive and a file kept as itself can store anything.
    stores = any(loaded.is_archive or loaded.kept is not None for loaded in batch.documents)
    used = (
        (await get_storage_usage(db, user_id=user_id)).used_bytes
        if stores and storage_limit_bytes() is not None
        else None
    )
    staged = StagedFiles()
    written_dives: set[int] = set()
    unnumbered: list[int] = []
    dives = _Dives()
    details = _DiveDetails(db, user_id)
    plans: list[ImportPlan] = []
    made: dict[str, MintedUuids] = {}
    retired = 0
    savepoint = None if apply else await db.begin_nested()
    try:
        for index, row in enumerate(batch.rows):
            if row.loaded is None:
                continue
            key = file_key(row.name, row.sha256)
            file_minted = made.setdefault(key, MintedUuids(earlier=(minted or {}).get(key, {})))
            plan = await plan_import(
                db,
                user_id=user_id,
                loaded=row.loaded,
                resolution_ran=resolution_ran,
                newly_resolved_aphia_ids=newly_resolved_aphia_ids,
                check_in=check_in,
                portrait=portrait,
                claim_links=apply,
                batch_dive_ids=frozenset(written_dives),
                held=staged.held,
                gone_dives=(gone_dives or {}).get(key, frozenset()),
                minted=file_minted,
            )
            for match in plan.recording_matches:
                if match.dive_id not in written_dives:
                    await details.note_before(match.dive_uuid)
            written = await write_import(db, user_id=user_id, loaded=row.loaded, plan=plan, staged=staged)
            written_dives.update(written.dive_ids.values())
            unnumbered.extend(written.dive_ids[source] for source in plan.unnumbered_dives)
            dives.add(index, row.loaded, plan, written)
            plans.append(plan)
            retired += portrait_change(plan)[1]

        # The one thing a batch does as a whole rather than file by file: its dives with no
        # number are numbered in date order across every file, as the dive form would number
        # them entered in that order, which a file planned on its own cannot see.
        await assign_suggested_numbers(db, user_id, unnumbered)
        await ensure_room(
            db, user_id=user_id, incoming=staged.ceiling(), retired=retired, exact=staged.exact, used=used
        )
        if apply:
            await staged.write(db)
        documents = batch.documents
        rows, notes, notes_truncated = await dives.reports(
            db, details, serialise_new=not apply if serialise_new is None else serialise_new
        )
        return BatchReport(
            collections=_collections(plans),
            files=_files(plans),
            notes=notes,
            notes_truncated=notes_truncated,
            members=_members(batch, plans),
            dives=rows,
            check_in_details=_check_in_details(plans),
            portrait=None if apply else await _portrait_offer(plans),
            archive=any(loaded.is_archive for loaded in documents),
            minted=made,
        )
    finally:
        if savepoint is not None:
            await savepoint.rollback()


async def _portrait_offer(plans: Sequence[ImportPlan]) -> ImportPortraitOffer | None:
    """The archive's portrait, as the review offers it - only an archive carries one."""
    for plan in plans:
        if (offer := await plan.portrait_offer()) is not None:
            return offer
    return None


def _members(batch: LoadedBatch, plans: Sequence[ImportPlan]) -> list[ImportMemberReport]:
    planned = iter(plans)
    reports = []
    for row in batch.rows:
        plan = next(planned) if row.loaded is not None else None
        reports.append(
            ImportMemberReport(
                part=row.part,
                container=row.container,
                name=row.name,
                byte_size=row.size,
                sha256=row.sha256,
                format=row.format,
                opened=row.opened,
                kept=plan is not None and plan.kept,
                not_kept=None if plan is None else plan.not_kept,
                refusal=None if row.refusal is None else str(row.refusal),
            )
        )
    return reports
