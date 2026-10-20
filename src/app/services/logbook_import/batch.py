"""An import of many files: each imported in turn, inside one transaction, with one report.

**A batch writes what its files would write imported one at a time, in the batch's order** -
each as an import of its own, previewed and applied - and that is the whole of what two files
make of each other but for dive numbers, given after the last file in date order
(`assign_suggested_numbers`). Each file is planned against the logbook as the files before it
left it, and written before the next is planned, so every rule the import has about a
recording the account stores applies unchanged to one an earlier file of the same batch
brought: the same-recording gate fills it, the strict gate attaches a second computer's to its
dive, and a file whose bytes already gave a dive its identity links. The FIT and the JSON of
one dive are one dive with one recording holding both files, whether they arrive together or
on two days, and the same batch twice writes nothing the second time.

**The preview runs the same pass and rolls it back**, which is what makes its counts the
apply's rather than a prediction of them: a later file's outcome depends on what the earlier
ones wrote, and nothing short of writing them says what that is. No object is written by a
preview, and none by an apply until the whole batch has fitted the account's storage limit
(`staging.py`).

`DECISIONS.md`, *"An import is its files imported one at a time"*, has the reasoning.
"""

import uuid as uuid_pkg
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.utils.datetime_offset import combine_dive_start_time
from ...models.dive import Dive
from ...models.dive_recording import DiveRecording
from ...schemas.dive import RecordingDevice
from ...schemas.logbook_import import (
    ImportCheckInDetail,
    ImportCheckInSubmission,
    ImportCollectionReport,
    ImportDevice,
    ImportDive,
    ImportDiveNote,
    ImportDiveOutcome,
    ImportDiveReport,
    ImportDocument,
    ImportFileReport,
    ImportMemberReport,
    ImportNote,
    ImportNoteCode,
    ImportNoteGroup,
    ImportPortraitChoice,
    ImportPortraitOffer,
    ImportSpecies,
)
from ..dive_numbering import assign_suggested_numbers
from ..dive_recordings import DEVICE_COLUMNS
from ..storage_usage import ensure_room, get_storage_usage, storage_limit_bytes
from .notes import (
    MAX_DIVE_NOTES,
    DiveSource,
    Finding,
    StoredDive,
    finding_note,
    findings,
    grouped,
    import_note,
    inferred_paths,
    left_out,
    stored_dives,
)
from .planner import COLLECTIONS, Action, ImportPlan, plan_import, portrait_change
from .reader import LoadedBatch, LoadedImport
from .staging import StagedFiles
from .writer import WrittenImport, write_import

# Which outcome of one dive says more, where several files reach it: created or restored
# over a file or a recording added, over a link, over a skip.
_RANK = {
    ImportDiveOutcome.SKIPPED: 0,
    ImportDiveOutcome.LINKED: 1,
    ImportDiveOutcome.UPDATED: 2,
    ImportDiveOutcome.CREATED: 3,
    ImportDiveOutcome.RESTORED: 3,
}


# A document dive, as the batch member it came from and the uuid that document gives it: two
# files' documents can give one uuid to two dives, and a stored dive's uuid can be neither.
type _Source = tuple[int, uuid_pkg.UUID]


@dataclass(slots=True)
class _DiveRow:
    outcome: ImportDiveOutcome
    dive_id: int | None
    members: list[int]
    files_added: int = 0
    recordings_added: int = 0
    reason: str | None = None
    # A skipped dive's own figures, where there is no stored dive to read them from.
    source: ImportDive | None = None
    # The document dives whose notes this row carries, each as (member, the document's uuid).
    sources: list[_Source] = field(default_factory=list)


def _reason(plan: ImportPlan, source_uuid: Any, codes: Iterable[ImportNoteCode]) -> str | None:
    """The sentence a plan noted about one document dive, under one of `codes`."""
    wanted = set(codes)
    return next(
        (note.message for note in plan.notes if note.code in wanted and note.uuid == source_uuid),
        None,
    )


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

    def _touch(
        self,
        dive_id: int,
        source: _Source,
        outcome: ImportDiveOutcome,
        *,
        files: int = 0,
        recordings: int = 0,
        reason: str | None = None,
    ) -> None:
        member = source[0]
        row = self._by_dive.get(dive_id)
        if row is None:
            row = self._by_dive[dive_id] = _DiveRow(outcome=outcome, dive_id=dive_id, members=[], reason=reason)
            self.rows.append(row)
        elif _RANK[outcome] > _RANK[row.outcome]:
            row.outcome, row.reason = outcome, reason
        if member not in row.members:
            row.members.append(member)
        row.files_added += files
        row.recordings_added += recordings
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
                self._touch(written.dive_ids[record.source_uuid], source, ImportDiveOutcome.CREATED)
            elif record.action is Action.RESTORE:
                self._touch(written.dive_ids[record.source_uuid], source, ImportDiveOutcome.RESTORED)
            elif record.action is Action.LINK and record.row_id is not None:
                self._touch(record.row_id, source, ImportDiveOutcome.LINKED)
            elif record.action is Action.SKIP and record.source_uuid not in matched:
                row = _DiveRow(
                    outcome=ImportDiveOutcome.SKIPPED,
                    dive_id=None,
                    members=[member],
                    reason=_reason(plan, record.source_uuid, [ImportNoteCode.RECORD_SKIPPED]),
                    source=documented.get(record.source_uuid),
                )
                self.rows.append(row)
                self._claim(row, source)
        for match, files in zip(plan.recording_matches, written.match_files, strict=True):
            recordings = 1 if match.kind == "attach" else 0
            source = (member, match.source_uuid)
            if files or recordings:
                self._touch(match.dive_id, source, ImportDiveOutcome.UPDATED, files=files, recordings=recordings)
            else:
                codes = [ImportNoteCode.RECORDING_FILLED, ImportNoteCode.RECORDING_ATTACHED]
                self._touch(
                    match.dive_id, source, ImportDiveOutcome.SKIPPED, reason=_reason(plan, match.source_uuid, codes)
                )

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

    def _shown(self, row: _DiveRow, stored: StoredDive | None) -> list[tuple[object, ImportDiveNote]]:
        """Everything a row says, in the import's order, each beside the note it was made from:
        the import's own about its dives, then its files' findings the dive still bears out."""
        shown: list[tuple[object, ImportDiveNote]] = [
            (note, import_note(note)) for source in row.sources for note in self._own_notes(source)
        ]
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
                shown.append((found.note, finding_note(found)))
        return shown

    async def reports(self, db: AsyncSession) -> tuple[list[ImportDiveReport], list[ImportNoteGroup], int]:
        """The rows, each read off the dive as the dive read gives it, with its notes - and the
        groups of every note on no row, a row's overflow among them."""
        ids = [row.dive_id for row in self.rows if row.dive_id is not None]
        dives = (
            {
                one.id: one
                for one in await db.execute(
                    select(
                        Dive.id,
                        Dive.uuid,
                        Dive.start_time,
                        Dive.utc_offset_minutes,
                        Dive.start_date_only,
                        Dive.submerged_time,
                        Dive.max_depth,
                    ).where(Dive.id.in_(ids))
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
            listed = shown[:MAX_DIVE_NOTES]
            on_rows.update(id(note) for note, _ in listed)
            updated = row.outcome is ImportDiveOutcome.UPDATED
            stored = None if row.dive_id is None else dives.get(row.dive_id)
            figures: dict[str, Any] = {}
            if stored is not None:
                figures = {
                    "uuid": stored.uuid,
                    "start_time": combine_dive_start_time(
                        stored.start_time, stored.utc_offset_minutes, stored.start_date_only
                    ),
                    "submerged_time": stored.submerged_time,
                    "max_depth": stored.max_depth,
                    "device": devices.get(stored.id),
                }
            elif row.source is not None:
                figures = {
                    "start_time": row.source.started_at,
                    "submerged_time": row.source.submerged_time,
                    "max_depth": row.source.max_depth,
                    "device": _documented_device(row.source),
                }
            reports.append(
                ImportDiveReport(
                    outcome=row.outcome,
                    files_added=row.files_added if updated else 0,
                    recordings_added=row.recordings_added if updated else 0,
                    reason=row.reason if row.outcome is ImportDiveOutcome.SKIPPED else None,
                    members=row.members,
                    notes=[note for _, note in listed],
                    notes_truncated=len(shown) - len(listed),
                    **figures,
                )
            )
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
    """Everything both routes answer with, over the whole batch."""

    collections: list[ImportCollectionReport]
    files: ImportFileReport
    notes: list[ImportNoteGroup]
    notes_truncated: int
    members: list[ImportMemberReport]
    dives: list[ImportDiveReport]
    check_in_details: list[ImportCheckInDetail]
    # The archive's portrait beside the account's, on a preview.
    portrait: ImportPortraitOffer | None
    first: ImportDocument
    archive: bool


def batch_species(batch: LoadedBatch) -> list[ImportSpecies]:
    """Every species the batch's documents carry, for the catalog's pre-pass."""
    return [species for loaded in batch.documents for species in loaded.document.species]


def _collections(plans: Sequence[ImportPlan]) -> list[ImportCollectionReport]:
    """Each collection's counts summed over the batch's files, each counted as the import of
    it alone would count it, after the files before it."""
    totals = {name: [0, 0, 0, 0] for name in COLLECTIONS}
    for plan in plans:
        for report in plan.collection_reports():
            counts = totals[report.collection]
            counts[0] += report.created
            counts[1] += report.linked
            counts[2] += report.restored
            counts[3] += report.skipped
    return [
        ImportCollectionReport(collection=name, created=c, linked=li, restored=r, skipped=s)
        for name, (c, li, r, s) in totals.items()
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
    newly_resolved_aphia_ids: frozenset[int] = frozenset(),
    check_in: ImportCheckInSubmission | None = None,
    portrait: ImportPortraitChoice | None = None,
) -> BatchReport:
    """Import every file of the batch in turn, and report the whole.

    **The preview writes and rolls back; the apply writes and leaves the commit to the
    caller**, who commits once. Either way, the files the batch stores are checked against
    the account's storage limit after the last file is planned and before any object is
    written, and an import that would cross it is refused whole with the 413 every other
    write gives.

    `check_in` and `portrait` are what the diver confirmed in the preview, handed to every
    file: a detail a file does not carry, or one already written, writes nothing, and only
    an archive carries a portrait. The apply spends the limit on linking people as it links
    them; the preview reads it.
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
    plans: list[ImportPlan] = []
    retired = 0
    try:
        for index, row in enumerate(batch.rows):
            if row.loaded is None:
                continue
            plan = await plan_import(
                db,
                user_id=user_id,
                loaded=row.loaded,
                resolution_ran=apply,
                newly_resolved_aphia_ids=newly_resolved_aphia_ids,
                check_in=check_in,
                portrait=portrait,
                claim_links=apply,
                batch_dive_ids=frozenset(written_dives),
            )
            written = await write_import(db, user_id=user_id, loaded=row.loaded, plan=plan, staged=staged)
            written_dives.update(written.dive_ids.values())
            unnumbered.extend(written.dive_ids[source] for source in plan.unnumbered_dives)
            dives.add(index, row.loaded, plan, written)
            plans.append(plan)
            retired += portrait_change(plan)[1] + written.retired_bytes

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
        rows, notes, notes_truncated = await dives.reports(db)
        return BatchReport(
            collections=_collections(plans),
            files=_files(plans),
            notes=notes,
            notes_truncated=notes_truncated,
            members=_members(batch, plans),
            dives=rows,
            check_in_details=_check_in_details(plans),
            portrait=None if apply else await _portrait_offer(plans),
            first=documents[0].document,
            archive=any(loaded.is_archive for loaded in documents),
        )
    finally:
        if not apply:
            await db.rollback()


async def _portrait_offer(plans: Sequence[ImportPlan]) -> ImportPortraitOffer | None:
    """The archive's portrait, as the preview offers it - only an archive carries one."""
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
