"""An import the server holds, from the upload to the end of its history.

**The files travel once.** `POST /imports` keeps the readable files in the blob store and the
review on a `logbook_import` row, one pending per account, so any device can read it and finish
it. The apply sends no bytes: it reads the held files back and runs the same pass. An upload
applied at once is that pair in one request: one pass that writes, stored as the applied import the
pair would leave, holding no file and never taking or reading the pending slot.

**An account's passes take turns.** The upload's review pass, the apply's and the one of an upload
applied at once each run under one lock of the account, held to the end of the pass's transaction:
the review pass writes the same rows before it rolls them back, so without it two passes carrying
one new dive race on its uuid.

**The review is the contract.** It is what the pass reported, stored as JSON and served as stored,
never validated against the current schemas - so a row written by an earlier build still answers,
and a later change to anything it embeds owes `REVIEW_SHAPE` a bump and a call on the stored rows.
The apply holds to it: the document dives whose reviewed dive has gone are skipped, the uuids the
first pass minted are minted again, and each modified dive is reconciled to its reviewed values
(`reconcile.py`).

**The slot is checked before the body and claimed last**, under a partial unique index, once the
review is made and the files are in the store: an upload racing another's does that work too and
is then refused, rather than an empty pending import blocking every other device for as long as
a body takes to arrive.

**No connection is held across the held files.** One may be the whole request bound, so the upload
writes them between the pass and the claim, and the apply reads and converts them before it takes
the row's lock. What the apply stores on dives it writes inside its transaction, as any import has.
"""

import base64
import hashlib
import uuid as uuid_pkg
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from tempfile import SpooledTemporaryFile, TemporaryFile
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from ...core.config import settings
from ...core.db.database import release_read_transaction
from ...models.dive import Dive
from ...models.logbook_import import LogbookImport, LogbookImportFile
from ...models.user import User
from ...schemas.dive import DiveNumberingSummary
from ...schemas.logbook_import import (
    REVIEW_SHAPE,
    ImportCheckInSubmission,
    ImportDiveNote,
    ImportDiveOutcome,
    ImportNoteCode,
    ImportPortraitChoice,
    LogbookImportStatus,
)
from .. import blob_store
from ..dive_detail import load_dive_detail
from ..dive_numbering import summarize_numbering
from ..dive_stats import recalculate_dive_stats
from ..export.archive import SPOOL_THRESHOLD
from ..gear_stats import recalculate_gear_dive_counts
from ..user_pictures import RENDITION_CONTENT_TYPE
from .batch import BatchDive, BatchReport, file_key, import_batch
from .parts import ImportPart, ImportRequest
from .planner import DELETED_HERE
from .reader import BatchRow, LoadedBatch
from .reconcile import hold_reviewed_values, review_note, unlanded

KEY_KIND = "import-files"

ROLE_PART = "part"
ROLE_PORTRAIT = "portrait"

_OVERTAKEN = "This dive reached your logbook another way after the review, so the import did not create it."
_UNSHOWN = "The review did not show this dive, because your logbook changed after it."
_UNREAD_SINCE = "This dive's file could no longer be read when the import was applied, so nothing was written for it."


# The namespace of the per-account lock `lock_account_imports` takes, keyed `(this, user_id)`, beside
# the other two-integer advisory locks' (`crud_invitations`, `crud_authentication_requests`).
_ACCOUNT_IMPORT_LOCK = 0x0D1E_0004


async def lock_account_imports(db: AsyncSession, *, user_id: int) -> None:
    """Wait for any other import pass of the account, and hold its turn until this transaction
    ends. Taken before anything the pass reads, so it plans against what the other wrote."""
    await db.execute(select(func.pg_advisory_xact_lock(_ACCOUNT_IMPORT_LOCK, user_id)))


def _is_live(now: datetime) -> ColumnElement[bool]:
    return or_(LogbookImport.expires_at.is_(None), LogbookImport.expires_at >= now)


def _past_expiry(expires_at: datetime | None) -> bool:
    return expires_at is not None and expires_at < datetime.now(UTC)


def _pending_conflict(uuid: uuid_pkg.UUID) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f"Import {uuid} is waiting to be finished. Apply it or discard it before uploading another.",
    )


def _overtaken_conflict() -> HTTPException:
    return HTTPException(
        status_code=409, detail="Another import of yours finished while this one was uploading. Upload it again."
    )


async def _file_keys(db: AsyncSession, import_id: int) -> list[str]:
    rows = await db.execute(
        select(LogbookImportFile.storage_key).where(LogbookImportFile.logbook_import_id == import_id)
    )
    return list(rows.scalars())


async def _delete_import(db: AsyncSession, import_id: int, *still: ColumnElement[bool]) -> None:
    """Delete one import with its file rows, its files after the commit. A row that no longer
    meets `still` when the `DELETE` reaches it stays, and so do its files."""
    keys = await _file_keys(db, import_id)
    went = await db.execute(
        delete(LogbookImport).where(LogbookImport.id == import_id, *still).returning(LogbookImport.id)
    )
    if went.first() is not None:
        blob_store.delete_after_commit(db, keys)


async def refuse_while_pending(db: AsyncSession, *, user_id: int) -> None:
    """Refuse an upload while the account has a pending import, and clear one past its lifetime.

    Before the body, with its commit: the transaction it opens is released before a byte is read.
    """
    pending = (
        await db.execute(
            select(LogbookImport.id, LogbookImport.uuid, LogbookImport.expires_at).where(
                LogbookImport.user_id == user_id, LogbookImport.status == LogbookImportStatus.PENDING.value
            )
        )
    ).first()
    if pending is None:
        return
    if not _past_expiry(pending.expires_at):
        raise _pending_conflict(pending.uuid)
    # An apply that took the row in its last moments may commit before this reaches it, and then
    # the row is history, and the slot is free anyway.
    await _delete_import(
        db,
        pending.id,
        LogbookImport.status == LogbookImportStatus.PENDING.value,
        LogbookImport.expires_at < datetime.now(UTC),
    )
    await db.commit()


def _pending_expiry(now: datetime) -> datetime | None:
    hours = settings.IMPORT_PENDING_LIFETIME_HOURS
    return None if hours is None else now + timedelta(hours=hours)


def _history_expiry(now: datetime) -> datetime | None:
    days = settings.IMPORT_HISTORY_RETENTION_DAYS
    return None if days is None else now + timedelta(days=days)


def _counts(dives: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for dive in dives:
        counts[dive["outcome"]] = counts.get(dive["outcome"], 0) + 1
    return counts


def _row_notes(dive: dict[str, Any], notes: list[ImportDiveNote], truncated: int) -> dict[str, Any]:
    return {
        "uuid": dive["uuid"],
        "start_time": dive["start_time"],
        "device": dive["device"],
        "outcome": dive["outcome"],
        "reason": dive["reason"],
        "notes": [note.model_dump(mode="json") for note in notes],
        "notes_truncated": truncated,
    }


def _wire_dive(row: BatchDive) -> dict[str, Any]:
    return {
        "uuid": None if row.uuid is None else str(row.uuid),
        "outcome": row.outcome.value,
        "dive": row.dive,
        "changes": row.changes,
        "start_time": jsonable_encoder(row.start_time),
        "device": None if row.device is None else row.device.model_dump(mode="json"),
        "members": row.members,
        "reason": row.reason,
    }


def _notes_of(report: BatchReport, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "notes": [group.model_dump(mode="json") for group in report.notes],
        "notes_truncated": report.notes_truncated,
        "dives": rows,
    }


def _sources(batch: LoadedBatch, row: BatchDive) -> list[dict[str, str]]:
    """A row's document dives, each file named so the apply finds it in the batch it reads."""
    named = []
    for index, uuid in row.sources:
        file = batch.rows[index]
        named.append({"file": file_key(file.name, file.sha256), "dive": str(uuid)})
    return named


async def _hold_file(row: BatchRow, parts: Sequence[ImportPart]) -> dict[str, Any]:
    """One readable file into the store, from a spool: `PutObject` reads the length off what it
    is handed, so a zip's member goes onto one first. Answers what its row records."""
    key = blob_store.new_key(KEY_KIND, sha256=row.sha256)
    if row.in_zip:
        with TemporaryFile() as spool:
            await run_in_threadpool(row.copy_member_to, spool)
            await blob_store.put_file(key, spool)
    else:
        source = parts[row.part].spool
        assert source is not None
        source.seek(0)
        await blob_store.put_file(key, source)
    return {
        "role": ROLE_PART,
        "part": row.part,
        "filename": row.name,
        "byte_size": row.size,
        "sha256": row.sha256,
        "storage_key": key,
    }


async def _hold_portrait(data_url: str) -> dict[str, Any]:
    """The offered picture in the store rather than the column, inlined again when it is read."""
    data = base64.b64decode(data_url.partition(",")[2])
    digest = hashlib.sha256(data).hexdigest()
    key = blob_store.new_key(KEY_KIND, sha256=digest)
    await blob_store.put(key, data)
    return {
        "role": ROLE_PORTRAIT,
        "filename": "portrait",
        "byte_size": len(data),
        "sha256": digest,
        "storage_key": key,
    }


def _review_of(report: BatchReport, numbering: DiveNumberingSummary, dives: list[dict[str, Any]]) -> dict[str, Any]:
    offer = report.portrait
    return {
        "archive": report.archive,
        "members": [member.model_dump(mode="json") for member in report.members],
        "collections": [collection.model_dump(mode="json") for collection in report.collections],
        "files": report.files.model_dump(mode="json"),
        "dives": dives,
        "check_in_details": [detail.model_dump(mode="json") for detail in report.check_in_details],
        "portrait": None if offer is None else {"account_sha256": offer.account_sha256},
        "numbering": numbering.model_dump(mode="json"),
    }


def _minted_of(report: BatchReport) -> dict[str, Any]:
    return {key: minted.minted for key, minted in report.minted.items() if minted.minted}


async def hold_upload(
    db: AsyncSession,
    *,
    user_id: int,
    parts: Sequence[ImportPart],
    batch: LoadedBatch,
    newly_resolved_aphia_ids: frozenset[int],
) -> LogbookImport:
    """Review the batch, hold its readable files and claim the account's slot. Leaves the commit
    to the caller, which the files are written ahead of.

    The claim comes last so that no transaction stays open across the store, and an upload that
    loses it to another takes back the files it wrote: no row ever named them.
    """
    await lock_account_imports(db, user_id=user_id)
    numbering = await summarize_numbering(db=db, user_id=user_id)
    report = await import_batch(
        db,
        user_id=user_id,
        batch=batch,
        apply=False,
        resolution_ran=True,
        newly_resolved_aphia_ids=newly_resolved_aphia_ids,
    )
    await release_read_transaction(db)
    # Only what the app could read: a zip's own row reads into nothing, its readable members
    # being rows of their own, which is what keeps it and its unreadable ones off the store.
    files = [await _hold_file(row, parts) for row in batch.read]
    if report.portrait is not None:
        files.append(await _hold_portrait(report.portrait.proposed))

    dives = [_wire_dive(row) for row in report.dives]
    now = datetime.now(UTC)
    held = LogbookImport(
        user_id=user_id,
        review_shape=REVIEW_SHAPE,
        created_at=now,
        expires_at=_pending_expiry(now),
        review=_review_of(report, numbering, dives),
        notes=_notes_of(
            report,
            [_row_notes(dive, row.notes, row.notes_truncated) for dive, row in zip(dives, report.dives, strict=True)],
        ),
        counts=_counts(dives),
        sources=[_sources(batch, row) for row in report.dives],
        remaps={"files": _minted_of(report), "species": sorted(newly_resolved_aphia_ids)},
    )
    db.add(held)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        for file in files:
            await blob_store.delete(file["storage_key"])
        winner = (
            await db.execute(
                select(LogbookImport.uuid).where(
                    LogbookImport.user_id == user_id, LogbookImport.status == LogbookImportStatus.PENDING.value
                )
            )
        ).scalar_one_or_none()
        # The winner can finish while the files above are deleted, and then there is none to name.
        raise (_overtaken_conflict() if winner is None else _pending_conflict(winner)) from exc
    db.add_all(LogbookImportFile(logbook_import_id=held.id, **file) for file in files)
    return held


async def apply_upload(
    db: AsyncSession, *, user_id: int, batch: LoadedBatch, newly_resolved_aphia_ids: frozenset[int]
) -> LogbookImport:
    """Write the batch and store it as an applied import, with no review before it. Leaves the
    commit to the caller.

    What `hold_upload` then `apply_held` with no choices would leave: the pass the apply runs, its
    new dives serialised as the review's would have been, and the row the apply settles to, less
    what only the apply's second pass reads - the held files, the sources and what was minted.
    """
    await lock_account_imports(db, user_id=user_id)
    numbering = await summarize_numbering(db=db, user_id=user_id)
    report = await import_batch(
        db,
        user_id=user_id,
        batch=batch,
        apply=True,
        resolution_ran=True,
        newly_resolved_aphia_ids=newly_resolved_aphia_ids,
        serialise_new=True,
    )
    dives = [_wire_dive(row) for row in report.dives]
    now = datetime.now(UTC)
    applied = LogbookImport(
        user_id=user_id,
        review_shape=REVIEW_SHAPE,
        status=LogbookImportStatus.APPLIED.value,
        created_at=now,
        applied_at=now,
        expires_at=_history_expiry(now),
        review={**_review_of(report, numbering, dives), "check_in_details": [], "portrait": None},
        notes=_notes_of(
            report,
            [_row_notes(dive, row.notes, row.notes_truncated) for dive, row in zip(dives, report.dives, strict=True)],
        ),
        counts=_counts(dives),
    )
    db.add(applied)
    await db.flush()
    return applied


async def find_import(db: AsyncSession, *, user_id: int, uuid: uuid_pkg.UUID) -> LogbookImport | None:
    """One of the account's imports, or `None` for an absent, expired or someone else's one."""
    found = await db.execute(
        select(LogbookImport).where(
            LogbookImport.uuid == uuid, LogbookImport.user_id == user_id, _is_live(datetime.now(UTC))
        )
    )
    return found.scalar_one_or_none()


async def lock_import(db: AsyncSession, *, user_id: int, uuid: uuid_pkg.UUID) -> LogbookImport | None:
    """`find_import`, holding the row, so an apply and a discard from two devices run one after
    the other and the second finds what the first left."""
    found = await db.execute(
        select(LogbookImport)
        .where(LogbookImport.uuid == uuid, LogbookImport.user_id == user_id, _is_live(datetime.now(UTC)))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return found.scalar_one_or_none()


async def find_pending_import(db: AsyncSession, *, user_id: int) -> LogbookImport | None:
    found = await db.execute(
        select(LogbookImport).where(
            LogbookImport.user_id == user_id,
            LogbookImport.status == LogbookImportStatus.PENDING.value,
            _is_live(datetime.now(UTC)),
        )
    )
    return found.scalar_one_or_none()


async def list_applied(db: AsyncSession, *, user_id: int, offset: int, limit: int) -> tuple[list[Any], int]:
    """The account's history, newest first, and how long it is."""
    listed = (LogbookImport.user_id == user_id, LogbookImport.status == LogbookImportStatus.APPLIED.value)
    live = _is_live(datetime.now(UTC))
    rows = await db.execute(
        select(LogbookImport.uuid, LogbookImport.created_at, LogbookImport.applied_at, LogbookImport.counts)
        .where(*listed, live)
        .order_by(LogbookImport.applied_at.desc(), LogbookImport.id.desc())
        .offset(offset)
        .limit(limit)
    )
    total = await db.execute(select(func.count()).select_from(LogbookImport).where(*listed, live))
    return list(rows), int(total.scalar_one())


async def _portrait_url(db: AsyncSession, import_id: int) -> str | None:
    key = (
        await db.execute(
            select(LogbookImportFile.storage_key).where(
                LogbookImportFile.logbook_import_id == import_id, LogbookImportFile.role == ROLE_PORTRAIT
            )
        )
    ).scalar_one_or_none()
    if key is None:
        return None
    data = await blob_store.get(key)
    return f"data:{RENDITION_CONTENT_TYPE};base64,{base64.b64encode(data).decode('ascii')}"


async def import_body(db: AsyncSession, row: LogbookImport) -> dict[str, Any]:
    """The import as the routes answer it: what was stored, beside the row's own columns."""
    body = {
        "uuid": str(row.uuid),
        "status": row.status,
        "created_at": jsonable_encoder(row.created_at),
        "applied_at": jsonable_encoder(row.applied_at),
        "expires_at": jsonable_encoder(row.expires_at),
        **row.review,
    }
    offer = body["portrait"]
    if offer is not None:
        proposed = await _portrait_url(db, row.id)
        body["portrait"] = None if proposed is None else {**offer, "proposed": proposed}
    return body


async def held_files(db: AsyncSession, row: LogbookImport) -> list[Any]:
    """What `held_parts` reads, as plain rows, so it needs no session and none is held across it."""
    found = await db.execute(
        select(
            LogbookImportFile.part,
            LogbookImportFile.filename,
            LogbookImportFile.byte_size,
            LogbookImportFile.sha256,
            LogbookImportFile.storage_key,
        )
        .where(LogbookImportFile.logbook_import_id == row.id, LogbookImportFile.role == ROLE_PART)
        .order_by(LogbookImportFile.id)
    )
    return list(found)


async def held_parts(files: Sequence[Any]) -> ImportRequest:
    """The held files back on spools, as the parts reader builds them and under its one bound:
    their bytes in memory together never pass one `SPOOL_THRESHOLD`."""
    request = ImportRequest(parts=[])
    in_memory = 0
    try:
        for file in files:
            spool: Any = SpooledTemporaryFile(max_size=0)
            request.parts.append(
                ImportPart(
                    index=file.part or 0, filename=file.filename, size=file.byte_size, sha256=file.sha256, spool=spool
                )
            )
            if in_memory + file.byte_size > SPOOL_THRESHOLD:
                await run_in_threadpool(spool.rollover)
            else:
                in_memory += file.byte_size
            await blob_store.get_file(file.storage_key, spool)
            spool.seek(0)
    except BaseException:
        request.close()
        raise
    return request


def refuse_unless_pending(row: LogbookImport) -> None:
    if row.status != LogbookImportStatus.PENDING.value:
        raise HTTPException(status_code=409, detail="This import has already been applied.")


async def _live_dives(db: AsyncSession, user_id: int, uuids: Sequence[str]) -> set[str]:
    if not uuids:
        return set()
    rows = await db.execute(
        select(Dive.uuid).where(
            Dive.uuid.in_([uuid_pkg.UUID(uuid) for uuid in uuids]),
            Dive.user_id == user_id,
            Dive.is_deleted.is_(False),
        )
    )
    return {str(uuid) for uuid in rows.scalars()}


def _gone_sources(rows: Sequence[dict[str, Any]], gone: set[str]) -> dict[str, frozenset[uuid_pkg.UUID]]:
    by_file: dict[str, set[uuid_pkg.UUID]] = {}
    for row in rows:
        if row["dive"]["uuid"] in gone:
            for source in row["sources"]:
                by_file.setdefault(source["file"], set()).add(uuid_pkg.UUID(source["dive"]))
    return {file: frozenset(dives) for file, dives in by_file.items()}


@dataclass(slots=True)
class _SecondPass:
    """The apply's rows, found from the review's: by the dive, and else by the document dives
    that reached it, which are the same on both passes."""

    by_uuid: dict[str, BatchDive]
    by_source: dict[tuple[str, str], BatchDive]
    taken: set[int]

    @classmethod
    def of(cls, report: BatchReport, batch: LoadedBatch) -> _SecondPass:
        by_source: dict[tuple[str, str], BatchDive] = {}
        for row in report.dives:
            for source in _sources(batch, row):
                by_source.setdefault((source["file"], source["dive"]), row)
        return cls(
            by_uuid={str(row.uuid): row for row in report.dives if row.uuid is not None},
            by_source=by_source,
            taken=set(),
        )

    def take(self, uuid: str | None, sources: list[dict[str, str]]) -> BatchDive | None:
        found = None if uuid is None else self.by_uuid.get(uuid)
        if found is None:
            shared = (self.by_source.get((source["file"], source["dive"])) for source in sources)
            # A skip stands for every row its document dive reached, so a dive it partly made and
            # a gone dive it partly matched both read it.
            found = next(
                (row for row in shared if row is not None and (row.uuid is None or id(row) not in self.taken)), None
            )
        if found is not None:
            self.taken.add(id(found))
        # Each file of a gone dive skips on its own, and they are all this one row.
        for source in sources:
            skipped = self.by_source.get((source["file"], source["dive"]))
            if skipped is not None and skipped.uuid is None:
                self.taken.add(id(skipped))
        return found


def _adopt(dive: dict[str, Any], settled: BatchDive) -> None:
    """A row takes what the second pass did to it, keeping the dive the review showed wherever
    the row is still one a card draws."""
    dive["outcome"] = settled.outcome.value
    dive["uuid"] = dive["uuid"] if settled.uuid is None else str(settled.uuid)
    dive["changes"] = settled.changes
    dive["reason"] = settled.reason
    if settled.outcome is ImportDiveOutcome.UNCHANGED:
        dive["dive"] = None
    elif settled.dive is not None:
        dive["dive"] = settled.dive


def _skip_row(dive: dict[str, Any], reason: str) -> None:
    dive["outcome"] = ImportDiveOutcome.SKIPPED.value
    dive["changes"] = []
    dive["reason"] = reason


def _unshown_note() -> ImportDiveNote:
    return ImportDiveNote(code=ImportNoteCode.REVIEW_UNSHOWN, kind=None, collection="dives", message=_UNSHOWN)


def _overtaken_note() -> ImportDiveNote:
    return ImportDiveNote(code=ImportNoteCode.REVIEW_OVERTAKEN, kind=None, collection="dives", message=_OVERTAKEN)


class _Application:
    """One apply, settling the review's rows against what its pass did."""

    def __init__(self, db: AsyncSession, *, user_id: int, owner_uuid: uuid_pkg.UUID) -> None:
        self._db = db
        self._user_id = user_id
        self._owner_uuid = owner_uuid
        self.wrote = False

    async def _read(self, uuid: str) -> dict[str, Any]:
        detail = await load_dive_detail(
            self._db, user_id=self._user_id, owner_uuid=self._owner_uuid, uuid=uuid_pkg.UUID(uuid)
        )
        return detail.model_dump(mode="json", by_alias=True)

    async def _reconcile(self, dive: dict[str, Any]) -> list[ImportDiveNote]:
        stored = await self._read(dive["uuid"])
        held = await hold_reviewed_values(self._db, reviewed=dive["dive"], changes=dive["changes"], stored=stored)
        if held.wrote:
            self.wrote = True
            stored = await self._read(dive["uuid"])
        return [review_note(sentence) for sentence in (*held.moved, *unlanded(held.others, stored))]

    async def settle(self, dive: dict[str, Any], settled: BatchDive | None, gone: set[str]) -> list[ImportDiveNote]:
        """Bring one reviewed row to what the apply did, answering the notes that adds."""
        reviewed = ImportDiveOutcome(dive["outcome"])
        if dive["uuid"] in gone:
            _skip_row(dive, DELETED_HERE)
            return []
        if reviewed is ImportDiveOutcome.MODIFIED:
            return await self._reconcile(dive)
        if settled is None:
            if reviewed is ImportDiveOutcome.NEW:
                _skip_row(dive, _UNREAD_SINCE)
            return []
        if settled.outcome is reviewed:
            return []
        _adopt(dive, settled)
        if settled.outcome is ImportDiveOutcome.NEW and dive["dive"] is None:
            dive["dive"] = await self._read(dive["uuid"])
        overtaken = reviewed is ImportDiveOutcome.NEW and settled.outcome is not ImportDiveOutcome.SKIPPED
        return [_overtaken_note()] if overtaken else []

    async def extra_row(self, row: BatchDive, members: list[int]) -> dict[str, Any]:
        """A dive the second pass reached that the review has no row for. `members` are its files
        in the review's `members`, which the apply's batch numbers differently."""
        dive = {**_wire_dive(row), "members": members}
        if row.outcome is ImportDiveOutcome.NEW and row.uuid is not None:
            dive["dive"] = await self._read(str(row.uuid))
        return dive


def _settle_members(review: dict[str, Any], report: BatchReport) -> None:
    second = {(member.name, member.sha256): member for member in report.members}
    for member in review["members"]:
        settled = second.get((member["name"], member["sha256"]))
        if settled is not None:
            member["kept"] = settled.kept
            member["not_kept"] = None if settled.not_kept is None else settled.not_kept.value


async def apply_held(
    db: AsyncSession,
    *,
    row: LogbookImport,
    user_id: int,
    batch: LoadedBatch,
    check_in: ImportCheckInSubmission | None,
    portrait: ImportPortraitChoice | None,
) -> None:
    """Write the import as reviewed, on the locked row. Leaves the commit to the caller."""
    await lock_account_imports(db, user_id=user_id)
    review = dict(row.review)
    rows = [dict(dive) for dive in review["dives"]]
    held = [
        {"dive": dive, "sources": sources}
        for dive, sources in zip(rows, row.sources, strict=True)
        if dive["uuid"] is not None and dive["outcome"] != ImportDiveOutcome.NEW.value
    ]
    uuids = [one["dive"]["uuid"] for one in held]
    gone = set(uuids) - await _live_dives(db, user_id, uuids)

    report = await import_batch(
        db,
        user_id=user_id,
        batch=batch,
        apply=True,
        resolution_ran=True,
        newly_resolved_aphia_ids=frozenset(row.remaps.get("species", ())),
        check_in=check_in,
        portrait=portrait,
        gone_dives=_gone_sources(held, gone),
        minted=row.remaps.get("files", {}),
    )

    owner_uuid = (await db.execute(select(User.uuid).where(User.id == user_id))).scalar_one()
    application = _Application(db, user_id=user_id, owner_uuid=owner_uuid)
    second = _SecondPass.of(report, batch)
    notes = []
    for dive, sources in zip(rows, row.sources, strict=True):
        # A modified dive still standing is found by its uuid alone: a file of its that lands
        # elsewhere now is a dive of its own, which the extra rows below show.
        standing = dive["outcome"] == ImportDiveOutcome.MODIFIED.value and dive["uuid"] not in gone
        settled = second.take(dive["uuid"], [] if standing else sources)
        added = await application.settle(dive, settled, gone)
        said = [] if settled is None else settled.notes
        truncated = 0 if settled is None else settled.notes_truncated
        notes.append(_row_notes(dive, [*said, *added], truncated))
    reviewed_files = {(member["name"], member["sha256"]): index for index, member in enumerate(review["members"])}
    for extra in report.dives:
        if id(extra) not in second.taken:
            files = (batch.rows[index] for index in extra.members)
            dive = await application.extra_row(extra, [reviewed_files[file.name, file.sha256] for file in files])
            rows.append(dive)
            notes.append(_row_notes(dive, [_unshown_note(), *extra.notes], extra.notes_truncated))

    if application.wrote:
        await recalculate_dive_stats(db, user_id=user_id, commit=False)
        await recalculate_gear_dive_counts(db, user_id=user_id, commit=False)

    _settle_members(review, report)
    now = datetime.now(UTC)
    row.review = {
        **review,
        "collections": [collection.model_dump(mode="json") for collection in report.collections],
        "files": report.files.model_dump(mode="json"),
        "dives": rows,
        "check_in_details": [],
        "portrait": None,
    }
    row.notes = _notes_of(report, notes)
    row.counts = _counts(rows)
    row.status = LogbookImportStatus.APPLIED.value
    row.applied_at = now
    row.expires_at = _history_expiry(now)

    keys = await _file_keys(db, row.id)
    await db.execute(delete(LogbookImportFile).where(LogbookImportFile.logbook_import_id == row.id))
    blob_store.delete_after_commit(db, keys)


async def delete_import(db: AsyncSession, *, row: LogbookImport, status: LogbookImportStatus) -> None:
    """Discard a pending import or leave an applied one out of the history, on the locked row.

    The caller names what it is deleting, so a discard sent from a review another device has since
    applied does not take the history.
    """
    if row.status != status.value:
        raise HTTPException(status_code=409, detail=f"This import is {row.status}, not {status.value}.")
    await _delete_import(db, row.id)
    await db.commit()


# One batch's rows and their file keys, comfortably one transaction.
PURGE_BATCH_SIZE = 200


async def purge_expired(db: AsyncSession, *, now: datetime, limit: int) -> int:
    """Delete up to `limit` imports past their expiry, their files after the commit, and return
    how many went.

    One choice of rows feeds the keys and the `DELETE`, or a row could go with its keys unread. The
    `DELETE` repeats the choice's predicate, so an import applied in between keeps its review, and
    only the keys of the imports that went are handed over.
    """
    expired = LogbookImport.expires_at < now
    ids = list((await db.execute(select(LogbookImport.id).where(expired).limit(limit))).scalars())
    keys_by_import = list(
        await db.execute(
            select(LogbookImportFile.logbook_import_id, LogbookImportFile.storage_key).where(
                LogbookImportFile.logbook_import_id.in_(ids)
            )
        )
    )
    gone = set(
        (
            await db.execute(
                delete(LogbookImport).where(LogbookImport.id.in_(ids), expired).returning(LogbookImport.id)
            )
        ).scalars()
    )
    blob_store.delete_after_commit(db, [one.storage_key for one in keys_by_import if one.logbook_import_id in gone])
    await db.commit()
    return len(gone)
