"""Logbook import: `services/logbook_import/`, end to end against real Postgres.

**The corpus is the app's own export.** Every round-trip test below builds a logbook with
the ordinary generators, streams it through the *real* `write_divejson`, and imports the
bytes back - so what is under test is the pair, not a hand-written fixture that could
drift from either half. A fixture would also have to be re-written every time the writer
gains a member, which is precisely when a round trip is worth having.

Postgres-backed rather than mocked, and the reasons are the same three the feature's own
invariants turn on. The uuid rules read `is_deleted` and `user_id` off real rows across
three accounts. The uniqueness resolutions are decided by real indexes -
`ux_dive_site_user_id_name_location_lower` is a functional index on `lower()` and
`coalesce()`, which no mock can be wrong about. And "all or nothing in rows" is a claim
about a transaction.

The hand-written documents that do appear are the cases the writer cannot produce: a
document from a *later* minor version, one carrying a value this app refuses, one whose
`started_at` has no offset. Those are the reader's obligations rather than the round
trip's, and the reference implementation is by construction unable to exercise them.
"""

import hashlib
import io
import json
import uuid as uuid_pkg
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import divejson
import pytest
import pytest_asyncio
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import dives as dives_module
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.models.certification import Certification
from src.app.models.course import Course
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_file import DiveFile
from src.app.models.dive_gear_item import DiveGearItem
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_recording import DiveRecording
from src.app.models.dive_site import DiveSite
from src.app.models.dive_species import DiveSpecies
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.gear_set import GearSet
from src.app.models.gear_set_item import GearSetItem
from src.app.models.species import Species
from src.app.models.trip import Trip
from src.app.schemas.certification import CertificationAgency
from src.app.schemas.dive import DiveUpdateRequest
from src.app.schemas.logbook_import import ImportNoteCode
from src.app.services.export import load_export_bundle, write_divejson
from src.app.services.export.archive import DIVEJSON_NAME
from src.app.services.export.paths import plan_archive_paths
from src.app.services.logbook_import import (
    ImportTooLargeError,
    UnsupportedImportError,
    load_import,
    parse_document,
    plan_import,
    write_import,
)
from src.app.services.logbook_import import reader as import_reader
from src.app.services.logbook_import.planner import _DIVE_BOUNDS, _MIXTURE_BOUNDS
from src.app.services.logbook_import.reader import DuplicateMemberError, MalformedImportError
from tests.conftest import db_available
from tests.helpers.generators import (
    create_certification,
    create_course,
    create_dive,
    create_dive_recording,
    create_dive_site,
    create_gear_item,
    create_gear_service_record,
    create_gear_service_schedule,
    create_gear_set,
    create_species,
    create_trip,
    create_user,
)

pytestmark = pytest.mark.skipif(not db_available(), reason="Postgres is not reachable")


# ------------------------------------------------------------------ plumbing


def _upload(data: bytes, filename: str = "logbook.divejson") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename, size=len(data))


async def _export(db: AsyncSession, user_id: int, *, archive_paths: bool = False) -> bytes:
    bundle = await load_export_bundle(db, user_id=user_id)
    paths = plan_archive_paths(bundle) if archive_paths else None
    exported_at = datetime.now(UTC)
    chunks = [chunk async for chunk in write_divejson(db, bundle, exported_at=exported_at, paths=paths)]
    return b"".join(chunks)


async def _patch_start_time(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, owner: Any, dive_uuid: uuid_pkg.UUID, start_time: str
) -> None:
    """Drive the real `PATCH /dive/{uuid}` against a dive this module imported.

    Only the two Redis invalidators are stubbed; the ownership fetch, the offset rule and
    the UPDATE all run for real, because what these two tests claim is about the endpoint.
    `test_dive_start_time.py` pins `split_updated_start_time` on its own.
    """
    for name in ("invalidate_dive_caches", "invalidate_gear_caches"):
        monkeypatch.setattr(dives_module, name, AsyncMock())
    await dives_module.patch_dive(
        request=MagicMock(),
        uuid=dive_uuid,
        values=DiveUpdateRequest.model_validate({"start_time": start_time}),
        current_user={"id": owner.id, "uuid": owner.uuid},
        db=db,
    )


async def _preview(db: AsyncSession, user_id: int, data: bytes, filename: str = "logbook.divejson") -> Any:
    with await load_import(_upload(data, filename)) as loaded:
        return await plan_import(db, user_id=user_id, loaded=loaded)


async def _apply(db: AsyncSession, user_id: int, data: bytes, filename: str = "logbook.divejson") -> Any:
    """Plan and write in one transaction, exactly as `POST /import/logbook` does.

    The species pre-pass is deliberately absent: it makes an outbound call, and every
    species in these tests is either already in the catalog or meant to be reported as
    unresolvable. `resolution_ran=True` is what tells the planner to say so.
    """
    with await load_import(_upload(data, filename)) as loaded:
        plan = await plan_import(db, user_id=user_id, loaded=loaded, resolution_ran=True)
        await write_import(db, user_id=user_id, loaded=loaded, plan=plan)
        await db.commit()
        return plan


def _counts(plan: Any) -> dict[str, tuple[int, int, int, int]]:
    return {
        report.collection: (report.created, report.linked, report.restored, report.skipped)
        for report in plan.collection_reports()
    }


def _codes(plan: Any) -> set[ImportNoteCode]:
    return {note.code for note in plan.notes}


def _seed_logbook(db: Session) -> Any:
    """One diver with something in every collection the envelope carries.

    Deliberately not the minimum: the round trip is only worth running over a document
    that exercises the reference chains - a dive that names a trip, a course, two sites and
    a gear item, a gear set over that item, a schedule on it and a record against the
    schedule.
    """
    user = create_user(db)
    trip = create_trip(db, user)
    course = create_course(db, user)
    site_a = create_dive_site(db, user)
    site_b = create_dive_site(db, user)
    item = create_gear_item(db, user)
    gear_set = create_gear_set(db, user)
    schedule = create_gear_service_schedule(db, user, item)
    create_gear_service_record(db, user, item, schedule=schedule)
    create_certification(db, user, course=course)
    species = create_species(db)
    dive = create_dive(db, user, trip=trip, course=course)
    db.add(DiveMixture(dive_id=dive.id, volume=12.0, oxygen=32.0, helium=0.0, start_pressure=200.0, end_pressure=60.0))
    db.commit()

    db.add_all(
        [
            DiveDiveSite(dive_id=dive.id, dive_site_id=site_a.id, position=0),
            DiveDiveSite(dive_id=dive.id, dive_site_id=site_b.id, position=1),
            DiveGearItem(dive_id=dive.id, gear_item_id=item.id, position=0),
            DiveSpecies(dive_id=dive.id, species_id=species.id, position=0),
            GearSetItem(gear_set_id=gear_set.id, gear_item_id=item.id, position=0),
        ]
    )
    db.commit()
    return user


@pytest_asyncio.fixture
async def seeded(db: Session, async_db: AsyncSession) -> Any:
    user = _seed_logbook(db)
    return user, await _export(async_db, user.id)


# ------------------------------------------------------- converted uploads

UDDF_CORPUS = Path(__file__).parent / "fixtures" / "uddf" / "demo-account.uddf"


async def _convert_and_plan(
    db: AsyncSession, user_id: int, data: bytes, moment: datetime, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict, Any]:
    """One conversion, stamped with a chosen `exported_at`, plus the plan it produces."""
    monkeypatch.setattr(import_reader, "_conversion_moment", lambda: moment)
    with await load_import(_upload(data, "demo-account.uddf")) as loaded:
        assert loaded.conversion is not None
        return loaded.conversion.document, await plan_import(db, user_id=user_id, loaded=loaded)


class TestConvertedUploads:
    """A logbook this app did not write, through the same four stages.

    The corpus is `tests/fixtures/uddf/demo-account.uddf` - a real download of a whole demo
    account out of this app's own UDDF writer - because it is the only captured
    dive-computer-shaped document this repository has and it exercises the widest document
    the converter will meet here. The library's own fixtures cover the readers themselves.
    """

    @pytest.mark.asyncio
    async def test_a_uddf_logbook_imports(self, db: Session, async_db: AsyncSession) -> None:
        user = create_user(db)

        plan = await _apply(async_db, user.id, UDDF_CORPUS.read_bytes(), "demo-account.uddf")

        created = _counts(plan)["dives"][0]
        assert created > 0
        stored = (await async_db.execute(select(Dive).where(Dive.user_id == user.id))).scalars().all()
        assert len(stored) == created

    @pytest.mark.asyncio
    async def test_converting_twice_plans_identically(
        self, db: Session, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Why apply can re-convert instead of spooling the preview's result.

        `exported_at` is the one value in a converted document that is not a function of the
        source, and nothing downstream reads it - which is what makes the token over the
        *uploaded* bytes an honest receipt. Everything else is frozen namespaces, `uuid5`
        identities and positional fallback in document order, so a conversion an hour later
        is the same document and the same plan.
        """
        user = create_user(db)
        source = UDDF_CORPUS.read_bytes()

        morning, plan_a = await _convert_and_plan(
            async_db, user.id, source, datetime(2026, 4, 17, 9, 0, tzinfo=UTC), monkeypatch
        )
        evening, plan_b = await _convert_and_plan(
            async_db, user.id, source, datetime(2026, 9, 7, 18, 30, tzinfo=UTC), monkeypatch
        )

        assert morning["exported_at"] != evening["exported_at"], "the two runs must differ somewhere"
        assert divejson.compared(morning) == divejson.compared(evening)
        assert _counts(plan_a) == _counts(plan_b)
        assert [(note.code, note.collection, note.uuid, note.message) for note in plan_a.notes] == [
            (note.code, note.collection, note.uuid, note.message) for note in plan_b.notes
        ]
        assert plan_a.file_report() == plan_b.file_report()


# ------------------------------------------------------------------ the round trip


class TestTheRoundTrip:
    """Export a logbook, import it into a second account, and check what arrived.

    The cross-account case rather than the same-account one on purpose: it is the branch
    where every uuid is somebody else's, so it exercises the remap rule *and* the reference
    rewriting that has to follow it. Same-account import is idempotence, below.
    """

    @pytest.mark.asyncio
    async def test_every_collection_arrives_once(self, seeded: Any, db: Session, async_db: AsyncSession) -> None:
        _, document = seeded
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, document)

        counts = _counts(plan)
        assert counts["dives"] == (1, 0, 0, 0)
        assert counts["trips"] == (1, 0, 0, 0)
        assert counts["sites"] == (2, 0, 0, 0)
        assert counts["gear"] == (1, 0, 0, 0)
        assert counts["gear_sets"] == (1, 0, 0, 0)
        assert counts["gear_service_schedules"] == (1, 0, 0, 0)
        assert counts["gear_service_records"] == (1, 0, 0, 0)
        assert counts["certifications"] == (1, 0, 0, 0)

        dives = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().all()
        assert len(dives) == 1
        assert dives[0].trip_id is not None

    @pytest.mark.asyncio
    async def test_a_second_export_of_the_destination_says_the_same_thing(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The strongest single assertion this feature has: the destination's own export is
        semantically the document that built it, up to identity and the members a bare
        document cannot carry."""
        _, document = seeded
        destination = create_user(db)
        await _apply(async_db, destination.id, document)

        re_exported = parse_document(await _export(async_db, destination.id))
        issues = divejson.validate_document(re_exported)
        assert not issues, [str(issue) for issue in issues]

        original = parse_document(document)
        for collection in ("dives", "trips", "sites", "gear", "gear_sets", "certifications"):
            assert len(re_exported[collection]) == len(original[collection]), collection

        source_dive, restored_dive = original["dives"][0], re_exported["dives"][0]
        for member in ("dive_number", "started_at", "duration", "max_depth", "avg_depth", "created_at"):
            assert restored_dive.get(member) == source_dive.get(member), member
        assert len(restored_dive["site_uuids"]) == 2
        assert restored_dive["cylinders"] == source_dive["cylinders"]

    @pytest.mark.asyncio
    async def test_uuids_are_remapped_because_they_belong_to_the_source(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The cross-account cause, isolated: nothing in this document is a duplicate, so
        the *other* remap code must not appear. Which code comes out is the contract - the
        two differ in where a reference to the old identifier lands."""
        source_user, document = seeded
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, document)

        codes = _codes(plan)
        assert ImportNoteCode.RECORD_REMAPPED_REFERENCES_FOLLOW in codes
        assert ImportNoteCode.RECORD_REMAPPED_REFERENCES_STAY not in codes
        original = parse_document(document)
        source_uuids = {uuid_pkg.UUID(dive["uuid"]) for dive in original["dives"]}
        destination_uuids = set(
            (await async_db.execute(select(Dive.uuid).where(Dive.user_id == destination.id))).scalars()
        )
        assert not (source_uuids & destination_uuids)
        # And the source is untouched, which is the half a remap exists to protect.
        assert (
            await async_db.execute(select(Dive.uuid).where(Dive.user_id == source_user.id))
        ).scalars().all() == list(source_uuids)


class TestIdempotence:
    """Importing a document twice creates nothing the second time - **where uuids can
    carry that**, which is the account that owns them."""

    @pytest.mark.asyncio
    async def test_importing_a_logbook_into_its_own_account_creates_nothing(
        self, seeded: Any, async_db: AsyncSession
    ) -> None:
        user, document = seeded
        before = len((await async_db.execute(select(Dive.id).where(Dive.user_id == user.id))).scalars().all())

        plan = await _apply(async_db, user.id, document)

        counts = _counts(plan)
        assert counts["dives"] == (0, 1, 0, 0)
        assert counts["sites"] == (0, 2, 0, 0)
        assert all(created == 0 for created, _, _, _ in counts.values())
        after = len((await async_db.execute(select(Dive.id).where(Dive.user_id == user.id))).scalars().all())
        assert after == before

    @pytest.mark.asyncio
    async def test_a_cross_account_import_run_twice_duplicates_and_the_preview_says_so(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The branch where idempotence is impossible by construction, pinned so that the
        honest guard - a preview that reports the full creation set again - cannot quietly
        become a wrong claim."""
        _, document = seeded
        destination = create_user(db)
        await _apply(async_db, destination.id, document)

        second = await _preview(async_db, destination.id, document)

        assert _counts(second)["dives"] == (1, 0, 0, 0)


class TestRestore:
    """A soft-deleted row of the caller's comes back, under its own uuid.

    The application's only un-delete path, and the one branch a `is_deleted=False` lookup
    would break in the worst way: it would call the husk's uuid unclaimed, create against
    it, and hit the full unique index.
    """

    @pytest.mark.asyncio
    async def test_a_deleted_dive_is_restored_rather_than_recreated(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        user, document = seeded
        dive = db.query(Dive).filter(Dive.user_id == user.id).one()
        original_id, original_uuid = dive.id, dive.uuid
        dive.is_deleted = True
        dive.deleted_at = datetime.now(UTC)
        dive.notes = "edited after deletion"
        db.commit()

        plan = await _apply(async_db, user.id, document)

        assert _counts(plan)["dives"] == (0, 0, 1, 0)
        assert ImportNoteCode.RECORD_RESTORED in _codes(plan)
        restored = (await async_db.execute(select(Dive).where(Dive.uuid == original_uuid))).scalars().one()
        assert restored.id == original_id
        assert restored.is_deleted is False
        assert restored.deleted_at is None
        # Wholesale: the husk keeps nothing but its identity.
        assert restored.notes != "edited after deletion"

    @pytest.mark.asyncio
    async def test_a_restored_dive_gets_its_children_back(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        user, document = seeded
        dive = db.query(Dive).filter(Dive.user_id == user.id).one()
        db.query(DiveMixture).filter(DiveMixture.dive_id == dive.id).delete()
        dive.is_deleted = True
        dive.deleted_at = datetime.now(UTC)
        db.commit()

        await _apply(async_db, user.id, document)

        mixtures = (await async_db.execute(select(DiveMixture).where(DiveMixture.dive_id == dive.id))).scalars().all()
        assert len(mixtures) == 1
        assert mixtures[0].oxygen == 32.0

    @pytest.mark.asyncio
    async def test_a_deleted_certification_and_service_record_restore_too(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The branch covers every importable soft-deleting table, not dives alone."""
        user, document = seeded
        for model in (Certification, GearServiceRecord):
            db.query(model).filter(model.user_id == user.id).update(
                {"is_deleted": True, "deleted_at": datetime.now(UTC)}
            )
        db.commit()

        plan = await _apply(async_db, user.id, document)

        counts = _counts(plan)
        assert counts["certifications"] == (0, 0, 1, 0)
        assert counts["gear_service_records"] == (0, 0, 1, 0)


class TestUniquenessNeverFailsAnImport:
    """No uniqueness rule anywhere 500s an import - the caller's existing row wins and the
    document's record links to it."""

    @pytest.mark.asyncio
    async def test_a_site_of_the_same_name_links_rather_than_duplicating(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        destination = create_user(db)
        original = parse_document(document)
        clash = original["sites"][0]
        db.add(DiveSite(user_id=destination.id, name=clash["name"], location=clash.get("location"), notes=""))
        db.commit()

        plan = await _apply(async_db, destination.id, document)

        created, linked, _, _ = _counts(plan)["sites"]
        assert (created, linked) == (1, 1)
        assert (
            len((await async_db.execute(select(DiveSite.id).where(DiveSite.user_id == destination.id))).scalars().all())
            == 2
        )

    @pytest.mark.asyncio
    async def test_a_dive_referencing_a_linked_site_reaches_the_existing_row(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        destination = create_user(db)
        original = parse_document(document)
        clash = original["sites"][0]
        existing = DiveSite(user_id=destination.id, name=clash["name"], location=clash.get("location"), notes="")
        db.add(existing)
        db.commit()
        db.refresh(existing)

        await _apply(async_db, destination.id, document)

        dive = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        linked_ids = set(
            (await async_db.execute(select(DiveDiveSite.dive_site_id).where(DiveDiveSite.dive_id == dive.id))).scalars()
        )
        assert existing.id in linked_ids

    @pytest.mark.asyncio
    async def test_two_records_of_one_document_with_one_name_become_one_row(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        destination = create_user(db)
        parsed = json.loads(document)
        twin = dict(parsed["sites"][0])
        twin["uuid"] = str(uuid7())
        parsed["sites"].append(twin)
        parsed["dives"][0]["site_uuids"].append(twin["uuid"])

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        created, linked, _, _ = _counts(plan)["sites"]
        assert (created, linked) == (2, 1)


class TestFilesFollowTheirBytes:
    """A file row's existence is this repo's claim that the bytes exist, so a bare document
    creates none and the archive is what puts them back."""

    @pytest.mark.asyncio
    async def test_a_bare_document_reports_its_files_and_creates_no_rows(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        user = _seed_logbook(db)
        dive = db.query(Dive).filter(Dive.user_id == user.id).one()
        db.add(
            DiveFile(
                user_id=user.id,
                recording_id=create_dive_recording(db, user, dive).id,
                dive_id=dive.id,
                sha256="a" * 64,
                content_type="application/json",
                byte_size=3,
                original_filename="dive.json",
                parser_key="suunto_json",
                storage_key=f"dive-files/{uuid7().hex}",
            )
        )
        db.commit()
        document = await _export(async_db, user.id)
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, document)

        assert plan.files_referenced == 1
        assert plan.files_not_contained == 1
        assert plan.files_restored == 0
        assert ImportNoteCode.FILE_NOT_CONTAINED in _codes(plan)
        assert (
            not (await async_db.execute(select(DiveFile.id).where(DiveFile.user_id == destination.id))).scalars().all()
        )

    @pytest.mark.asyncio
    async def test_an_archive_restores_the_bytes_and_verifies_their_digest(
        self, db: Session, async_db: AsyncSession, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from src.app.services import blob_store

        monkeypatch.setattr(blob_store, "storage_root", lambda: tmp_path)
        user = _seed_logbook(db)
        dive = db.query(Dive).filter(Dive.user_id == user.id).one()
        payload = b'{"hello": "dive"}'
        digest = hashlib.sha256(payload).hexdigest()
        key = blob_store.new_key("dive-files", sha256=digest)
        await blob_store.put(key, payload)
        db.add(
            DiveFile(
                user_id=user.id,
                recording_id=create_dive_recording(db, user, dive).id,
                dive_id=dive.id,
                sha256=digest,
                content_type="application/json",
                byte_size=len(payload),
                original_filename="dive.json",
                parser_key="suunto_json",
                storage_key=key,
            )
        )
        db.commit()

        archive = _zip_of(await _export(async_db, user.id, archive_paths=True), {})
        member = parse_document(_document_of(archive))["dives"][0]["recordings"][0]["source_files"][0]["archive_path"]
        archive = _zip_of(await _export(async_db, user.id, archive_paths=True), {member: payload})
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, archive, filename="logbook.zip")

        assert plan.files_restored == 1
        stored = (await async_db.execute(select(DiveFile).where(DiveFile.user_id == destination.id))).scalars().one()
        assert stored.sha256 == digest
        assert await blob_store.get(stored.storage_key) == payload

    @pytest.mark.asyncio
    async def test_bytes_that_do_not_match_their_digest_are_skipped(
        self, db: Session, async_db: AsyncSession, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from src.app.services import blob_store

        monkeypatch.setattr(blob_store, "storage_root", lambda: tmp_path)
        user = _seed_logbook(db)
        dive = db.query(Dive).filter(Dive.user_id == user.id).one()
        payload = b'{"hello": "dive"}'
        digest = hashlib.sha256(payload).hexdigest()
        key = blob_store.new_key("dive-files", sha256=digest)
        await blob_store.put(key, payload)
        db.add(
            DiveFile(
                user_id=user.id,
                recording_id=create_dive_recording(db, user, dive).id,
                dive_id=dive.id,
                sha256=digest,
                content_type="application/json",
                byte_size=len(payload),
                original_filename="dive.json",
                parser_key="suunto_json",
                storage_key=key,
            )
        )
        db.commit()
        document = await _export(async_db, user.id, archive_paths=True)
        member = parse_document(document)["dives"][0]["recordings"][0]["source_files"][0]["archive_path"]
        archive = _zip_of(document, {member: b"not the bytes the manifest names"})
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, archive, filename="logbook.zip")

        assert plan.files_restored == 0
        assert plan.files_skipped == 1
        assert ImportNoteCode.FILE_SKIPPED in _codes(plan)
        assert (
            not (await async_db.execute(select(DiveFile.id).where(DiveFile.user_id == destination.id))).scalars().all()
        )


def _zip_of(document: bytes, members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(DIVEJSON_NAME, document)
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _document_of(archive: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        return opened.read(DIVEJSON_NAME)


class TestProfiles:
    """A dive imported with a profile serves the profile the source served."""

    @pytest.mark.asyncio
    async def test_the_samples_and_the_span_survive(self, db: Session, async_db: AsyncSession) -> None:
        user = _seed_logbook(db)
        dive = db.query(Dive).filter(Dive.user_id == user.id).one()
        db.add(
            DiveProfile(
                recording_id=create_dive_recording(db, user, dive).id,
                dive_id=dive.id,
                source_sha256="b" * 64,
                parser_key="suunto_json",
                extractor_version=3,
                duration=2400,
                depth_sample_count=3,
                data={
                    "depth": {"t": [0, 10, 20], "v": [100, 500, 300]},
                    # A marker past the last sample, which spec §6.4 blesses and which the
                    # app produces by design - the case that made the spec move.
                    "events": [{"t": 2430, "type": "bookmark"}],
                },
            )
        )
        db.commit()
        document = await _export(async_db, user.id)
        destination = create_user(db)

        await _apply(async_db, destination.id, document)

        imported_dive = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        profile = (
            (await async_db.execute(select(DiveProfile).where(DiveProfile.dive_id == imported_dive.id))).scalars().one()
        )
        assert profile.duration == 2400
        assert profile.depth_sample_count == 3
        assert profile.event_count == 1
        assert profile.parser_key == "divejson_import"

    @pytest.mark.asyncio
    async def test_a_channel_whose_times_go_backwards_is_dropped_and_the_dive_is_not(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["recordings"] = [
            {"profile": {"duration": 30, "depth": {"times": [0, 20, 10], "values": [1, 2, 3]}}}
        ]
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        assert ImportNoteCode.VALUE_DROPPED in _codes(plan)
        imported = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert (
            not (await async_db.execute(select(DiveProfile.id).where(DiveProfile.dive_id == imported.id)))
            .scalars()
            .all()
        )


class TestSpeciesLinks:
    """The catalog is global and ownerless, so a sighting is the one reference an import can
    lose without losing the dive - and the one whose *preview* has to say something the
    apply will agree with."""

    @pytest.mark.asyncio
    async def test_a_species_already_in_the_catalog_links_by_aphia_id(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """Never by uuid: a uuid means nothing across instances, an AphiaID names the same
        animal everywhere (spec §6.11). The destination gets the *catalog's* row, whatever
        uuid the document called it."""
        _, document = seeded
        parsed = json.loads(document)
        aphia_id = parsed["species"][0]["aphia_id"]
        parsed["species"][0]["uuid"] = str(uuid7())
        parsed["dives"][0]["species_uuids"] = [parsed["species"][0]["uuid"]]
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["species"] == (0, 1, 0, 0)
        catalog_id = (await async_db.execute(select(Species.id).where(Species.aphia_id == aphia_id))).scalars().one()
        dive = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        linked = set(
            (await async_db.execute(select(DiveSpecies.species_id).where(DiveSpecies.dive_id == dive.id))).scalars()
        )
        assert linked == {catalog_id}

    @pytest.mark.asyncio
    async def test_a_preview_does_not_claim_a_pending_lookup_was_dropped(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The preview counts an unknown AphiaID as one it *will* look up. A per-dive note
        saying the sighting "was not imported" would contradict that in the same report -
        and would spend one note per dive against the cap for a logbook full of new
        species."""
        _, document = seeded
        parsed = json.loads(document)
        parsed["species"][0]["aphia_id"] = 900_000_000 + int(uuid7().hex[-6:], 16)
        destination = create_user(db)

        plan = await _preview(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["species"] == (1, 0, 0, 0)
        assert not [
            note for note in plan.notes if note.collection == "dives" and note.code is ImportNoteCode.SPECIES_UNRESOLVED
        ]

    @pytest.mark.asyncio
    async def test_an_unresolvable_species_drops_the_sighting_and_keeps_the_dive(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The apply's answer, where the lookup really has run and come back empty."""
        _, document = seeded
        parsed = json.loads(document)
        parsed["species"][0]["aphia_id"] = 900_000_000 + int(uuid7().hex[-6:], 16)
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        assert _counts(plan)["species"] == (0, 0, 0, 1)
        assert ImportNoteCode.SPECIES_UNRESOLVED in _codes(plan)
        dive = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert (
            not (await async_db.execute(select(DiveSpecies.species_id).where(DiveSpecies.dive_id == dive.id)))
            .scalars()
            .all()
        )


class TestTwoSchedulesOnOneNewGearItem:
    """The uniqueness case the existing-row lookup cannot see.

    `ux_gear_service_schedule_item_kind_label` is keyed on `gear_item_id`, which a gear item
    this import is *creating* does not have yet - so the existing-row half of the dedupe has
    nothing to look in. Skipping the within-document half along with it let two schedules of
    one document reach the same index and take the whole import down with an
    `IntegrityError`.
    """

    @pytest.mark.asyncio
    async def test_two_identical_schedules_become_one_row(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        twin = dict(parsed["gear_service_schedules"][0])
        twin["uuid"] = str(uuid7())
        parsed["gear_service_schedules"].append(twin)
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        created, linked, _, _ = _counts(plan)["gear_service_schedules"]
        assert (created, linked) == (1, 1)
        assert (
            len(
                (
                    await async_db.execute(
                        select(GearServiceSchedule.id).where(GearServiceSchedule.user_id == destination.id)
                    )
                )
                .scalars()
                .all()
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_two_duplicate_gear_items_collapsing_take_their_schedules_with_them(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The second shape: the gear items collapse through the alias branch, so two
        schedules that named different gear uuids end up under one row."""
        _, document = seeded
        parsed = json.loads(document)
        gear_twin = dict(parsed["gear"][0])
        gear_twin["uuid"] = str(uuid7())
        parsed["gear"].append(gear_twin)
        schedule_twin = dict(parsed["gear_service_schedules"][0])
        schedule_twin["uuid"] = str(uuid7())
        schedule_twin["gear_uuid"] = gear_twin["uuid"]
        parsed["gear_service_schedules"].append(schedule_twin)
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["gear"] == (1, 1, 0, 0)
        created, linked, _, _ = _counts(plan)["gear_service_schedules"]
        assert (created, linked) == (1, 1)

    @pytest.mark.asyncio
    async def test_two_gear_records_linking_to_one_existing_item_share_its_schedule_slot(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The third shape, and the one an alias key on the *document's* uuid misses.

        When the destination already owns a matching gear item, two document gear records
        both take `_claim_unique`'s existing-row branch - which returns a link with a row id
        and **no** `canonical_source_uuid`, so `_reference` hands back two different uuids
        for one row. Their schedules have to collapse on the row, not on the document.
        """
        _, document = seeded
        parsed = json.loads(document)
        original = parsed["gear"][0]
        destination = create_user(db)
        db.add(GearItem(user_id=destination.id, name=original["name"], brand=original.get("brand"), notes=""))
        db.commit()
        gear_twin = dict(original)
        gear_twin["uuid"] = str(uuid7())
        parsed["gear"].append(gear_twin)
        schedule_twin = dict(parsed["gear_service_schedules"][0])
        schedule_twin["uuid"] = str(uuid7())
        schedule_twin["gear_uuid"] = gear_twin["uuid"]
        parsed["gear_service_schedules"].append(schedule_twin)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["gear"] == (0, 2, 0, 0)
        created, linked, _, _ = _counts(plan)["gear_service_schedules"]
        assert (created, linked) == (1, 1)
        assert (
            len(
                (
                    await async_db.execute(
                        select(GearServiceSchedule.id).where(GearServiceSchedule.user_id == destination.id)
                    )
                )
                .scalars()
                .all()
            )
            == 1
        )


class TestTheOffsetUnknownState:
    """The one state only this endpoint can create, and the one no other suite covers.

    A dive whose source recorded no offset stores its wall clock with a NULL
    `utc_offset_minutes` and reads back offset-less - not converted, not stamped with
    anything. Round-tripped through the real writer, because the failure this pins is
    silent: a fabricated offset looks exactly like a real one.

    Creating the state is still this endpoint's alone, and the last three tests are the
    boundary of that: a dive that already has none keeps it through an edit of its own wall
    clock, one that has an offset cannot be stripped of it, and a create still refuses an
    offsetless value outright.
    """

    @pytest.mark.asyncio
    async def test_an_offset_less_start_time_round_trips_unchanged(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["started_at"] = "2026-04-17T11:49:23"
        destination = create_user(db)

        await _apply(async_db, destination.id, json.dumps(parsed).encode())

        stored = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert stored.utc_offset_minutes is None
        assert stored.start_time.replace(tzinfo=None) == datetime(2026, 4, 17, 11, 49, 23)

        re_exported = parse_document(await _export(async_db, destination.id))
        assert re_exported["dives"][0]["started_at"] == "2026-04-17T11:49:23"
        issues = divejson.validate_document(re_exported)
        assert not issues, [str(issue) for issue in issues]

    @pytest.mark.asyncio
    async def test_the_column_readers_do_not_fault_on_it(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """`dive_activity` and the species life list read `utc_offset_minutes` directly
        rather than through `combine_start_time`, so a NULL surfaces there as a wrong
        calendar day or an aggregate fault rather than as a visible wrong offset."""
        from src.app.services.dive_activity import dive_activity
        from src.app.services.species_life_list import species_life_list

        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["started_at"] = "2026-04-17T23:49:23"
        destination = create_user(db)
        await _apply(async_db, destination.id, json.dumps(parsed).encode())

        activity = await dive_activity(async_db, destination.id)

        assert [(point.year, point.month, point.day) for point in activity] == [(2026, 4, 17)]
        # The life list is the sharper of the two: it aggregates the offset column through
        # `array_agg(..., type_=ARRAY(Integer))[1]`, so it needs a real sighting on a real
        # offset-less dive to exercise the NULL at all - and `_seed_logbook` gives it one.
        life_list = await species_life_list(async_db, user_id=destination.id, offset=0, limit=10)
        assert life_list["total_count"] == 1
        entry = life_list["data"][0]
        assert entry["first_seen"].utcoffset() is None
        assert entry["first_seen"].hour == 23

    @pytest.mark.asyncio
    async def test_the_write_api_still_demands_an_offset_to_create_a_dive(self) -> None:
        """The requirement became a write-side rule, not a deleted one - and creating is
        still the half where it is absolute, which is what keeps import the only origin of
        the state."""
        from pydantic import ValidationError

        from src.app.schemas.dive import DiveCreate, DiveRead

        with pytest.raises(ValidationError):
            DiveCreate(dive_number=1, start_time=datetime(2026, 4, 17, 11, 49), duration=60)
        # And the read shape serves what is stored.
        assert DiveRead.model_fields["start_time"].annotation is datetime

    @pytest.mark.asyncio
    async def test_the_write_api_takes_back_the_offsetless_value_it_exported(
        self, seeded: Any, db: Session, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The round trip does not close at the export: a value this app writes into its
        own document has to be one its own dive-write API accepts, or the
        reference-implementation claim is only true one way. So `PATCH /dive/{uuid}` takes
        the offsetless `started_at` back, leaves the offset NULL, and the re-export still
        carries the corrected wall clock with no offset on it.
        """
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["started_at"] = "2026-04-17T11:49:23"
        destination = create_user(db)
        await _apply(async_db, destination.id, json.dumps(parsed).encode())
        stored = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()

        await _patch_start_time(async_db, monkeypatch, destination, stored.uuid, "2026-04-17T12:15:00")

        await async_db.refresh(stored)
        assert stored.utc_offset_minutes is None
        assert stored.start_time.replace(tzinfo=None) == datetime(2026, 4, 17, 12, 15, 0)
        re_exported = parse_document(await _export(async_db, destination.id))
        assert re_exported["dives"][0]["started_at"] == "2026-04-17T12:15:00"

    @pytest.mark.asyncio
    async def test_an_update_may_not_take_the_offset_off_a_dive_that_has_one(
        self, seeded: Any, db: Session, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of the asymmetry, and the reason it is one: were an edit allowed
        to drop an offset, editing would be a second way to bring the unknown state into
        existence and this endpoint would stop being its only origin."""
        _, document = seeded
        destination = create_user(db)
        await _apply(async_db, destination.id, document)
        stored = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert stored.utc_offset_minutes is not None

        with pytest.raises(UnprocessableEntityException, match="already unknown"):
            await _patch_start_time(async_db, monkeypatch, destination, stored.uuid, "2026-04-17T12:15:00")

        await async_db.refresh(stored)
        assert stored.utc_offset_minutes is not None


class TestTheReaderRefusesOnlyWhatItMust:
    """415 is "not a document I implement", 422 is "one that is broken", and everything
    readable imports."""

    @pytest.mark.asyncio
    async def test_a_foreign_json_document_is_unsupported(self) -> None:
        with pytest.raises(UnsupportedImportError):
            await load_import(_upload(b'{"format": "uddf", "version": "3.2.2"}'))

    @pytest.mark.asyncio
    async def test_a_later_major_version_is_unsupported(self) -> None:
        raw = b'{"format": "divejson", "version": "2.0", "exported_at": "2026-01-01T00:00:00Z"}'
        with pytest.raises(UnsupportedImportError):
            await load_import(_upload(raw))

    @pytest.mark.asyncio
    async def test_a_later_minor_version_is_read(self, seeded: Any) -> None:
        """§7 makes minor versions additive and §5.6 makes a reader ignore what it does not
        know, so a `1.9` document with an unheard-of member imports as far as 1.0 defines."""
        _, document = seeded
        parsed = json.loads(document)
        parsed["version"] = "1.9"
        parsed["dives"][0]["moon_phase"] = "waxing"
        parsed["extensions"] = {"com.example.divekit": {"mood": "great"}}

        with await load_import(_upload(json.dumps(parsed).encode())) as loaded:
            assert loaded.document.version == "1.9"
            assert loaded.document.extensions == {"com.example.divekit": {"mood": "great"}}

    @pytest.mark.asyncio
    async def test_a_duplicate_member_is_refused(self) -> None:
        raw = b'{"format": "divejson", "version": "1.0", "exported_at": "2026-01-01T00:00:00Z", "exported_at": "x"}'
        with pytest.raises(DuplicateMemberError):
            await load_import(_upload(raw))

    @pytest.mark.asyncio
    async def test_members_out_of_order_are_still_read(self) -> None:
        """§4's ordering is a writer's obligation and `divejson validate` is where it is
        enforced. A reader refusing an otherwise readable logbook over the order two
        members were written in would be doing the thing this feature exists to end."""
        raw = b'{"exported_at": "2026-01-01T00:00:00Z", "format": "divejson", "version": "1.0"}'
        with await load_import(_upload(raw)) as loaded:
            assert loaded.document.format == "divejson"

    @pytest.mark.asyncio
    async def test_a_document_past_the_cap_is_refused(self, monkeypatch: Any) -> None:
        from src.app.services.logbook_import import reader

        monkeypatch.setattr(reader, "MAX_ARCHIVE_SIZE", 32)
        with pytest.raises(ImportTooLargeError):
            await load_import(_upload(b'{"format": "divejson", "version": "1.0"}' + b" " * 64))

    @pytest.mark.asyncio
    async def test_an_archive_with_no_logbook_member_is_unsupported(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "not a logbook")
        with pytest.raises(UnsupportedImportError):
            await load_import(_upload(buffer.getvalue(), "logbook.zip"))

    @pytest.mark.asyncio
    async def test_a_broken_document_is_malformed(self) -> None:
        with pytest.raises(MalformedImportError):
            await load_import(_upload(b'{"format": "divejson", "version": "1.0", "dives": "not a list"}'))


class TestNothingInventedNothingFatal:
    @pytest.mark.asyncio
    async def test_a_value_the_database_refuses_is_dropped_and_the_dive_imports(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["surface_pressure"] = 42.0
        parsed["dives"][0]["altitude"] = 99999
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        assert ImportNoteCode.VALUE_DROPPED in _codes(plan)
        stored = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert stored.surface_pressure_bar is None
        assert stored.altitude is None

    @pytest.mark.asyncio
    async def test_the_widened_surface_pressure_floor_is_storable(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """0.44 bar is ambient pressure at `ck_dive_altitude_range`'s own 6500 m ceiling,
        and the old 0.5 floor refused it."""
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["surface_pressure"] = 0.44
        destination = create_user(db)

        await _apply(async_db, destination.id, json.dumps(parsed).encode())

        stored = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert stored.surface_pressure_bar == 0.44

    @pytest.mark.asyncio
    async def test_a_dive_with_no_duration_and_no_profile_is_skipped_not_invented(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        del parsed["dives"][0]["duration"]
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (0, 0, 0, 1)
        assert ImportNoteCode.RECORD_SKIPPED in _codes(plan)

    @pytest.mark.asyncio
    async def test_a_cylinder_with_no_mix_is_stored_with_its_mix_absent(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """§6.3: absent `oxygen` means not recorded, not 21 - and divers plan gas off these
        numbers, so nothing is assumed. The cylinder itself is real either way, which is
        why it is now stored with the member missing rather than skipped: dropping it lost
        the pressures and the gas number the document *did* carry.

        The inverse of a test that asserted the skip. `dive_mixture.oxygen` was `NOT NULL`
        when it was written, and the skip was the honest answer while it was.
        """
        _, document = seeded
        parsed = json.loads(document)
        del parsed["dives"][0]["cylinders"][0]["oxygen"]
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        assert ImportNoteCode.RECORD_SKIPPED not in _codes(plan)
        dive = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        stored = (await async_db.execute(select(DiveMixture).where(DiveMixture.dive_id == dive.id))).scalars().all()
        assert [mixture.oxygen for mixture in stored] == [None]
        assert [mixture.volume for mixture in stored] == [parsed["dives"][0]["cylinders"][0]["volume"]]

    @pytest.mark.asyncio
    async def test_a_cylinder_with_no_volume_is_stored_with_its_size_absent(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The mix-only cylinder: the shape a UDDF `<tankdata>` with a gas link and no
        `<tankvolume>` converts to, and the one this app could not hold at all."""
        _, document = seeded
        parsed = json.loads(document)
        del parsed["dives"][0]["cylinders"][0]["volume"]
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        assert ImportNoteCode.RECORD_SKIPPED not in _codes(plan)
        dive = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        stored = (await async_db.execute(select(DiveMixture).where(DiveMixture.dive_id == dive.id))).scalars().one()
        assert stored.volume is None
        assert stored.oxygen == parsed["dives"][0]["cylinders"][0]["oxygen"]

    @pytest.mark.asyncio
    async def test_a_cylinder_size_outside_what_this_app_stores_goes_without_the_cylinder(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """A *recorded* member the app cannot hold is still dropped - what changed is that
        the rest of the cylinder no longer goes with it."""
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["cylinders"][0]["volume"] = -3.0
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert ImportNoteCode.VALUE_DROPPED in _codes(plan)
        dive = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        stored = (await async_db.execute(select(DiveMixture).where(DiveMixture.dive_id == dive.id))).scalars().one()
        assert stored.volume is None
        assert stored.oxygen == parsed["dives"][0]["cylinders"][0]["oxygen"]

    @pytest.mark.asyncio
    async def test_a_mix_adding_past_100_percent_loses_both_fractions_and_keeps_the_cylinder(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """Neither fraction says which of them is wrong, so both go - the same answer the
        pressure pair beside it has always given, now that there is somewhere to put it."""
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["cylinders"][0]["oxygen"] = 60.0
        parsed["dives"][0]["cylinders"][0]["helium"] = 50.0
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert ImportNoteCode.VALUE_DROPPED in _codes(plan)
        dive = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        stored = (await async_db.execute(select(DiveMixture).where(DiveMixture.dive_id == dive.id))).scalars().one()
        assert (stored.oxygen, stored.helium) == (None, None)
        assert stored.volume == parsed["dives"][0]["cylinders"][0]["volume"]

    @pytest.mark.asyncio
    async def test_an_unknown_gear_type_reads_as_absent(self, seeded: Any, db: Session, async_db: AsyncSession) -> None:
        _, document = seeded
        parsed = json.loads(document)
        parsed["gear"][0]["type"] = "air_horn"
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["gear"] == (1, 0, 0, 0)
        item = (await async_db.execute(select(GearItem).where(GearItem.user_id == destination.id))).scalars().one()
        assert item.type is None

    @pytest.mark.asyncio
    async def test_a_dangling_reference_is_reported_and_the_dive_imports(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["trip_uuid"] = str(uuid7())
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        assert ImportNoteCode.REFERENCE_UNRESOLVED in _codes(plan)
        stored = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert stored.trip_id is None

    @pytest.mark.asyncio
    async def test_a_null_where_a_collection_belongs_reads_as_empty(self) -> None:
        raw = b'{"format": "divejson", "version": "1.0", "exported_at": "2026-01-01T00:00:00Z", "dives": null}'
        with await load_import(_upload(raw)) as loaded:
            assert loaded.document.dives == []


class TestDerivedState:
    @pytest.mark.asyncio
    async def test_the_dashboard_totals_and_gear_counts_are_recomputed(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        from src.app.models.user_dive_stats import UserDiveStats

        _, document = seeded
        destination = create_user(db)

        await _apply(async_db, destination.id, document)

        stats = (
            (await async_db.execute(select(UserDiveStats).where(UserDiveStats.user_id == destination.id)))
            .scalars()
            .one()
        )
        assert stats.total_dives == 1
        item = (await async_db.execute(select(GearItem).where(GearItem.user_id == destination.id))).scalars().one()
        assert item.dive_count == 1

    @pytest.mark.asyncio
    async def test_a_schedules_due_dates_come_from_the_imported_records(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        destination = create_user(db)

        await _apply(async_db, destination.id, document)

        schedule = (
            (await async_db.execute(select(GearServiceSchedule).where(GearServiceSchedule.user_id == destination.id)))
            .scalars()
            .one()
        )
        record = (
            (await async_db.execute(select(GearServiceRecord).where(GearServiceRecord.user_id == destination.id)))
            .scalars()
            .one()
        )
        assert schedule.last_service_on == record.serviced_on

    @pytest.mark.asyncio
    async def test_the_snapshots_import_as_recorded(self, seeded: Any, db: Session, async_db: AsyncSession) -> None:
        """`dive_count_at_start` and `dive_count_at_service` are historical facts with no
        recomputation procedure (spec §5.7), so recomputing them against the destination's
        live counters is the reset-every-baseline failure the distinction exists to
        prevent."""
        user, document = seeded
        source_record = db.query(GearServiceRecord).filter(GearServiceRecord.user_id == user.id).one()
        destination = create_user(db)

        await _apply(async_db, destination.id, document)

        imported = (
            (await async_db.execute(select(GearServiceRecord).where(GearServiceRecord.user_id == destination.id)))
            .scalars()
            .one()
        )
        assert imported.dive_count_at_service == source_record.dive_count_at_service


class TestTwoRecordsClaimingOneIdentifier:
    """A uuid used twice in one collection is two records, and the second is remapped.

    Not conforming - §5.3 makes every uuid in a document unique and the reference corpus
    carries an invalid fixture for it - which is exactly why it gets a reader's answer. The
    failure it replaces is the one shape of loss this module has no other route to: the
    records are accumulated into a dict keyed on the document's uuid, so without the remap
    the second silently overwrote the first, and the first was never written, never counted
    and never mentioned.
    """

    @pytest.mark.asyncio
    async def test_both_records_arrive_and_the_counts_still_add_up(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        twin = dict(parsed["dives"][0])
        twin["dive_number"] = 99
        twin["started_at"] = "2027-06-01T09:00:00+00:00"
        parsed["dives"].append(twin)
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        created, linked, restored, skipped = _counts(plan)["dives"]
        assert created + linked + restored + skipped == len(parsed["dives"])
        assert created == 2
        assert ImportNoteCode.RECORD_REMAPPED_REFERENCES_STAY in _codes(plan)
        numbers = sorted(
            (await async_db.execute(select(Dive.dive_number).where(Dive.user_id == destination.id))).scalars()
        )
        assert numbers == sorted([parsed["dives"][0]["dive_number"], 99])

    @pytest.mark.asyncio
    async def test_a_skippable_twin_does_not_take_the_good_record_with_it(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The worse variant: overwriting discarded a complete dive and reported one skip,
        under the *bad* copy's reason."""
        _, document = seeded
        parsed = json.loads(document)
        twin = dict(parsed["dives"][0])
        del twin["started_at"]
        parsed["dives"].append(twin)
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (1, 0, 0, 1)
        assert len((await async_db.execute(select(Dive.id).where(Dive.user_id == destination.id))).scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_the_duplicate_is_the_only_remap_when_the_account_owns_the_identifiers(
        self, seeded: Any, async_db: AsyncSession
    ) -> None:
        """The duplicate-identifier cause, isolated.

        The test above imports into a *second* account, where every uuid is somebody
        else's, so both remap codes fire and neither is pinned by the other's absence.
        Importing into the owning account takes the link branch for every record the
        document already has here, leaving the twin's remap the only one in the report.
        """
        user, document = seeded
        parsed = json.loads(document)
        twin = dict(parsed["dives"][0])
        twin["dive_number"] = 99
        twin["started_at"] = "2027-06-01T09:00:00+00:00"
        parsed["dives"].append(twin)

        plan = await _apply(async_db, user.id, json.dumps(parsed).encode())

        codes = _codes(plan)
        assert ImportNoteCode.RECORD_REMAPPED_REFERENCES_STAY in codes
        assert ImportNoteCode.RECORD_REMAPPED_REFERENCES_FOLLOW not in codes
        assert _counts(plan)["dives"] == (1, 1, 0, 0)
        stayed = [note for note in plan.notes if note.code is ImportNoteCode.RECORD_REMAPPED_REFERENCES_STAY]
        assert len(stayed) == 1
        assert stayed[0].collection == "dives"
        assert stayed[0].uuid == uuid_pkg.UUID(twin["uuid"])

    @pytest.mark.asyncio
    async def test_a_reference_to_the_duplicated_identifier_reaches_the_first_record(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """What the code's name promises, on a collection something actually points at.

        The dive's `trip_uuid` is the duplicated identifier and nothing rewrites it, so it
        has to land on the trip the document defined first while the twin arrives beside it
        under an identifier no reference reaches. Distinct names on purpose: two trips
        sharing one would be the name-dedupe branch instead, which links rather than remaps.
        """
        _, document = seeded
        parsed = json.loads(document)
        twin = dict(parsed["trips"][0])
        twin["name"] = "The twin under a claimed identifier"
        parsed["trips"].append(twin)
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert ImportNoteCode.RECORD_REMAPPED_REFERENCES_STAY in _codes(plan)
        assert _counts(plan)["trips"] == (2, 0, 0, 0)
        trips = {
            trip.name: trip.id
            for trip in (await async_db.execute(select(Trip).where(Trip.user_id == destination.id))).scalars()
        }
        dive = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert dive.trip_id == trips[parsed["trips"][0]["name"]]
        assert dive.trip_id != trips[twin["name"]]


class TestAServiceRecordOnAScheduleTheImportDidNotWrite:
    """`recalculate_service_schedule` has to run for every schedule the import *touched*.

    A record landing on a rule the caller already had moves that rule's dates exactly as one
    landing on a rule this import created does - `api/v1/gear_service.py` recalculates on
    every record create, update and delete for that reason. Collecting only the schedules
    the import wrote left a linked one carrying a stale `last_service_on`, stale `next_due_*`
    and stale notify state that would go on suppressing a reminder it had already earned.
    """

    @pytest.mark.asyncio
    async def test_a_linked_schedules_due_dates_follow_the_imported_record(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        user, document = seeded
        source_item = db.query(GearItem).filter(GearItem.user_id == user.id).one()
        source_schedule = db.query(GearServiceSchedule).filter(GearServiceSchedule.user_id == user.id).one()
        source_record = db.query(GearServiceRecord).filter(GearServiceRecord.user_id == user.id).one()
        # The destination already owns the same gear item and the same rule - under its own
        # identifiers, so both link through their user-scoped unique indexes rather than
        # through the document's uuids - and has never had it serviced.
        destination = create_user(db)
        item = GearItem(user_id=destination.id, name=source_item.name, brand=source_item.brand, notes="")
        db.add(item)
        db.commit()
        db.refresh(item)
        existing = GearServiceSchedule(
            user_id=destination.id,
            gear_item_id=item.id,
            kind=source_schedule.kind,
            starts_on=source_schedule.starts_on,
            interval_months=source_schedule.interval_months,
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)
        assert existing.last_service_on is None

        plan = await _apply(async_db, destination.id, document)

        assert _counts(plan)["gear_service_schedules"] == (0, 1, 0, 0)
        assert _counts(plan)["gear_service_records"] == (1, 0, 0, 0)
        refreshed = (
            (await async_db.execute(select(GearServiceSchedule).where(GearServiceSchedule.id == existing.id)))
            .scalars()
            .one()
        )
        assert refreshed.last_service_on == source_record.serviced_on
        assert refreshed.next_due_on is not None


class TestTheDiverIsNeverApplied:
    @pytest.mark.asyncio
    async def test_the_destination_keeps_its_own_identity_and_settings(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        from src.app.models.user import User

        _, document = seeded
        destination = create_user(db)
        before = (
            destination.name,
            destination.username,
            destination.email,
            destination.units,
            list(destination.dive_form_hidden_fields),
        )

        plan = await _apply(async_db, destination.id, document)

        assert ImportNoteCode.DIVER_NOT_APPLIED in _codes(plan)
        after = (await async_db.execute(select(User).where(User.id == destination.id))).scalars().one()
        assert (
            after.name,
            after.username,
            after.email,
            after.units,
            list(after.dive_form_hidden_fields),
        ) == before

    @pytest.mark.asyncio
    async def test_the_destination_gains_none_of_the_documents_dive_form_presets(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """The presets ride in the `diver` member's extension, and that member is read,
        reported and never applied - so a restore does not hand this account somebody else's
        idea of which fields to hide. The note says so in as many words; this is the half
        that checks the rows.
        """
        from src.app.models.dive_form_preset import DiveFormPreset

        _, document = seeded
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, document)

        assert ImportNoteCode.DIVER_NOT_APPLIED in _codes(plan)
        presets = (
            (await async_db.execute(select(DiveFormPreset).where(DiveFormPreset.user_id == destination.id)))
            .scalars()
            .all()
        )
        assert list(presets) == []


class TestTheBoundsCensus:
    """A guard, not a test of behaviour: a new single-column bound on `dive` or
    `dive_mixture` must not be able to land without an import-side counterpart.

    The same shape as
    `test_every_single_column_bound_a_parser_can_reach_has_a_parse_side_guard`, and for the
    same reason - a value the database refuses must not take the write it rode in on with
    it. Pair rules are excluded by name: there is no "the bad value" in a pair, so the
    planner handles those on their own terms.
    """

    PAIR_RULES = frozenset(
        {
            "ck_dive_avg_depth_within_max",
            "ck_dive_entry_position_pair",
            "ck_dive_exit_position_pair",
            "ck_dive_mixture_oxygen_helium_sum",
            "ck_dive_mixture_pressure_order",
        }
    )
    # There used to be a third exclusion here, for the three `dive_mixture` bounds whose
    # columns the record could not exist without - the planner skipped the whole cylinder
    # rather than dropping a value, so no `_MIXTURE_BOUNDS` entry could cover them. Those
    # columns are nullable now and each has its own bound, so the exclusion is gone and the
    # census covers them like everything else.
    #
    # Single-column bounds guarded by `_Planner._position` rather than by `_DIVE_BOUNDS`,
    # because a coordinate arrives as a Position object and goes as a pair: dropping half of
    # one would leave a dive pinned to the equator, which `ck_dive_*_position_pair` refuses
    # anyway. Named here rather than silently passing, so a fifth coordinate column would
    # still have to be accounted for.
    POSITION_RULES = frozenset(
        {
            "ck_dive_entry_latitude_range",
            "ck_dive_entry_longitude_range",
            "ck_dive_exit_latitude_range",
            "ck_dive_exit_longitude_range",
        }
    )

    def test_every_bound_the_document_can_reach_has_a_guard(self) -> None:
        guarded = {bound.field for bound in _DIVE_BOUNDS} | {bound.field for bound in _MIXTURE_BOUNDS}
        # Column names, mapped onto the wire names the planner reads them under.
        wire = {"surface_pressure_bar": "surface_pressure"}
        unguarded = []
        # `tuple[Any, ...]` because `Model.__table__` is typed `FromClause` on a precisely
        # typed class, and only `Table` carries `.constraints`.
        models: tuple[Any, ...] = (Dive, DiveMixture)
        for model in models:
            table = model.__table__
            for constraint in table.constraints:
                name = getattr(constraint, "name", None)
                excluded = self.PAIR_RULES | self.POSITION_RULES
                if not name or not name.startswith("ck_") or name in excluded:
                    continue
                sqltext = str(getattr(constraint, "sqltext", ""))
                columns = [column.name for column in table.columns if column.name in sqltext]
                if not any(wire.get(column, column) in guarded for column in columns):
                    unguarded.append(name)
        assert not unguarded, (
            "these `CheckConstraint`s have no import-side guard, so a document carrying one of their values "
            f"would fail the whole import instead of losing the value: {sorted(unguarded)}"
        )


class TestAnArchiveThatWillNotInflate:
    """`zipfile` raises three different exceptions for a member it cannot decompress, and
    none of them is a subclass of anything the route translates.

    The commonest by far is the first: a diver zips their export with a password and hands
    it over. Unhandled, that is a 500 from an endpoint whose whole contract is a 415/422/413
    taxonomy - and on the apply path it aborts a half-written import over one bad member.
    """

    @staticmethod
    def _encrypt_flags(archive: bytes) -> bytes:
        """Set the general-purpose "encrypted" bit in both headers of the first member.

        `writestr` overwrites `ZipInfo.flag_bits`, and `zipfile` cannot *write* an encrypted
        member at all, so the flag is patched into the bytes: offset 6 of the local file
        header and offset 8 of the central directory entry. What comes back is a real
        archive that `zipfile` refuses to extract, which is the shape under test.
        """
        raw = bytearray(archive)
        raw[raw.index(b"PK\x03\x04") + 6] |= 0x01
        raw[raw.index(b"PK\x01\x02") + 8] |= 0x01
        return bytes(raw)

    @staticmethod
    def _break_crc(archive: bytes, marker: bytes) -> bytes:
        raw = bytearray(archive)
        raw[raw.index(marker)] ^= 0xFF
        return bytes(raw)

    @pytest.mark.asyncio
    async def test_a_password_protected_archive_is_malformed_not_a_500(self, seeded: Any) -> None:
        _, document = seeded
        archive = self._encrypt_flags(_zip_of(document, {}))

        with pytest.raises(MalformedImportError) as caught:
            await load_import(_upload(archive, "logbook.zip"))

        assert "password-protected" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_corrupt_logbook_member_is_malformed_not_a_500(self, seeded: Any) -> None:
        _, document = seeded
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(DIVEJSON_NAME, document)

        with pytest.raises(MalformedImportError):
            await load_import(_upload(self._break_crc(buffer.getvalue(), b'{"format"'), "logbook.zip"))

    @pytest.mark.asyncio
    async def test_a_corrupt_blob_member_skips_the_file_and_keeps_the_dive(
        self, db: Session, async_db: AsyncSession, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from src.app.services import blob_store

        monkeypatch.setattr(blob_store, "storage_root", lambda: tmp_path)
        user = _seed_logbook(db)
        dive = db.query(Dive).filter(Dive.user_id == user.id).one()
        payload = b"AAAABBBBCCCCDDDD"
        digest = hashlib.sha256(payload).hexdigest()
        key = blob_store.new_key("dive-files", sha256=digest)
        await blob_store.put(key, payload)
        db.add(
            DiveFile(
                user_id=user.id,
                recording_id=create_dive_recording(db, user, dive).id,
                dive_id=dive.id,
                sha256=digest,
                content_type="application/json",
                byte_size=len(payload),
                original_filename="dive.json",
                parser_key="suunto_json",
                storage_key=key,
            )
        )
        db.commit()
        document = await _export(async_db, user.id, archive_paths=True)
        member = parse_document(document)["dives"][0]["recordings"][0]["source_files"][0]["archive_path"]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(DIVEJSON_NAME, document)
            archive.writestr(member, payload)
        destination = create_user(db)

        plan = await _apply(
            async_db, destination.id, self._break_crc(buffer.getvalue(), payload), filename="logbook.zip"
        )

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        assert plan.files_restored == 0
        assert plan.files_skipped == 1
        assert (
            not (await async_db.execute(select(DiveFile.id).where(DiveFile.user_id == destination.id))).scalars().all()
        )


class TestNumbersWiderThanTheColumn:
    """The format puts no ceiling on any of its integer members - `dive_number` is a bare
    `{"type": "integer"}` in the published schema - and `Integer` here is 32 bits.

    So a **conforming** document can carry a number this app's columns cannot, and a
    converter with a unit bug is exactly how one arrives. Unbounded, that is SQLSTATE 22003
    raised from inside the apply transaction: the whole logbook refused over one number,
    which is the failure every other bound in the planner exists to prevent.
    """

    HUGE = 2**31

    @pytest.mark.asyncio
    async def test_an_unstorable_dive_number_falls_back_to_the_placeholder(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["dive_number"] = self.HUGE
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        assert ImportNoteCode.VALUE_DROPPED in _codes(plan)
        stored = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert stored.dive_number == 0

    @pytest.mark.asyncio
    async def test_an_unstorable_duration_skips_the_dive_rather_than_the_logbook(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["duration"] = self.HUGE
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (0, 0, 0, 1)
        # And everything else in the document still arrived.
        assert _counts(plan)["sites"] == (2, 0, 0, 0)

    @pytest.mark.asyncio
    async def test_an_unstorable_profile_reading_drops_the_channel(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["recordings"] = [
            {"profile": {"duration": 30, "depth": {"times": [0, 10, 20], "values": [100, self.HUGE, 300]}}}
        ]
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        imported = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert (
            not (await async_db.execute(select(DiveProfile.id).where(DiveProfile.dive_id == imported.id)))
            .scalars()
            .all()
        )

    @pytest.mark.asyncio
    async def test_an_unstorable_snapshot_count_drops_to_zero(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        _, document = seeded
        parsed = json.loads(document)
        parsed["gear_service_records"][0]["dive_count_at_service"] = self.HUGE
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["gear_service_records"] == (1, 0, 0, 0)
        record = (
            (await async_db.execute(select(GearServiceRecord).where(GearServiceRecord.user_id == destination.id)))
            .scalars()
            .one()
        )
        assert record.dive_count_at_service == 0


class TestNumbersThatOnlyOverflowOnceAddedUp:
    """The half a per-column bound cannot reach: a *derived* column that sums the values.

    Both of these write inside the apply transaction, from code that knows nothing about
    import - so the failure is not "a value was refused" but "the whole logbook was", over
    arithmetic in a tile nobody was looking at. Each is closed at its inputs.
    """

    MAX_DURATION = 366 * 24 * 60 * 60
    MAX_COUNT = 1_000_000

    @pytest.mark.asyncio
    async def test_two_year_long_dives_sum_without_refusing_the_import(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """`user_dive_stats.total_time` is `SUM(dive.duration)` over the whole account, so
        two dives each inside `dive.duration`'s own bound overflowed a 32-bit column."""
        from src.app.models.user_dive_stats import UserDiveStats

        _, document = seeded
        parsed = json.loads(document)
        first = parsed["dives"][0]
        first["duration"] = self.MAX_DURATION
        second = dict(first)
        second["uuid"] = str(uuid7())
        second["started_at"] = "2027-06-01T09:00:00+00:00"
        parsed["dives"].append(second)
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (2, 0, 0, 0)
        stats = (
            (await async_db.execute(select(UserDiveStats).where(UserDiveStats.user_id == destination.id)))
            .scalars()
            .one()
        )
        assert stats.total_time == 2 * self.MAX_DURATION

    @pytest.mark.asyncio
    async def test_a_dive_longer_than_a_year_is_skipped(self, seeded: Any, db: Session, async_db: AsyncSession) -> None:
        _, document = seeded
        parsed = json.loads(document)
        parsed["dives"][0]["duration"] = self.MAX_DURATION + 1
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (0, 0, 0, 1)

    @pytest.mark.asyncio
    async def test_a_schedule_at_the_count_ceiling_still_computes_its_next_due(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """`recalculate_service_schedule` writes `dive_count_at_start + interval_dives` into
        a column no wider than either of them."""
        _, document = seeded
        parsed = json.loads(document)
        schedule = parsed["gear_service_schedules"][0]
        schedule["dive_count_at_start"] = self.MAX_COUNT
        schedule["interval_dives"] = self.MAX_COUNT
        schedule.pop("interval_months", None)
        # The service record's own snapshot would otherwise become the baseline instead.
        parsed["gear_service_records"] = []
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["gear_service_schedules"] == (1, 0, 0, 0)
        stored = (
            (await async_db.execute(select(GearServiceSchedule).where(GearServiceSchedule.user_id == destination.id)))
            .scalars()
            .one()
        )
        assert stored.next_due_at_dive_count == 2 * self.MAX_COUNT


class TestTheIntegerColumnCensus:
    """The second guard, and the one a `CheckConstraint` sweep cannot be:  an `Integer`
    column's real bound is its *width*, which no rule is written on.

    So every `Integer` column an import writes is accounted for here by name - guarded in
    the planner, or excluded with the reason it needs no guard. A new one lands in neither
    bucket and this fails, which is the only thing that would notice.
    """

    # (table, column) -> why it cannot carry an unstorable number.
    ACCOUNTED: dict[tuple[str, str], str] = {
        ("dive", "dive_number"): "bounded in `_DIVE_BOUNDS`",
        ("dive", "duration"): "bounded in `_DIVE_BOUNDS`",
        ("dive", "visibility"): "bounded in `_DIVE_BOUNDS`",
        ("dive", "altitude"): "bounded in `_DIVE_BOUNDS`, to the model's own -450..6500",
        ("dive", "id"): "the sequence's, never the document's",
        ("dive", "user_id"): "the caller's",
        ("dive", "trip_id"): "resolved from a row this import wrote",
        ("dive", "course_id"): "resolved from a row this import wrote",
        ("dive", "utc_offset_minutes"): "derived from a parsed UTC offset, which Python bounds at a day",
        ("dive_mixture", "id"): "the sequence's",
        ("dive_mixture", "dive_id"): "resolved from a row this import wrote",
        ("dive_mixture", "gas_number"): "bounded in `_MIXTURE_BOUNDS`",
        ("dive_profile", "id"): "the sequence's",
        ("dive_profile", "recording_id"): "resolved from a row this import wrote",
        ("dive_profile", "dive_id"): "resolved from a row this import wrote",
        ("dive_profile", "extractor_version"): "this build's own constant",
        ("dive_profile", "duration"): "bounded in `_plan_profile`, against the samples and the declared span",
        ("dive_profile", "depth_sample_count"): "a length, capped by `MAX_POINTS_PER_CHANNEL`",
        ("dive_profile", "event_count"): "a count, capped by `MAX_EVENTS`",
        ("dive_profile", "max_depth_cm"): "an extreme of a channel `_series` bounds",
        ("dive_profile", "max_ceiling_cm"): "an extreme of a channel `_series` bounds",
        ("dive_profile", "min_temperature_c10"): "an extreme of a channel `_series` bounds",
        ("dive_profile", "max_temperature_c10"): "an extreme of a channel `_series` bounds",
        ("dive_profile", "min_pressure_bar10"): "an extreme of a channel `_series` bounds",
        ("dive_profile", "max_pressure_bar10"): "an extreme of a channel `_series` bounds",
        ("gear_service_schedule", "id"): "the sequence's",
        ("gear_service_schedule", "user_id"): "the caller's",
        ("gear_service_schedule", "gear_item_id"): "resolved from a row this import wrote",
        ("gear_service_schedule", "interval_months"): "bounded in `_plan_schedule`",
        ("gear_service_schedule", "interval_dives"): "bounded in `_plan_schedule`",
        ("gear_service_schedule", "dive_count_at_start"): "bounded by `_Planner._count`",
        ("gear_service_schedule", "next_due_at_dive_count"): "derived by `recalculate_service_schedule`",
        (
            "gear_service_schedule",
            "notified_for_due_at_dive_count",
        ): "digest-job notify state, never imported and cleared by `recalculate_service_schedule`",
        ("gear_service_record", "id"): "the sequence's",
        ("gear_service_record", "user_id"): "the caller's",
        ("gear_service_record", "gear_item_id"): "resolved from a row this import wrote",
        ("gear_service_record", "gear_service_schedule_id"): "resolved from a row this import wrote",
        ("gear_service_record", "dive_count_at_service"): "bounded by `_Planner._count`",
        ("gear_item", "id"): "the sequence's",
        ("gear_item", "user_id"): "the caller's",
        ("gear_item", "dive_count"): "written as 0 and recomputed by `recalculate_gear_dive_counts`",
        ("trip", "id"): "the sequence's",
        ("trip", "user_id"): "the caller's",
        ("course", "id"): "the sequence's",
        ("course", "user_id"): "the caller's",
        ("dive_site", "id"): "the sequence's",
        ("dive_site", "user_id"): "the caller's",
        ("gear_set", "id"): "the sequence's",
        ("gear_set", "user_id"): "the caller's",
        ("certification", "id"): "the sequence's",
        ("certification", "user_id"): "the caller's",
        ("certification", "course_id"): "resolved from a row this import wrote",
        ("user_dive_stats", "id"): "the sequence's",
        ("user_dive_stats", "user_id"): "the caller's",
        ("user_dive_stats", "total_dives"): "a count of rows, derived by `recalculate_dive_stats`",
        ("user_dive_stats", "species_seen"): "a count of distinct rows, derived by `recalculate_dive_stats`",
        ("trip_location", "id"): "the sequence's",
        ("trip_location", "trip_id"): "resolved from a row this import wrote",
        ("trip_location", "position"): "the list index, not the document's",
        ("dive_file", "id"): "the sequence's",
        ("dive_file", "user_id"): "the caller's",
        ("dive_file", "recording_id"): "resolved from a row this import wrote",
        ("dive_file", "dive_id"): "resolved from a row this import wrote",
        ("dive_recording", "id"): "the sequence's",
        ("dive_recording", "user_id"): "the caller's",
        ("dive_recording", "dive_id"): "resolved from a row this import wrote",
        ("dive_recording", "ordinal"): "the list index, not the document's",
        ("dive_recording", "device_dive_number"): "bounded in `_plan_recordings`",
        ("dive_recording", "utc_offset_minutes"): "derived from a parsed UTC offset, which Python bounds at a day",
        ("dive_recording", "duration"): "the samples' own span, capped by `_plan_profile`",
        ("dive_file", "byte_size"): "the restored bytes' own length, capped by `MAX_DIVE_FILE_SIZE`",
        ("certification_file", "id"): "the sequence's",
        ("certification_file", "certification_id"): "resolved from a row this import wrote",
        ("certification_file", "byte_size"): "the restored bytes' own length, capped by `MAX_CARD_FILE_SIZE`",
    }

    def test_every_integer_column_an_import_writes_is_accounted_for(self) -> None:
        """The `written` tuple is every table `writer.py` issues a statement against.

        Derive it from that module rather than from memory: `_Writer.write()` names the
        collections it walks, and `_recalculate` names the three maintainers it runs, one of
        which writes `user_dive_stats`. An omission here is silent - the census passes over
        a table it never looks at.
        """
        from sqlalchemy import BigInteger, Integer

        from src.app.models.certification_file import CertificationFile
        from src.app.models.trip_location import TripLocation
        from src.app.models.user_dive_stats import UserDiveStats

        written: tuple[Any, ...] = (
            Dive,
            DiveMixture,
            DiveRecording,
            DiveProfile,
            Trip,
            TripLocation,
            Course,
            DiveSite,
            GearItem,
            GearSet,
            GearServiceSchedule,
            GearServiceRecord,
            Certification,
            DiveFile,
            CertificationFile,
            UserDiveStats,
        )
        found = {
            (model.__table__.name, column.name)
            for model in written
            for column in model.__table__.columns
            # `BigInteger` is excluded rather than overlooked: `user_dive_stats.total_time`
            # widened precisely because it sums a bounded column, and a 64-bit column is not
            # reachable by any number a document can carry.
            if isinstance(column.type, Integer) and not isinstance(column.type, BigInteger)
        }

        assert found == set(self.ACCOUNTED), (
            "an `Integer` column an import writes is unaccounted for, so a conforming document carrying a number "
            f"wider than 32 bits would abort the whole import: {sorted(found ^ set(self.ACCOUNTED))}"
        )


class TestTheAgencyVocabulary:
    def test_the_app_enum_is_the_formats_own(self) -> None:
        """A REQUIRED member of a vocabulary the format freezes at 1.0 (spec §§6.16, 7), so
        laundering five real agencies through `other` would have made a round trip lossy on
        the one member the format guarantees cannot grow."""
        schema = divejson.load_schema()
        published = schema["$defs"]["certification"]["properties"]["agency"]["enum"]

        assert [member.value for member in CertificationAgency] == published


class TestTheFormatLabelTable:
    """`_FORMAT_LABELS` names every id `divejson.read_formats()` returns.

    The one guard in this repository that can see a **new reader** arrive. Everything else
    on both sides of the seam is written to tolerate an unknown format - the accepted set is
    computed per call and never listed, `formats_this_build_reads` falls back to the raw id,
    and the picker's extension list is the web app's - so a version bump that adds a reader
    changes what the API accepts with nothing anywhere reporting it. That is not
    hypothetical: `suunto_xml` shipped in `divejson` 0.4.0, and the pin crossed it into a
    build whose "formats this build reads" sentence rendered the bare string `suunto_xml`
    while the web app's picker refused the extension. Nobody saw it for ten review rounds.
    """

    def test_every_read_format_has_a_label(self) -> None:
        unlabelled = [fmt for fmt in divejson.read_formats() if fmt not in import_reader._FORMAT_LABELS]

        assert not unlabelled, (
            "`divejson` reads a format this build has no name for, so the API accepts it while every message "
            f"about it renders the raw id: {unlabelled}. Add it to `_FORMAT_LABELS`, and to the prose in "
            "`README.md`, `api/v1/logbook_import.py` and `schemas/logbook_import.py` that lists the set."
        )

    def test_no_label_outlives_its_format(self) -> None:
        """The mirror, and it is not symmetry for its own sake: a label for a format the
        library has dropped is a format this build advertises and refuses."""
        stale = [fmt for fmt in import_reader._FORMAT_LABELS if fmt not in divejson.read_formats()]

        assert not stale, f"`_FORMAT_LABELS` names a format `divejson` no longer reads: {stale}"


class TestTheImportGates:
    """A dive whose uuid is new is still matched against the logbook, recording by recording.

    Uuid matching has nothing to work with here and that is the point: another instance's
    export carries identifiers that mean nothing on this one, so the device and the clock are
    all there is. Without these gates a diver who imports their second computer's file after
    logging the dive from their first gets a second dive.
    """

    START = datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC)

    @staticmethod
    def _document(recording: dict[str, Any], **dive: Any) -> bytes:
        body = {
            "format": "divejson",
            "version": "1.0",
            "exported_at": "2026-09-09T10:00:00+00:00",
            "dives": [
                {
                    "uuid": str(uuid7()),
                    "dive_number": 1,
                    "started_at": "2026-09-08T15:17:38+03:00",
                    "duration": 3051,
                    "max_depth": 19.04,
                    "recordings": [recording],
                    "created_at": "2026-09-09T10:00:00+00:00",
                    **dive,
                }
            ],
        }
        return json.dumps(body).encode()

    def _seed(self, db: Session, **device: Any) -> tuple[Any, Dive]:
        """A dive with one recording carrying a device, a start and the two gate figures -
        which is what an attach through the create form leaves behind."""
        user = create_user(db)
        dive = create_dive(db, user)
        dive.start_time = self.START
        dive.utc_offset_minutes = 180
        dive.duration = 3051
        dive.max_depth = 19.04
        recording = create_dive_recording(db, user, dive)
        recording.start_time = self.START
        recording.utc_offset_minutes = 180
        recording.duration = 3051
        recording.max_depth = 19.04
        for column, value in device.items():
            setattr(recording, column, value)
        db.commit()
        return user, dive

    @pytest.mark.asyncio
    async def test_the_same_computers_second_export_fills_and_creates_no_dive(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The FIT arriving after the JSON was logged through the form. Nothing is created:
        every recording of the incoming dive is already in the logbook."""
        user, dive = self._seed(db, device_brand="Suunto", device_serial="253810000400")
        document = self._document(
            {
                "device": {"brand": "suunto", "model": "Suunto Ocean"},
                "started_at": "2026-09-08T15:17:38+03:00",
                "profile": {"duration": 3473, "depth": {"times": [0, 3473], "values": [0, 1904]}},
            },
            cns_end=9.0,
        )

        plan = await _apply(async_db, user.id, document)

        assert ImportNoteCode.RECORDING_FILLED in _codes(plan)
        assert _counts(plan)["dives"] == (0, 0, 0, 1)
        assert len((await async_db.execute(select(Dive.id).where(Dive.user_id == user.id))).scalars().all()) == 1
        # The stored recording keeps its serial and gains the model the incoming one had.
        recording = (await async_db.execute(select(DiveRecording).where(DiveRecording.dive_id == dive.id))).scalar_one()
        assert (recording.device_serial, recording.device_model) == ("253810000400", "Suunto Ocean")
        # And the dive's blank exposure reading fills from the document.
        assert (await async_db.execute(select(Dive.cns_end).where(Dive.id == dive.id))).scalar_one() == 9.0

    @pytest.mark.asyncio
    async def test_a_second_computer_is_attached_rather_than_logged_again(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The strict gate: a *different* device, well inside the window, agreeing on depth
        and duration. It joins the dive the caller already has."""
        user, dive = self._seed(db, device_brand="Suunto", device_serial="253810000400")
        document = self._document(
            {
                "device": {"brand": "Shearwater Research, Inc", "model": "Perdix 3", "serial": "D9772626"},
                "started_at": "2026-09-08T15:19:38+03:00",
                # The samples span 2940 s, which is what the gate compares. **Not the
                # declared `duration`**: a document may legitimately declare a span longer
                # than its own samples (a computer that stops sampling at the surface), and
                # what an imported recording's figures mean is "the samples' own".
                "profile": {"duration": 2940, "depth": {"times": [0, 2940], "values": [0, 1900]}},
            }
        )

        plan = await _apply(async_db, user.id, document)

        assert ImportNoteCode.RECORDING_ATTACHED in _codes(plan)
        assert len((await async_db.execute(select(Dive.id).where(Dive.user_id == user.id))).scalars().all()) == 1
        recordings = (
            (
                await async_db.execute(
                    select(DiveRecording).where(DiveRecording.dive_id == dive.id).order_by(DiveRecording.ordinal)
                )
            )
            .scalars()
            .all()
        )
        assert [row.ordinal for row in recordings] == [0, 1]
        assert recordings[1].device_serial == "D9772626"
        # **The samples' own span and deepest reading**, which is what an imported recording
        # has: a Recording carries no scalars of its own, so the document offers nowhere else
        # to read the two figures the strict gate compares from.
        assert (recordings[1].duration, recordings[1].max_depth) == (2940, 19.0)

    @pytest.mark.asyncio
    async def test_an_attached_recordings_cylinder_labels_are_mapped_onto_the_dives(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """`gas_number` is dive-scoped, so a second computer's own numbering has to move.

        This dive has EAN32 on cylinder 1 and a 50 % deco bottle on 2; the incoming computer
        numbers them the other way round. Without the mapping its pressure channel lands on
        the dive's back gas and the whole multi-tank figure is computed off the wrong tank -
        which nothing downstream can detect, because a `gas_number` that names *a* cylinder
        is indistinguishable from one that names the right one.
        """
        user, dive = self._seed(db, device_brand="Suunto", device_serial="253810000400")
        db.add_all(
            [
                DiveMixture(dive_id=dive.id, gas_number=1, oxygen=32.0, helium=0.0),
                DiveMixture(dive_id=dive.id, gas_number=2, oxygen=50.0, helium=0.0),
            ]
        )
        db.commit()
        document = self._document(
            {
                "device": {"brand": "Shearwater Research, Inc", "model": "Perdix 3", "serial": "D9772626"},
                "started_at": "2026-09-08T15:19:38+03:00",
                "profile": {
                    "duration": 2940,
                    "depth": {"times": [0, 2940], "values": [0, 1900]},
                    "pressures": [{"gas_number": 1, "times": [0], "values": [2000]}],
                    "events": [{"time": 0, "type": "gas_switch", "gas_number": 1}],
                },
            },
            # The incoming document's own cylinders, in its own labelling: its 1 is the deco
            # bottle the dive calls 2.
            cylinders=[
                {"gas_number": 1, "oxygen": 50.0, "helium": 0.0},
                {"gas_number": 2, "oxygen": 32.0, "helium": 0.0},
            ],
        )

        plan = await _apply(async_db, user.id, document)

        assert ImportNoteCode.RECORDING_ATTACHED in _codes(plan)
        attached = (
            (
                await async_db.execute(
                    select(DiveRecording.id).where(DiveRecording.dive_id == dive.id, DiveRecording.ordinal == 1)
                )
            )
            .scalars()
            .one()
        )
        # `DiveProfile.data` is `deferred`, so it has to be named in the select rather than
        # touched off an instance - a lazy load here is IO outside the greenlet.
        data = (
            await async_db.execute(select(DiveProfile.data).where(DiveProfile.recording_id == attached))
        ).scalar_one()
        assert [series["gas_number"] for series in data["pressure"]] == [2]
        assert [event["gas_number"] for event in data["events"]] == [2]

    @pytest.mark.asyncio
    async def test_an_unrelated_dive_is_still_a_dive(self, db: Session, async_db: AsyncSession) -> None:
        """The gate has to refuse as well as fire. A dive the next morning is nobody's second
        computer, and importing it must create a dive rather than fold it into yesterday's."""
        user, _ = self._seed(db, device_brand="Suunto", device_serial="253810000400")
        document = self._document(
            {
                # Everything but the clock agrees with the seeded dive, so the start window
                # is the clause doing the refusing here rather than a depth or a duration.
                "device": {"brand": "Garmin", "serial": "3542000001"},
                "started_at": "2026-09-09T09:00:00+03:00",
                "profile": {"duration": 2940, "depth": {"times": [0, 2940], "values": [0, 1904]}},
            },
            started_at="2026-09-09T09:00:00+03:00",
        )

        plan = await _apply(async_db, user.id, document)

        assert ImportNoteCode.RECORDING_ATTACHED not in _codes(plan)
        assert ImportNoteCode.RECORDING_FILLED not in _codes(plan)
        assert _counts(plan)["dives"] == (1, 0, 0, 0)

    @pytest.mark.asyncio
    async def test_an_empty_account_asks_no_gate_anything(self, db: Session, async_db: AsyncSession) -> None:
        """The guard that keeps the gates off the hot path: an account with no recordings has
        nothing to match against, and a whole-archive restore is exactly that. The dive is
        created and no note is raised."""
        user = create_user(db)
        document = self._document(
            {"device": {"brand": "Suunto"}, "profile": {"duration": 60, "depth": {"times": [0], "values": [0]}}}
        )

        plan = await _apply(async_db, user.id, document)

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        assert not {ImportNoteCode.RECORDING_ATTACHED, ImportNoteCode.RECORDING_FILLED} & _codes(plan)
