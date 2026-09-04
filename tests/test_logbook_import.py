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
from typing import Any

import pytest
import pytest_asyncio
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.models.certification import Certification
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_file import DiveFile
from src.app.models.dive_gear_item import DiveGearItem
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_site import DiveSite
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.gear_set_item import GearSetItem
from src.app.schemas.certification import CertificationAgency
from src.app.schemas.logbook_import import ImportNoteCode
from src.app.services.export import load_export_bundle, write_divejson
from src.app.services.export.archive import DIVEJSON_NAME
from src.app.services.export.paths import plan_archive_paths
from src.app.services.logbook_import import (
    ImportTooLargeError,
    UnsupportedImportError,
    load_import,
    plan_import,
    write_import,
)
from src.app.services.logbook_import.planner import _DIVE_BOUNDS, _MIXTURE_BOUNDS
from src.app.services.logbook_import.reader import DuplicateMemberError, MalformedImportError
from tests.conftest import db_available
from tests.helpers.divejson import SCHEMA_PATH, assert_conforms, parse_document
from tests.helpers.generators import (
    create_certification,
    create_course,
    create_dive,
    create_dive_site,
    create_gear_item,
    create_gear_service_record,
    create_gear_service_schedule,
    create_gear_set,
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


async def _preview(db: AsyncSession, user_id: int, data: bytes, filename: str = "logbook.divejson") -> Any:
    with await load_import(_upload(data, filename)) as loaded:
        return await plan_import(db, user_id=user_id, loaded=loaded)


async def _apply(db: AsyncSession, user_id: int, data: bytes, filename: str = "logbook.divejson") -> Any:
    """Plan and write in one transaction, exactly as `POST /import/divejson` does.

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
    dive = create_dive(db, user, trip=trip, course=course)
    db.add(DiveMixture(dive_id=dive.id, volume=12.0, oxygen=32.0, helium=0.0, start_pressure=200.0, end_pressure=60.0))
    db.commit()

    db.add_all(
        [
            DiveDiveSite(dive_id=dive.id, dive_site_id=site_a.id, position=0),
            DiveDiveSite(dive_id=dive.id, dive_site_id=site_b.id, position=1),
            DiveGearItem(dive_id=dive.id, gear_item_id=item.id, position=0),
            GearSetItem(gear_set_id=gear_set.id, gear_item_id=item.id, position=0),
        ]
    )
    db.commit()
    return user


@pytest_asyncio.fixture
async def seeded(db: Session, async_db: AsyncSession) -> Any:
    user = _seed_logbook(db)
    return user, await _export(async_db, user.id)


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
        assert_conforms(re_exported)

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
        source_user, document = seeded
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, document)

        assert ImportNoteCode.RECORD_REMAPPED in _codes(plan)
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
        member = parse_document(_document_of(archive))["dives"][0]["source_file"]["archive_path"]
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
        member = parse_document(document)["dives"][0]["source_file"]["archive_path"]
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
        parsed["dives"][0]["profile"] = {
            "duration": 30,
            "depth": {"times": [0, 20, 10], "values": [1, 2, 3]},
        }
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


class TestTheOffsetUnknownState:
    """The one state only this endpoint can create, and the one no other suite covers.

    A dive whose source recorded no offset stores its wall clock with a NULL
    `utc_offset_minutes` and reads back offset-less - not converted, not stamped with
    anything. Round-tripped through the real writer, because the failure this pins is
    silent: a fabricated offset looks exactly like a real one.
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
        assert_conforms(re_exported)

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
        life_list = await species_life_list(async_db, user_id=destination.id, offset=0, limit=10)
        assert life_list["total_count"] == 0

    @pytest.mark.asyncio
    async def test_the_write_api_still_demands_an_offset(self) -> None:
        """The requirement became a write-side rule, not a deleted one."""
        from pydantic import ValidationError

        from src.app.schemas.dive import DiveCreate, DiveRead

        with pytest.raises(ValidationError):
            DiveCreate(dive_number=1, start_time=datetime(2026, 4, 17, 11, 49), duration=60)
        # And the read shape serves what is stored.
        assert DiveRead.model_fields["start_time"].annotation is datetime


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
    async def test_a_cylinder_with_no_mix_is_skipped_and_the_dive_is_not(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        """§6.3: absent `oxygen` means not recorded, not 21 - and divers plan gas off these
        numbers, so the supply goes rather than the assumption being made."""
        _, document = seeded
        parsed = json.loads(document)
        del parsed["dives"][0]["cylinders"][0]["oxygen"]
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(parsed).encode())

        assert _counts(plan)["dives"] == (1, 0, 0, 0)
        dive = (await async_db.execute(select(Dive).where(Dive.user_id == destination.id))).scalars().one()
        assert (
            not (await async_db.execute(select(DiveMixture.id).where(DiveMixture.dive_id == dive.id))).scalars().all()
        )

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


class TestTheDiverIsNeverApplied:
    @pytest.mark.asyncio
    async def test_the_destination_keeps_its_own_identity_and_settings(
        self, seeded: Any, db: Session, async_db: AsyncSession
    ) -> None:
        from src.app.models.user import User

        _, document = seeded
        destination = create_user(db)
        before = (destination.name, destination.username, destination.email, destination.units)

        plan = await _apply(async_db, destination.id, document)

        assert ImportNoteCode.DIVER_NOT_APPLIED in _codes(plan)
        after = (await async_db.execute(select(User).where(User.id == destination.id))).scalars().one()
        assert (after.name, after.username, after.email, after.units) == before


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
    # Columns the *record* cannot exist without, so the planner skips the record rather than
    # dropping a value. Each is checked explicitly by a test above.
    REQUIRED_COLUMNS = frozenset(
        {"ck_dive_mixture_volume_positive", "ck_dive_mixture_oxygen_range", "ck_dive_mixture_helium_range"}
    )
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
                excluded = self.PAIR_RULES | self.REQUIRED_COLUMNS | self.POSITION_RULES
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
        member = parse_document(document)["dives"][0]["source_file"]["archive_path"]
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
        parsed["dives"][0]["profile"] = {
            "duration": 30,
            "depth": {"times": [0, 10, 20], "values": [100, self.HUGE, 300]},
        }
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
        ("trip_location", "id"): "the sequence's",
        ("trip_location", "trip_id"): "resolved from a row this import wrote",
        ("trip_location", "position"): "the list index, not the document's",
        ("dive_file", "id"): "the sequence's",
        ("dive_file", "user_id"): "the caller's",
        ("dive_file", "dive_id"): "resolved from a row this import wrote",
        ("dive_file", "byte_size"): "the restored bytes' own length, capped by `MAX_DIVE_FILE_SIZE`",
        ("certification_file", "id"): "the sequence's",
        ("certification_file", "certification_id"): "resolved from a row this import wrote",
        ("certification_file", "byte_size"): "the restored bytes' own length, capped by `MAX_CARD_FILE_SIZE`",
    }

    def test_every_integer_column_an_import_writes_is_accounted_for(self) -> None:
        from sqlalchemy import Integer

        from src.app.models.certification_file import CertificationFile
        from src.app.models.trip_location import TripLocation

        written: tuple[Any, ...] = (
            Dive,
            DiveMixture,
            DiveProfile,
            GearServiceSchedule,
            GearServiceRecord,
            GearItem,
            TripLocation,
            DiveFile,
            CertificationFile,
        )
        found = {
            (model.__table__.name, column.name)
            for model in written
            for column in model.__table__.columns
            if isinstance(column.type, Integer)
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
        schema = json.loads((SCHEMA_PATH).read_text(encoding="utf-8"))
        published = schema["$defs"]["certification"]["properties"]["agency"]["enum"]

        assert [member.value for member in CertificationAgency] == published
