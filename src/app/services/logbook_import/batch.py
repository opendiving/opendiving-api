"""An import of many files: each imported in turn, inside one transaction, with one report.

**A batch writes what its files would write imported one at a time, in the batch's order** -
each as an import of its own, previewed and applied - and that is the whole of what two files
make of each other. Each file is planned against the logbook as the files before it left it,
and written before the next is planned, so every rule the import has about a recording the
account stores applies unchanged to one an earlier file of the same batch brought: the
same-recording gate fills it, the strict gate attaches a second computer's to its dive, and
a file whose bytes already gave a dive its identity links. The FIT and the JSON of one dive
are one dive with one recording holding both files, whether they arrive together or on two
days, and the same batch twice writes nothing the second time.

**The preview runs the same pass and rolls it back**, which is what makes its counts the
apply's rather than a prediction of them: a later file's outcome depends on what the earlier
ones wrote, and nothing short of writing them says what that is. No object is written by a
preview, and none by an apply until the whole batch has fitted the account's storage limit
(`staging.py`).

`DECISIONS.md`, *"An import is its files imported one at a time"*, has the reasoning.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.utils.datetime_offset import combine_dive_start_time
from ...models.dive import Dive
from ...models.dive_recording import DiveRecording
from ...schemas.dive import RecordingDevice
from ...schemas.logbook_import import (
    ConversionReport,
    ImportCheckInDetail,
    ImportCheckInSubmission,
    ImportCollectionReport,
    ImportDevice,
    ImportDive,
    ImportDiveOutcome,
    ImportDiveReport,
    ImportDocument,
    ImportFileReport,
    ImportMemberReport,
    ImportNote,
    ImportNoteCode,
    ImportPortraitChoice,
    ImportSpecies,
)
from ..dive_recordings import DEVICE_COLUMNS
from ..storage_usage import ensure_room, get_storage_usage, storage_limit_bytes
from .planner import COLLECTIONS, MAX_NOTES, Action, ImportPlan, plan_import, portrait_change
from .reader import LoadedBatch, LoadedImport, conversion_report
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


def _reason(plan: ImportPlan, source_uuid: Any, codes: Iterable[ImportNoteCode]) -> str | None:
    """The sentence a plan noted about one document dive, under one of `codes`."""
    wanted = set(codes)
    return next(
        (note.message for note in plan.notes if note.code in wanted and note.uuid == source_uuid),
        None,
    )


@dataclass(slots=True)
class _Dives:
    """One row per dive the batch creates or touches, the most that happens to it winning."""

    rows: list[_DiveRow] = field(default_factory=list)
    _by_dive: dict[int, _DiveRow] = field(default_factory=dict)

    def _touch(
        self,
        dive_id: int,
        member: int,
        outcome: ImportDiveOutcome,
        *,
        files: int = 0,
        recordings: int = 0,
        reason: str | None = None,
    ) -> None:
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

    def add(self, member: int, loaded: LoadedImport, plan: ImportPlan, written: WrittenImport) -> None:
        """What one file's plan and write did to dives, in the document's order."""
        matched = {match.source_uuid for match in plan.recording_matches}
        documented = {dive.uuid: dive for dive in loaded.document.dives}
        for record in plan.records.get("dives", {}).values():
            if record.action is Action.CREATE:
                self._touch(written.dive_ids[record.source_uuid], member, ImportDiveOutcome.CREATED)
            elif record.action is Action.RESTORE:
                self._touch(written.dive_ids[record.source_uuid], member, ImportDiveOutcome.RESTORED)
            elif record.action is Action.LINK and record.row_id is not None:
                self._touch(record.row_id, member, ImportDiveOutcome.LINKED)
            elif record.action is Action.SKIP and record.source_uuid not in matched:
                self.rows.append(
                    _DiveRow(
                        outcome=ImportDiveOutcome.SKIPPED,
                        dive_id=None,
                        members=[member],
                        reason=_reason(plan, record.source_uuid, [ImportNoteCode.RECORD_SKIPPED]),
                        source=documented.get(record.source_uuid),
                    )
                )
        for match, files in zip(plan.recording_matches, written.match_files, strict=True):
            recordings = 1 if match.kind == "attach" else 0
            if files or recordings:
                self._touch(match.dive_id, member, ImportDiveOutcome.UPDATED, files=files, recordings=recordings)
            else:
                codes = [ImportNoteCode.RECORDING_FILLED, ImportNoteCode.RECORDING_ATTACHED]
                self._touch(
                    match.dive_id, member, ImportDiveOutcome.SKIPPED, reason=_reason(plan, match.source_uuid, codes)
                )

    async def reports(self, db: AsyncSession) -> list[ImportDiveReport]:
        """The rows, each read off the dive as the dive read gives it."""
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
                        Dive.duration,
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

        reports = []
        for row in self.rows:
            updated = row.outcome is ImportDiveOutcome.UPDATED
            stored = None if row.dive_id is None else dives.get(row.dive_id)
            figures: dict[str, Any] = {}
            if stored is not None:
                figures = {
                    "uuid": stored.uuid,
                    "start_time": combine_dive_start_time(
                        stored.start_time, stored.utc_offset_minutes, stored.start_date_only
                    ),
                    "duration": stored.duration,
                    "max_depth": stored.max_depth,
                    "device": devices.get(stored.id),
                }
            elif row.source is not None:
                figures = {
                    "start_time": row.source.started_at,
                    "duration": row.source.duration,
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
                    **figures,
                )
            )
        return reports


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
    notes: list[ImportNote]
    notes_truncated: int
    conversion: ConversionReport | None
    members: list[ImportMemberReport]
    dives: list[ImportDiveReport]
    check_in_details: list[ImportCheckInDetail]
    # The plan carrying the archive's portrait, for the preview's offer.
    portrait_plan: ImportPlan | None
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


def _notes(plans: Sequence[ImportPlan]) -> tuple[list[ImportNote], int]:
    """Every file's notes in the batch's order, under the one cap a report has."""
    every = [note for plan in plans for note in plan.notes]
    dropped = sum(plan.notes_dropped for plan in plans)
    return every[:MAX_NOTES], dropped + max(len(every) - MAX_NOTES, 0)


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
    limit = storage_limit_bytes()
    used = None if limit is None else (await get_storage_usage(db, user_id=user_id)).used_bytes
    staged = StagedFiles()
    written_dives: set[int] = set()
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
            dives.add(index, row.loaded, plan, written)
            plans.append(plan)
            retired += portrait_change(plan)[1]

        await ensure_room(
            db, user_id=user_id, incoming=staged.ceiling(), retired=retired, exact=staged.exact, used=used
        )
        if apply:
            await staged.write(db)
        notes, notes_truncated = _notes(plans)
        documents = batch.documents
        return BatchReport(
            collections=_collections(plans),
            files=_files(plans),
            notes=notes,
            notes_truncated=notes_truncated,
            conversion=conversion_report(documents),
            members=_members(batch, plans),
            dives=await dives.reports(db),
            check_in_details=_check_in_details(plans),
            portrait_plan=next((plan for plan in plans if plan.portrait is not None), None),
            first=documents[0].document,
            archive=any(loaded.is_archive for loaded in documents),
        )
    finally:
        if not apply:
            await db.rollback()


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
