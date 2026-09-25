"""Turning a plan into rows, in one transaction and with no judgement of its own.

Everything worth deciding was decided in `planner.py`; this module issues the statements.
That split is what makes the preview honest - the report a diver approves is produced by
the same pass that decides what to write - and it is why the only notes added here are the
ones a plan genuinely cannot predict: a restored file whose bytes do not match their digest,
one whose bytes are not a format this app stores, and whether the portrait the diver took was
still theirs to replace when the write reached it.

**Atomicity is in rows, and the blobs sit deliberately outside it.** Nothing here commits;
the caller does, once, at the end - so a failed, refused or interrupted import writes no
rows at all, and a retry after a timeout can never half-duplicate a logbook. The files
volume is written *before* the transaction that references it, which is the ordering
`store_recording_file` and `store_certification_file` already use for the same reason: every
database-visible state names bytes that exist, and the only thing a failure can leave is an
unreferenced file. That is the recorded and accepted orphan case, reclaimed by
`sweep_orphaned_files.py`; there is no compensating unlink, which is the concurrent-write
trap `DECISIONS.md` records as having already gone wrong once.

The write order is `_RESOLUTION_ORDER`, so every reference resolves to a row that already
exists.
"""

import hashlib
import logging
import uuid as uuid_pkg
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ...crud.crud_dive_dive_sites import replace_dive_sites_for_dive
from ...crud.crud_dive_gear_items import replace_gear_items_for_dive
from ...crud.crud_dive_mixtures import get_mixtures_for_dive, replace_mixtures_for_dive
from ...crud.crud_dive_species import replace_species_for_dive
from ...crud.crud_gear_set_items import replace_gear_items_for_set
from ...models.certification import Certification
from ...models.certification_file import CertificationFile
from ...models.course import Course
from ...models.dive import Dive
from ...models.dive_file import DiveFile
from ...models.dive_recording import DiveRecording
from ...models.dive_site import DiveSite
from ...models.gear_item import GearItem
from ...models.gear_service_record import GearServiceRecord
from ...models.gear_service_schedule import GearServiceSchedule
from ...models.gear_set import GearSet
from ...models.trip import Trip
from ...models.trip_part import TripPart
from ...models.user import User
from ...schemas.certification import CertificationSide
from ...schemas.dive import DiveMode, Salinity
from ...schemas.dive_mixture import DiveMixtureCreate, as_create
from ...schemas.logbook_import import ImportNote, ImportNoteCode
from ...schemas.parsed_dive import DiveMixtureSchema, ParsedDecoModel, ParsedDevice
from ..blob_store import new_key
from ..blob_store import put as put_blob
from ..certification_files import KEY_KIND as CERTIFICATION_KEY_KIND
from ..certification_files import UnsupportedCardFileError, sniff_content_type
from ..dive_files import KEY_KIND as DIVE_FILE_KEY_KIND
from ..dive_files import (
    TECH_SCALAR_FIELDS,
    apply_gas_mapping,
    delete_files_for_dive,
    fill_dive_mixtures,
    fill_tech_scalars,
    relabel_gas_numbers,
)
from ..dive_profiles import (
    IMPORT_PARSER_KEY,
    get_existing_profile,
    profile_payload_digest,
    recording_source_digest,
    store_profile,
)
from ..dive_recordings import (
    DECO_MODEL_COLUMNS,
    DEVICE_COLUMNS,
    create_recording,
    fill_device_fields,
    fill_gate_figures,
    fill_readouts,
    fill_recording_settings,
    fill_start,
    next_ordinal,
)
from ..dive_stats import recalculate_dive_stats
from ..gear_service import recalculate_service_schedule
from ..gear_stats import recalculate_gear_dive_counts
from ..user_pictures import write_imported_portrait
from .planner import (
    MAX_NOTES,
    Action,
    ImportPlan,
    PlannedFile,
    PlannedProfile,
    PlannedRecord,
    PlannedRecording,
    PlannedRecordingMatch,
)
from .reader import LoadedImport

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _StoredBlob:
    """A binary that is already on the volume, waiting for the row that names it."""

    storage_key: str
    digest: str
    byte_size: int
    content_type: str


class _Writer:
    def __init__(self, db: AsyncSession, *, user_id: int, loaded: LoadedImport, plan: ImportPlan) -> None:
        self._db = db
        self._user_id = user_id
        self._loaded = loaded
        self._plan = plan
        self._now = datetime.now(UTC)
        # Document uuid -> row id, per collection. Seeded with everything the plan already
        # resolved (links and restores both name a row) and filled in as rows are created.
        self._ids: dict[tuple[str, uuid_pkg.UUID], int] = {
            (collection, record.source_uuid): record.row_id
            for collection, records in plan.records.items()
            for record in records.values()
            if record.row_id is not None
        }
        # Every schedule whose due dates this import invalidated - the ones it wrote, and
        # the ones it merely hung a new service record on. The second kind is what a
        # "recalculate what I created" reading misses; see `_recalculate`.
        self._schedule_ids: list[int] = []

    # ------------------------------------------------------------------ helpers

    def _id(self, collection: str, source_uuid: uuid_pkg.UUID | None) -> int | None:
        return None if source_uuid is None else self._ids.get((collection, source_uuid))

    def _ids_for(self, collection: str, source_uuids: list[uuid_pkg.UUID]) -> list[int]:
        return [row_id for row_id in (self._id(collection, one) for one in source_uuids) if row_id is not None]

    async def _write_row(self, collection: str, model: Any, record: PlannedRecord) -> int:
        """Insert a new row, or bring a soft-deleted one back and overwrite it.

        The restore branch clears `is_deleted` and `deleted_at` **together** and rewrites
        every imported column, keeping nothing of the husk but its identity (`id`, `uuid`,
        `user_id`). Anything less would leave a row that is half the document's and half
        whatever it was when the diver deleted it, which is the state nobody could reason
        about afterwards.
        """
        if record.action is Action.RESTORE and record.row_id is not None:
            await self._db.execute(
                update(model)
                .where(model.id == record.row_id)
                .values(**record.values, is_deleted=False, deleted_at=None, updated_at=self._now)
            )
            self._ids[(collection, record.source_uuid)] = record.row_id
            return record.row_id

        result = await self._db.execute(
            # `uuid` is spelled out rather than left to `PublicUUIDMixin`'s
            # `default_factory`: that is a dataclass-level default applied when the ORM
            # constructs an instance, and a Core-level INSERT never constructs one.
            insert(model).values(**record.values, uuid=record.uuid).returning(model.id)
        )
        row_id = int(result.scalar_one())
        self._ids[(collection, record.source_uuid)] = row_id
        return row_id

    def _note(
        self, code: ImportNoteCode, message: str, *, collection: str | None = None, uuid: uuid_pkg.UUID | None = None
    ) -> None:
        if len(self._plan.notes) >= MAX_NOTES:
            self._plan.notes_dropped += 1
            return
        self._plan.notes.append(ImportNote(code=code, collection=collection, uuid=uuid, message=message))

    # ------------------------------------------------------------------ blobs

    async def _store_blob(
        self, planned: PlannedFile, *, kind: str, collection: str, record_uuid: uuid_pkg.UUID, sniff: bool
    ) -> _StoredBlob | None:
        """Verify one member of the archive and put it on the volume.

        **The digest is checked against the document's own manifest entry**, which is what
        makes a restored file verified end to end rather than merely present: the entry was
        written from the source instance's stored digest, so a match says the bytes survived
        the export, the zip and the transfer intact.

        `sniff` is for card images, whose content type is read off the leading bytes exactly
        as `store_certification_file` reads it - a document is no more trustworthy about a
        media type than an uploader is, and that value ends up in a response header.
        """
        data = self._loaded.read_member(planned.archive_path)
        if data is None:
            # Either the member vanished from the container's index or it would not inflate
            # - a password-protected archive, a CRC failure, a compression method this build
            # has no decoder for. `read_member` collapses them because the answer here is
            # the same for all of them, and it is not "abort a half-written import".
            self._file_skipped(collection, record_uuid, "its bytes could not be read out of the archive")
            return None
        digest = hashlib.sha256(data).hexdigest()
        if digest != planned.sha256:
            self._file_skipped(collection, record_uuid, "its bytes did not match the digest the document recorded")
            return None

        content_type = planned.content_type
        if sniff:
            try:
                content_type = sniff_content_type(data)
            except UnsupportedCardFileError:
                self._file_skipped(collection, record_uuid, "its bytes are not an image or a PDF")
                return None

        # Minted per write and never derived from the row, which is `blob_store.new_key`'s
        # whole rule: a retired key must never be mintable again, or a concurrent write's
        # post-commit unlink deletes the file another request has just put there.
        key = new_key(kind, sha256=digest)
        await put_blob(key, data)
        return _StoredBlob(storage_key=key, digest=digest, byte_size=len(data), content_type=content_type)

    def _file_skipped(self, collection: str, record_uuid: uuid_pkg.UUID, reason: str) -> None:
        """Correct the plan's file counts for the one thing planning cannot know.

        `member_size` told the plan the member was there and small enough; only reading it
        can say whether it is the file the document claims. So the count moves here rather
        than the preview being vaguer about it.
        """
        self._plan.files_restored -= 1
        self._plan.files_skipped += 1
        self._note(
            ImportNoteCode.FILE_SKIPPED,
            f"A stored file was not restored because {reason}.",
            collection=collection,
            uuid=record_uuid,
        )

    # ------------------------------------------------------------------ collections

    async def write(self) -> None:
        await self._write_check_in()
        await self._write_portrait()
        await self._write_trips()
        await self._write_courses()
        await self._write_sites()
        await self._write_gear()
        await self._write_gear_sets()
        await self._write_schedules()
        await self._write_service_records()
        await self._write_certifications()
        await self._write_dives()
        # After the dives, deliberately: every match named a dive that predates this import,
        # and writing them last keeps that true of the order as well as of the plan.
        await self._write_recording_matches()
        await self._recalculate()

    async def _write_check_in(self) -> None:
        if self._plan.check_in_values:
            await self._db.execute(
                update(User).where(User.id == self._user_id).values(**self._plan.check_in_values, updated_at=self._now)
            )

    async def _write_portrait(self) -> None:
        portrait = self._plan.portrait
        if portrait is None or not portrait.take:
            return
        if await write_imported_portrait(
            self._db,
            user_id=self._user_id,
            picture=portrait.picture,
            filename=portrait.filename,
            held=portrait.held,
        ):
            self._note(
                ImportNoteCode.CHECK_IN_DETAIL_WRITTEN, "The portrait chosen in the preview was saved to this account."
            )
        else:
            self._note(
                ImportNoteCode.PORTRAIT_KEPT,
                "This account's portrait was kept: it changed while the archive's was being saved.",
            )

    async def _write_trips(self) -> None:
        for record in self._plan.writable("trips"):
            trip_id = await self._write_row("trips", Trip, record)
            parts = record.children.get("parts") or []
            if parts:
                await self._db.execute(insert(TripPart), [{"trip_id": trip_id, **row} for row in parts])

    async def _write_courses(self) -> None:
        for record in self._plan.writable("courses"):
            await self._write_row("courses", Course, record)

    async def _write_sites(self) -> None:
        for record in self._plan.writable("sites"):
            await self._write_row("sites", DiveSite, record)

    async def _write_gear(self) -> None:
        for record in self._plan.writable("gear"):
            await self._write_row("gear", GearItem, record)

    async def _write_gear_sets(self) -> None:
        for record in self._plan.writable("gear_sets"):
            set_id = await self._write_row("gear_sets", GearSet, record)
            await replace_gear_items_for_set(
                db=self._db,
                gear_set_id=set_id,
                gear_item_ids=self._ids_for("gear", record.children.get("gear_uuids") or []),
                commit=False,
            )

    async def _write_schedules(self) -> None:
        for record in self._plan.writable("gear_service_schedules"):
            gear_item_id = self._id("gear", record.children.get("gear_uuid"))
            if gear_item_id is None:
                continue
            record.values["gear_item_id"] = gear_item_id
            self._stale_schedule(await self._write_row("gear_service_schedules", GearServiceSchedule, record))

    async def _write_service_records(self) -> None:
        for record in self._plan.writable("gear_service_records"):
            gear_item_id = self._id("gear", record.children.get("gear_uuid"))
            if gear_item_id is None:
                continue
            record.values["gear_item_id"] = gear_item_id
            schedule_id = self._id("gear_service_schedules", record.children.get("schedule_uuid"))
            record.values["gear_service_schedule_id"] = schedule_id
            await self._write_row("gear_service_records", GearServiceRecord, record)
            # **Whether or not this import wrote that schedule.** A record attached to a rule
            # the caller already had moves its due dates exactly as one attached to a rule
            # this import created does, and `api/v1/gear_service.py` recalculates on every
            # record create, update and delete for precisely that reason. Collecting only
            # the schedules this import wrote left a linked one carrying a stale
            # `last_service_on`, stale `next_due_*`, and stale notify state that would go on
            # suppressing the reminder for a threshold it had already crossed.
            self._stale_schedule(schedule_id)

    async def _write_certifications(self) -> None:
        for record in self._plan.writable("certifications"):
            record.values["course_id"] = self._id("courses", record.children.get("course_uuid"))
            certification_id = await self._write_row("certifications", Certification, record)
            for side in (CertificationSide.FRONT, CertificationSide.BACK):
                planned = record.children.get(side.value)
                if planned is None:
                    continue
                stored = await self._store_blob(
                    planned,
                    kind=CERTIFICATION_KEY_KIND,
                    collection="certifications",
                    record_uuid=record.source_uuid,
                    sniff=True,
                )
                if stored is None:
                    continue
                await self._db.execute(
                    insert(CertificationFile).values(
                        certification_id=certification_id,
                        side=side.value,
                        content_type=stored.content_type,
                        byte_size=stored.byte_size,
                        original_filename=planned.original_filename,
                        sha256=stored.digest,
                        storage_key=stored.storage_key,
                        uuid=uuid7(),
                        created_at=self._now,
                    )
                )

    async def _write_dives(self) -> None:
        for record in self._plan.writable("dives"):
            if record.action is Action.RESTORE and record.row_id is not None:
                # Before the row itself, not after: `delete_files_for_dive` clears the
                # dive's tech scalars along with the recordings they were read off, so
                # running it afterwards would wipe the positions this import just wrote. It
                # also takes the old recordings and their profiles, which is what stops a
                # restored dive from keeping curves the document does not describe.
                await delete_files_for_dive(self._db, dive_id=record.row_id, commit=False)

            record.values["trip_id"] = self._id("trips", record.children.get("trip_uuid"))
            record.values["course_id"] = self._id("courses", record.children.get("course_uuid"))
            dive_id = await self._write_row("dives", Dive, record)

            await replace_mixtures_for_dive(
                db=self._db,
                dive_id=dive_id,
                mixtures=[DiveMixtureCreate(**row) for row in record.children.get("mixtures") or []],
                commit=False,
            )
            await replace_dive_sites_for_dive(
                db=self._db,
                dive_id=dive_id,
                dive_site_ids=self._ids_for("sites", record.children.get("site_uuids") or []),
                commit=False,
            )
            await replace_gear_items_for_dive(
                db=self._db,
                dive_id=dive_id,
                gear_item_ids=self._ids_for("gear", record.children.get("gear_uuids") or []),
                commit=False,
            )
            await replace_species_for_dive(
                db=self._db, dive_id=dive_id, species_ids=record.children.get("species_ids") or [], commit=False
            )
            for recording in record.children.get("recordings") or []:
                await self._write_recording(record, dive_id, recording)

    async def _write_recording(
        self, record: PlannedRecord, dive_id: int, planned: PlannedRecording, *, ordinal: int | None = None
    ) -> int:
        """One recording, its files and its samples, which have to agree with each other.

        `dive_profile.source_sha256`'s documented job is to match what the profile was read
        out of - `should_extract` and the backfill's candidate query both select on the
        mismatch - so it splits by path. On the **archive** path it records the restored
        files' digest, which is truthful (the source instance extracted precisely this
        profile from precisely those bytes) and leaves files and profile in agreement, so an
        archive-restored recording is never a backfill candidate and the profile ETag still
        names real file digests. On the **bare** path there are no file rows - the recording
        cannot be a candidate regardless - and the column records the imported payload's own
        digest, purely as provenance.

        That is not the invention rule being bent: §5.4 governs logbook data a writer emits,
        and these columns describe where *this instance's copy* came from, which really is
        the import.

        `parser_key` is `divejson_import` whenever this instance stored no bytes, and that is
        more than provenance: it is what tells both backfills these samples can never be
        re-derived here, so nothing later overwrites them with an extraction off files the
        recording does not have.
        """
        recording_id = await create_recording(
            self._db,
            dive_id=dive_id,
            user_id=self._user_id,
            ordinal=planned.ordinal if ordinal is None else ordinal,
            mode=None if planned.mode is None else DiveMode(planned.mode),
            salinity=None if planned.salinity is None else Salinity(planned.salinity),
            readouts=planned.readouts,
            start_time=planned.start_time,
            utc_offset_minutes=planned.utc_offset_minutes,
            duration=planned.duration,
            max_depth=planned.max_depth,
        )
        # The device columns and the model's, both already keyed by column name and both
        # already bounded by the planner - one statement rather than two, since they land on
        # the same row and neither depends on the other. `mode` goes through
        # `create_recording` instead, being a plain column the insert already names.
        columns = {**planned.device, **planned.deco_model}
        if columns:
            await self._db.execute(update(DiveRecording).where(DiveRecording.id == recording_id).values(**columns))

        digests, parser_key = await self._write_recording_files(
            record.source_uuid, recording_id=recording_id, dive_id=dive_id, planned_files=planned.files
        )

        if planned.profile is None:
            return recording_id
        await store_profile(
            self._db,
            recording_id=recording_id,
            dive_id=dive_id,
            profile=planned.profile.profile,
            source_sha256=recording_source_digest(digests) if digests else _payload_digest(planned.profile),
            parser_key=(parser_key or IMPORT_PARSER_KEY) if digests else IMPORT_PARSER_KEY,
            commit=False,
            duration=planned.profile.duration,
        )
        return recording_id

    async def _write_recording_files(
        self,
        source_uuid: uuid_pkg.UUID,
        *,
        recording_id: int,
        dive_id: int,
        planned_files: Sequence[PlannedFile],
    ) -> tuple[list[str], str | None]:
        """Store an archive's bytes against one recording. Returns their digests, in order.

        Shared by both write paths, and that is the point: a recording the import *creates*
        and one it *fills* are alike in this respect - each is a record whose files the
        archive is carrying, and the planner has already counted and claimed them either
        way. It lived inside `_write_recording` while only that path had files, and the fill
        path then silently dropped every one of them while the report went on saying
        `files.restored`.
        """
        digests: list[str] = []
        parser_key: str | None = None
        for planned_file in planned_files:
            stored = await self._store_blob(
                planned_file,
                kind=DIVE_FILE_KEY_KIND,
                collection="dives",
                record_uuid=source_uuid,
                sniff=False,
            )
            if stored is None:
                continue
            await self._db.execute(
                insert(DiveFile).values(
                    user_id=self._user_id,
                    recording_id=recording_id,
                    dive_id=dive_id,
                    sha256=stored.digest,
                    content_type=stored.content_type,
                    byte_size=stored.byte_size,
                    original_filename=planned_file.original_filename,
                    parser_key=planned_file.parser_key or IMPORT_PARSER_KEY,
                    storage_key=stored.storage_key,
                    uuid=uuid7(),
                    created_at=self._now,
                )
            )
            digests.append(stored.digest)
            parser_key = parser_key or planned_file.parser_key
        return digests, parser_key

    async def _write_recording_matches(self) -> None:
        """Apply the incoming recordings that belong to dives the caller already has.

        **After every dive is written**, which is what stops a match landing on a dive this
        same import created: the gates ran against the logbook as it was when the plan was
        made, so every `dive_id` here is a row that predates the import, and walking this list
        last keeps the write in the same order the plan reasoned in.

        A **fill** writes no recording row. It fills the stored recording's blanks - its
        device columns, its settings, its readouts, its start, the two figures the gates
        compare - and, where that recording had no samples at all, its profile. The stored
        dive's own blanks fill too, from the primary recording alone: its positions, and the
        members its cylinders have none of, where those cylinders still demonstrably describe
        the document's. Nothing is ever overwritten,
        which is the whole rule: the diver may have corrected any of it, and a fill that won
        an argument with an edit would be the silent loss this repository already refuses on
        the backfill path.

        An **attach** appends a recording to that dive, after its last. It touches none of
        the dive's own figures - those are the primary recording's - and it maps its
        cylinder labels onto the dive's own list, because `gas_number` is dive-scoped and a
        second computer numbers its tanks its own way.
        """
        for match in self._plan.recording_matches:
            if match.kind == "attach":
                await self._attach_recording(match)
            else:
                await self._fill_recording(match)

    async def _attach_recording(self, match: PlannedRecordingMatch) -> None:
        ordinal = await next_ordinal(self._db, dive_id=match.dive_id)
        stored_mixtures = await get_mixtures_for_dive(db=self._db, dive_id=match.dive_id)
        planned = match.recording
        if planned.profile is not None and match.mixtures:
            mapping, appended = relabel_gas_numbers(
                [DiveMixtureSchema(**row) for row in match.mixtures], stored_mixtures
            )
            remapped = apply_gas_mapping(planned.profile.profile, mapping)
            if remapped is not None:
                planned = replace(planned, profile=replace(planned.profile, profile=remapped))
            if appended:
                await replace_mixtures_for_dive(
                    db=self._db,
                    dive_id=match.dive_id,
                    mixtures=[
                        *(as_create(row) for row in stored_mixtures),
                        # `appended` is the parser's own shape, not a stored row - its `role`/
                        # `usage` are enums already, so it needs no `as_create`.
                        *(DiveMixtureCreate(**row.model_dump()) for row in appended),
                    ],
                    commit=False,
                )
        record = PlannedRecord(action=Action.CREATE, source_uuid=match.source_uuid, uuid=match.source_uuid)
        await self._write_recording(record, match.dive_id, planned, ordinal=ordinal)

    async def _fill_recording(self, match: PlannedRecordingMatch) -> None:
        planned = match.recording
        if match.recording_id is None:  # pragma: no cover - a `fill` always names one
            return

        await fill_device_fields(
            self._db,
            recording_id=match.recording_id,
            device=ParsedDevice(**{member: planned.device.get(column) for member, column in DEVICE_COLUMNS.items()}),
        )
        # The same fill-only rule for the two settings, through the same helper the attach
        # path uses: this document is a second reading of a record the logbook already holds,
        # so it contributes what the stored recording has no value for and overwrites nothing.
        #
        # Rebuilt as the parsed shapes because that is what the fill takes, and the round trip
        # is safe rather than lossy: the planner has already bounded every member and dropped
        # the gradient-factor pair unless it is whole and in order, so `ParsedDecoModel`'s own
        # validators have nothing left to reject.
        await fill_recording_settings(
            self._db,
            recording_id=match.recording_id,
            mode=None if planned.mode is None else DiveMode(planned.mode),
            deco_model=ParsedDecoModel(
                **{member: planned.deco_model.get(column) for member, column in DECO_MODEL_COLUMNS.items()}
            ),
            salinity=None if planned.salinity is None else Salinity(planned.salinity),
        )
        # The recording's own readouts, on every recording and above the primary-only line
        # below: they are this device's figures, whichever position it holds on the dive.
        await fill_readouts(self._db, recording_id=match.recording_id, readouts=planned.readouts)
        await fill_gate_figures(
            self._db, recording_id=match.recording_id, duration=planned.duration, max_depth=planned.max_depth
        )
        await fill_start(
            self._db,
            recording_id=match.recording_id,
            start_time=planned.start_time,
            utc_offset_minutes=planned.utc_offset_minutes,
        )

        # **The archive's bytes go onto the stored recording**, which is the one thing a fill
        # writes rather than fills: a file is not a value that can already be there. This is
        # the second export of a record the logbook already holds - the case a recording
        # exists to make representable - so it joins that recording's files exactly as it
        # would through the attach route. The planner has already counted these as restored
        # and claimed their digests, so dropping them here reported bytes as stored that were
        # never written.
        #
        # The stored profile is left alone. Its `source_sha256` no longer names what the
        # recording holds, which makes it a `backfill_profiles` candidate - and re-deriving
        # from the newly arrived bytes is that script's job rather than an import's, an import
        # being the one path that stores samples it did not extract.
        await self._write_recording_files(
            match.source_uuid,
            recording_id=match.recording_id,
            dive_id=match.dive_id,
            planned_files=planned.files,
        )

        if (
            planned.profile is not None
            and await get_existing_profile(self._db, recording_id=match.recording_id) is None
        ):
            # Samples for a record that had none - a device-only recording an older document
            # created, meeting the document that carries its curves. A recording that already
            # has samples keeps them: two readings of one sensor are not merged.
            await store_profile(
                self._db,
                recording_id=match.recording_id,
                dive_id=match.dive_id,
                profile=planned.profile.profile,
                source_sha256=_payload_digest(planned.profile),
                parser_key=IMPORT_PARSER_KEY,
                commit=False,
                duration=planned.profile.duration,
            )

        # **Everything below this line is the dive's rather than the recording's, and only
        # the primary recording may write it** - `_rederive_recording`'s rule on the attach
        # path. A secondary recording is a second computer's account of the same dive: its
        # positions are its own, and its cylinder labelling is its own numbering.
        if match.ordinal != 0:
            return

        # `dive_values` is `PlannedRecord.values`, which is already keyed by column name -
        # the same dict the dive insert would have taken - so `TECH_SCALAR_FIELDS` indexes it
        # directly: the entry and exit fixes. A member the document did not carry is `None`
        # and `fill_tech_scalars` skips it; one it did carry lands only where the dive has none.
        await fill_tech_scalars(
            self._db,
            dive_id=match.dive_id,
            scalars={name: match.dive_values.get(name) for name in TECH_SCALAR_FIELDS},
        )
        # The cylinders fill on the same terms as the readings above and through the same
        # function the attach route uses: this document is a second reading of a record the
        # logbook already holds, so its `oxygen` lands in a cylinder that has none and never
        # over one that has. Not `merge_mixture_fields` - that is the backfill's question
        # (may these values be written *over* these rows?) and it would put this document's
        # `gas_number` on top of the label the stored profile's pressure channels are already
        # attributed under.
        await fill_dive_mixtures(
            self._db, dive_id=match.dive_id, parsed=[DiveMixtureSchema(**row) for row in match.mixtures]
        )

    def _stale_schedule(self, schedule_id: int | None) -> None:
        """Mark one schedule as needing its due dates recomputed. Deduped, order kept."""
        if schedule_id is not None and schedule_id not in self._schedule_ids:
            self._schedule_ids.append(schedule_id)

    async def _recalculate(self) -> None:
        """Every derived member, from what this import actually wrote.

        Recomputed rather than imported, per spec §5.7 and the app's own derived-state rule:
        the destination's view of the underlying records is the only thing that can make
        these true, and a restored account whose dashboard reads zero dives is what skipping
        them ships. The service schedules come last because each one's next due date is a
        function of the service *records* this import just created - and the set is every
        schedule this import *touched*, which is not the same as every schedule it wrote: a
        record landing on a rule the caller already had moves that rule's dates too.
        """
        await recalculate_dive_stats(self._db, user_id=self._user_id, commit=False)
        await recalculate_gear_dive_counts(self._db, user_id=self._user_id, commit=False)
        for schedule_id in self._schedule_ids:
            await recalculate_service_schedule(self._db, schedule_id, commit=False)


def _payload_digest(planned: PlannedProfile) -> str:
    """A provenance digest for a profile that arrived with no file behind it.

    Unwraps the `PlannedProfile` and defers to `profile_payload_digest`, which the merge
    writes its own folded samples under: the two are the same question - what does a profile
    no file produced record as its source? - and two hashes of one payload would be two
    things to keep in step.
    """
    return profile_payload_digest(planned.profile)


async def write_import(db: AsyncSession, *, user_id: int, loaded: LoadedImport, plan: ImportPlan) -> None:
    """Write everything the plan describes. Does **not** commit - the caller owns that."""
    await _Writer(db, user_id=user_id, loaded=loaded, plan=plan).write()
