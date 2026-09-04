"""Turning a plan into rows, in one transaction and with no judgement of its own.

Everything worth deciding was decided in `planner.py`; this module issues the statements.
That split is what makes the preview honest - the report a diver approves is produced by
the same pass that decides what to write - and it is why the only notes added here are the
two a plan genuinely cannot predict: a restored file whose bytes do not match their digest,
and one whose bytes are not a format this app stores.

**Atomicity is in rows, and the blobs sit deliberately outside it.** Nothing here commits;
the caller does, once, at the end - so a failed, refused or interrupted import writes no
rows at all, and a retry after a timeout can never half-duplicate a logbook. The files
volume is written *before* the transaction that references it, which is the ordering
`store_dive_file` and `store_certification_file` already use for the same reason: every
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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ...crud.crud_dive_dive_sites import replace_dive_sites_for_dive
from ...crud.crud_dive_gear_items import replace_gear_items_for_dive
from ...crud.crud_dive_mixtures import replace_mixtures_for_dive
from ...crud.crud_dive_species import replace_species_for_dive
from ...crud.crud_gear_set_items import replace_gear_items_for_set
from ...models.certification import Certification
from ...models.certification_file import CertificationFile
from ...models.course import Course
from ...models.dive import Dive
from ...models.dive_file import DiveFile
from ...models.dive_site import DiveSite
from ...models.gear_item import GearItem
from ...models.gear_service_record import GearServiceRecord
from ...models.gear_service_schedule import GearServiceSchedule
from ...models.gear_set import GearSet
from ...models.trip import Trip
from ...models.trip_location import TripLocation
from ...schemas.certification import CertificationSide
from ...schemas.dive_mixture import DiveMixtureCreate
from ...schemas.logbook_import import ImportNote, ImportNoteCode
from ..blob_store import new_key
from ..blob_store import put as put_blob
from ..certification_files import KEY_KIND as CERTIFICATION_KEY_KIND
from ..certification_files import UnsupportedCardFileError, sniff_content_type
from ..dive_files import KEY_KIND as DIVE_FILE_KEY_KIND
from ..dive_files import delete_dive_file
from ..dive_profiles import store_profile
from ..dive_stats import recalculate_dive_stats
from ..gear_service import recalculate_service_schedule
from ..gear_stats import recalculate_gear_dive_counts
from .planner import IMPORT_PARSER_KEY, MAX_NOTES, Action, ImportPlan, PlannedFile, PlannedProfile, PlannedRecord
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

    def _note(self, code: ImportNoteCode, message: str, *, collection: str, uuid: uuid_pkg.UUID) -> None:
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
        await self._write_trips()
        await self._write_courses()
        await self._write_sites()
        await self._write_gear()
        await self._write_gear_sets()
        await self._write_schedules()
        await self._write_service_records()
        await self._write_certifications()
        await self._write_dives()
        await self._recalculate()

    async def _write_trips(self) -> None:
        for record in self._plan.writable("trips"):
            trip_id = await self._write_row("trips", Trip, record)
            locations = record.children.get("locations") or []
            if locations:
                await self._db.execute(insert(TripLocation), [{"trip_id": trip_id, **row} for row in locations])

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
            self._schedule_ids.append(await self._write_row("gear_service_schedules", GearServiceSchedule, record))

    async def _write_service_records(self) -> None:
        for record in self._plan.writable("gear_service_records"):
            gear_item_id = self._id("gear", record.children.get("gear_uuid"))
            if gear_item_id is None:
                continue
            record.values["gear_item_id"] = gear_item_id
            record.values["gear_service_schedule_id"] = self._id(
                "gear_service_schedules", record.children.get("schedule_uuid")
            )
            await self._write_row("gear_service_records", GearServiceRecord, record)

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
                # Before the row itself, not after: `delete_dive_file` clears the dive's
                # tech scalars along with the file they were read off, so running it
                # afterwards would wipe the CNS and OTU this import just wrote. It also
                # takes the old profile, which is what stops a restored dive from keeping
                # curves the document does not describe.
                await delete_dive_file(self._db, dive_id=record.row_id, commit=False)

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
            await self._write_dive_file_and_profile(record, dive_id)

    async def _write_dive_file_and_profile(self, record: PlannedRecord, dive_id: int) -> None:
        """The dive's stored export and its samples, which have to agree with each other.

        `dive_profile.source_sha256`'s documented job is to match the dive's *file* digest -
        `should_extract` and the backfill's candidate query both select on the mismatch - so
        it splits by path. On the **archive** path it records the restored file's own
        digest, which is truthful (the source instance extracted precisely this profile from
        precisely those bytes) and leaves file and profile in agreement, so an
        archive-restored dive is never a backfill candidate and the profile ETag still names
        a real file digest. On the **bare** path there is no file row - the dive cannot be a
        candidate regardless - and the column records the imported payload's own digest,
        purely as provenance.

        That is not the invention rule being bent: §5.4 governs logbook data a writer emits,
        and these three columns describe where *this instance's copy* came from, which
        really is the import.
        """
        planned_file: PlannedFile | None = record.children.get("source_file")
        planned_profile: PlannedProfile | None = record.children.get("profile")
        stored = None
        if planned_file is not None:
            stored = await self._store_blob(
                planned_file,
                kind=DIVE_FILE_KEY_KIND,
                collection="dives",
                record_uuid=record.source_uuid,
                sniff=False,
            )
        if stored is not None and planned_file is not None:
            await self._db.execute(
                insert(DiveFile).values(
                    user_id=self._user_id,
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

        if planned_profile is None:
            return
        source_sha256 = stored.digest if stored is not None else _payload_digest(planned_profile)
        parser_key = (planned_file.parser_key if stored is not None and planned_file is not None else None) or (
            IMPORT_PARSER_KEY
        )
        await store_profile(
            self._db,
            dive_id=dive_id,
            profile=planned_profile.profile,
            source_sha256=source_sha256,
            parser_key=parser_key,
            commit=False,
            duration=planned_profile.duration,
        )

    async def _recalculate(self) -> None:
        """Every derived member, from what this import actually wrote.

        Recomputed rather than imported, per spec §5.7 and the app's own derived-state rule:
        the destination's view of the underlying records is the only thing that can make
        these true, and a restored account whose dashboard reads zero dives is what skipping
        them ships. The service schedules come last because each one's next due date is a
        function of the service *records* this import just created.
        """
        await recalculate_dive_stats(self._db, user_id=self._user_id, commit=False)
        await recalculate_gear_dive_counts(self._db, user_id=self._user_id, commit=False)
        for schedule_id in self._schedule_ids:
            await recalculate_service_schedule(self._db, schedule_id, commit=False)


def _payload_digest(planned: PlannedProfile) -> str:
    """A provenance digest for a profile that arrived with no file behind it.

    Hashes the stored payload itself, which is the only thing this instance actually
    received. It can never equal a `dive_file.sha256` - there is no file - so it cannot
    accidentally satisfy `should_extract`, and a bare-imported dive is not a backfill
    candidate in any case.
    """
    return hashlib.sha256(repr(planned.profile.to_data()).encode("utf-8")).hexdigest()


async def write_import(db: AsyncSession, *, user_id: int, loaded: LoadedImport, plan: ImportPlan) -> None:
    """Write everything the plan describes. Does **not** commit - the caller owns that."""
    await _Writer(db, user_id=user_id, loaded=loaded, plan=plan).write()
