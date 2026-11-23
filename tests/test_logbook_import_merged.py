"""A file of a merged dive belongs to it, against real Postgres.

A merge is not a deletion: the dive it soft-deletes names the dive it joined, and to an import a
record of either half - by its uuid, by its recording or by its bytes - is the merged dive. A
merged dive deleted afterwards is a deleted dive like any other, and so is a duplicate the diver
deleted by hand.
"""

import json
import uuid as uuid_pkg
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.v1 import dives as dives_module
from src.app.api.v1.dives import _cached_read_dive
from src.app.models.dive import Dive
from src.app.models.dive_file import DiveFile
from src.app.models.dive_recording import DiveRecording
from src.app.models.user import User
from src.app.schemas.dive_computer import ListedDive
from src.app.schemas.logbook_import import ImportDiveOutcome, ImportNoteCode
from src.app.services import blob_store
from src.app.services.dive_computers import known_dives
from src.app.services.dive_recordings import DEVICE_COLUMNS
from tests.conftest import db_available
from tests.helpers.generators import create_user
from tests.test_dive_merge_rows import (
    FIRST_PART_SAMPLES,
    FIRST_START,
    SECOND_PART_SAMPLES,
    SECOND_START,
    _attach,
    _dive_at,
    _export,
    _merge,
    _recordings,
    _samples,
    _two_halves,
)
from tests.test_logbook_import_batch import _codes, _count, _delete, _dive_ids, _import

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytest.fixture
def volume(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def merging(monkeypatch: pytest.MonkeyPatch) -> None:
    """The merge route with Redis out of the way, as its own tests drive it."""
    for name in (
        "invalidate_dive_caches",
        "invalidate_gear_caches",
        "invalidate_trip_caches",
        "invalidate_dive_site_caches",
        "invalidate_contact_caches",
        "invalidate_course_caches",
    ):
        monkeypatch.setattr(dives_module, name, AsyncMock())
    monkeypatch.setattr(dives_module, "_cached_read_dive", _cached_read_dive.__wrapped__)  # type: ignore[attr-defined]


SERIAL = "253810000400"
PART_ONE = _export(start=FIRST_START, duration=180, max_depth="5.0", samples=_samples(FIRST_PART_SAMPLES, depth_cm=500))
PART_TWO = _export(
    start=SECOND_START, duration=2921, max_depth="19.04", samples=_samples(SECOND_PART_SAMPLES, depth_cm=1904)
)
A_MINUTE_IN = (datetime.fromisoformat(FIRST_START) + timedelta(seconds=60)).isoformat()


def _logbook(*dives: dict[str, Any]) -> bytes:
    """A DiveJSON document of `dives`, each with a fresh uuid unless it says otherwise."""
    records = [{"uuid": str(uuid_pkg.uuid4()), "submerged_time": 1800, **dive} for dive in dives]
    return json.dumps(
        {"format": "divejson", "version": "1.0", "exported_at": "2026-09-09T10:00:00+03:00", "dives": records}
    ).encode()


def _reading(start: str, device: dict[str, Any] | None = None) -> dict[str, Any]:
    """A logbook's dive of one recording starting at `start`, with ten minutes of samples."""
    recording: dict[str, Any] = {
        "started_at": start,
        "profile": {"depth": {"times": [0, 60_000, 600_000], "values": [0, 1500, 0]}},
    }
    if device is not None:
        recording["device"] = device
    return {"started_at": start, "recordings": [recording]}


async def _merged(async_db: AsyncSession, db: Session, diver: User) -> tuple[Dive, Dive]:
    first, second = await _two_halves(async_db, db, diver)
    result = await _merge(async_db, diver, first, second)
    assert result.folded is True
    return first, second


async def _files_of(db: AsyncSession, diver: User) -> int:
    return await _count(db, DiveFile, diver)


class TestAMergeIsRecorded:
    @pytest.mark.asyncio
    async def test_the_merged_away_dive_names_the_dive_it_joined_and_keeps_its_uuid(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        first, second = await _merged(async_db, db, diver)

        row = (
            await async_db.execute(
                select(Dive.uuid, Dive.is_deleted, Dive.merged_into_id)
                .where(Dive.id == second.id)
                .execution_options(populate_existing=True)
            )
        ).one()

        assert (row.uuid, row.is_deleted, row.merged_into_id) == (second.uuid, True, first.id)


class TestAFoldsRecords:
    """One computer's two records of one dive, folded into one recording."""

    @pytest.mark.parametrize("file", [PART_ONE, PART_TWO], ids=["the first record", "the second record"])
    @pytest.mark.asyncio
    async def test_each_record_s_own_file_imported_again_is_the_merged_dive(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, file: bytes
    ) -> None:
        diver = create_user(db)
        first, _ = await _merged(async_db, db, diver)
        files = await _files_of(async_db, diver)

        report = await _import(async_db, diver, [("dive.json", file)])

        assert [(row.outcome, row.uuid) for row in report.dives] == [(ImportDiveOutcome.UNCHANGED, first.uuid)]
        assert await _dive_ids(async_db, diver) == [first.id]
        assert await _files_of(async_db, diver) == files
        assert not {ImportNoteCode.RECORDING_FILLED, ImportNoteCode.RECORDING_ATTACHED} & _codes(report)

    @pytest.mark.asyncio
    async def test_a_record_under_the_merged_away_dive_s_uuid_is_the_merged_dive(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        first, second = await _merged(async_db, db, diver)

        report = await _import(
            async_db, diver, [("logbook.divejson", _logbook({"uuid": str(second.uuid), "started_at": SECOND_START}))]
        )

        [row] = report.dives
        assert (row.outcome, row.uuid) == (ImportDiveOutcome.UNCHANGED, first.uuid)
        assert [note.message for note in row.notes if note.code == ImportNoteCode.RECORD_LINKED.value] == [
            "This dive is one you merged into this one, so nothing was written for it."
        ]
        assert await _dive_ids(async_db, diver) == [first.id]

    @pytest.mark.parametrize("start", [FIRST_START, A_MINUTE_IN, SECOND_START])
    @pytest.mark.asyncio
    async def test_another_source_naming_the_serial_is_the_merged_dive(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, start: str
    ) -> None:
        diver = create_user(db)
        first, _ = await _merged(async_db, db, diver)
        [before] = await _recordings(async_db, first)
        stored = (before.duration, before.max_depth, before.device_model)

        report = await _import(async_db, diver, [("logbook.divejson", _logbook(_reading(start, {"serial": SERIAL})))])

        assert [(row.outcome, row.uuid) for row in report.dives] == [(ImportDiveOutcome.UNCHANGED, first.uuid)]
        assert await _dive_ids(async_db, diver) == [first.id]
        [after] = await _recordings(async_db, first)
        assert (after.duration, after.max_depth, after.device_model) == stored

    @pytest.mark.asyncio
    async def test_a_reading_of_one_record_the_logbook_lacks_the_file_of_adds_the_file_and_nothing_else(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        """Another export of the second record, trimmed differently: its file joins the merged
        recording, unread, and nothing of the recording is filled or replaced from it."""
        diver = create_user(db)
        first, _ = await _merged(async_db, db, diver)
        [before] = await _recordings(async_db, first)
        stored = (before.duration, before.max_depth, before.start_time)
        trimmed = _export(
            start=SECOND_START,
            duration=2900,
            max_depth="19.04",
            samples=_samples(SECOND_PART_SAMPLES - 5, depth_cm=1904),
        )

        [row] = (await _import(async_db, diver, [("trimmed.json", trimmed)])).dives

        assert (row.outcome, row.uuid) == (ImportDiveOutcome.MODIFIED, first.uuid)
        assert [change["member"] for change in row.changes] == ["recordings/0/files/2"]
        [after] = await _recordings(async_db, first)
        assert (after.duration, after.max_depth, after.start_time) == stored
        joined = (
            await async_db.execute(
                select(DiveFile.fixes_read).where(
                    DiveFile.user_id == diver.id, DiveFile.original_filename == "trimmed.json"
                )
            )
        ).scalar_one()
        assert joined is False, "stored unread, for the worker"

    @pytest.mark.asyncio
    async def test_a_header_only_reading_of_the_first_record_fills_nothing_and_the_form_still_fills(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        first, _ = await _merged(async_db, db, diver)
        header = _export(start=FIRST_START, duration=180, max_depth="5.0", cns_end=7.0)

        [row] = (await _import(async_db, diver, [("header.json", header)])).dives

        assert row.uuid == first.uuid
        [recording] = await _recordings(async_db, first)
        assert recording.cns_end is None

        await _attach(
            async_db,
            diver,
            first,
            _export(start=FIRST_START, duration=181, max_depth="5.0", cns_end=8.0),
            filename="form.json",
        )

        [recording] = await _recordings(async_db, first)
        assert recording.cns_end is not None, "the form's attach derives the recording from its files as before"

    @pytest.mark.asyncio
    async def test_a_recording_that_names_no_device_inside_the_span_is_placed_by_the_gates_as_before(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        first, _ = await _merged(async_db, db, diver)

        [row] = (await _import(async_db, diver, [("logbook.divejson", _logbook(_reading(A_MINUTE_IN)))])).dives

        assert row.outcome is ImportDiveOutcome.NEW
        assert len(await _dive_ids(async_db, diver)) == 2

    @pytest.mark.asyncio
    async def test_a_fold_made_before_merges_were_recorded_is_found_by_its_files(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        """The merged-away row unmarked, as one merged before the revision is, and the merged
        recording naming no device: neither the uuid nor the recording finds the merge, and the
        bytes of each record's own file do."""
        diver = create_user(db)
        first, second = await _merged(async_db, db, diver)
        await async_db.execute(update(Dive).where(Dive.id == second.id).values(merged_into_id=None))
        await async_db.execute(
            update(DiveRecording)
            .where(DiveRecording.dive_id == first.id)
            .values(dict.fromkeys(DEVICE_COLUMNS.values()))
        )
        await async_db.commit()

        for file in (PART_ONE, PART_TWO):
            [row] = (await _import(async_db, diver, [("dive.json", file)])).dives
            assert (row.outcome, row.uuid) == (ImportDiveOutcome.UNCHANGED, first.uuid)
        assert await _dive_ids(async_db, diver) == [first.id]


class TestTwoComputersMerged:
    @pytest.mark.asyncio
    async def test_the_second_computer_s_file_and_its_dive_s_uuid_are_the_merged_dive(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        first = _dive_at(db, diver, datetime.fromisoformat(FIRST_START), number=1)
        second = _dive_at(db, diver, datetime.fromisoformat(FIRST_START) + timedelta(seconds=20), number=2)
        mine = _export(start=FIRST_START, serial="111", samples=_samples(30, depth_cm=1500))
        theirs_start = (datetime.fromisoformat(FIRST_START) + timedelta(seconds=20)).isoformat()
        theirs = _export(start=theirs_start, serial="222", samples=_samples(30, depth_cm=1500))
        await _attach(async_db, diver, first, mine, filename="mine.json")
        await _attach(async_db, diver, second, theirs, filename="theirs.json")
        result = await _merge(async_db, diver, first, second)
        assert result.folded is False

        by_file = await _import(async_db, diver, [("theirs.json", theirs)])
        by_uuid = await _import(
            async_db, diver, [("logbook.divejson", _logbook({"uuid": str(second.uuid), "started_at": theirs_start}))]
        )

        for report in (by_file, by_uuid):
            assert [(row.outcome, row.uuid) for row in report.dives] == [(ImportDiveOutcome.UNCHANGED, first.uuid)]
        assert await _dive_ids(async_db, diver) == [first.id]


class TestWhatIsADeletion:
    @pytest.mark.asyncio
    async def test_a_dive_merged_twice_over_is_the_dive_it_ended_in(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        start = datetime.fromisoformat(FIRST_START)
        dives = [_dive_at(db, diver, start + timedelta(seconds=20 * index), number=index) for index in range(3)]
        for index, dive in enumerate(dives):
            moment = (start + timedelta(seconds=20 * index)).isoformat()
            await _attach(
                async_db, diver, dive, _export(start=moment, serial=f"{index}" * 3, samples=_samples(30)), filename="f"
            )
        earliest, middle, latest = dives
        await _merge(async_db, diver, middle, latest)
        await _merge(async_db, diver, earliest, middle)

        [row] = (
            await _import(
                async_db,
                diver,
                [("logbook.divejson", _logbook({"uuid": str(latest.uuid), "started_at": FIRST_START}))],
            )
        ).dives

        assert (row.outcome, row.uuid) == (ImportDiveOutcome.UNCHANGED, earliest.uuid)
        assert await _dive_ids(async_db, diver) == [earliest.id]

    @pytest.mark.asyncio
    async def test_a_merged_dive_deleted_afterwards_is_deleted_and_its_records_are_new_dives(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        first, second = await _merged(async_db, db, diver)
        await _delete(async_db, first.uuid)

        [by_uuid] = (
            await _import(
                async_db,
                diver,
                [("logbook.divejson", _logbook({"uuid": str(second.uuid), "started_at": SECOND_START}))],
            )
        ).dives
        by_files = (await _import(async_db, diver, [("one.json", PART_ONE), ("two.json", PART_TWO)])).dives

        assert (by_uuid.outcome, by_uuid.uuid) == (ImportDiveOutcome.NEW, second.uuid)
        assert [row.outcome for row in by_files] == [ImportDiveOutcome.NEW] * 2
        assert len(await _dive_ids(async_db, diver)) == 3

    @pytest.mark.asyncio
    async def test_a_duplicate_the_diver_deleted_beside_a_live_dive_is_a_deletion(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        kept = _dive_at(db, diver, datetime.fromisoformat(FIRST_START), number=1)
        duplicate = _dive_at(db, diver, datetime.fromisoformat(FIRST_START), number=2)
        await _attach(async_db, diver, kept, PART_TWO, filename="kept.json")
        await _delete(async_db, duplicate.uuid)

        [row] = (
            await _import(
                async_db,
                diver,
                [("logbook.divejson", _logbook({"uuid": str(duplicate.uuid), "started_at": FIRST_START}))],
            )
        ).dives

        assert (row.outcome, row.uuid) == (ImportDiveOutcome.NEW, duplicate.uuid)
        assert len(await _dive_ids(async_db, diver)) == 2


class TestTheListAndTheImportAgree:
    @pytest.mark.asyncio
    async def test_every_dive_the_list_holds_of_a_fold_is_one_whose_import_adds_none(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        first, second = await _merged(async_db, db, diver)
        listed = [
            (uuid_pkg.uuid4(), FIRST_START),
            (uuid_pkg.uuid4(), A_MINUTE_IN),
            (uuid_pkg.uuid4(), SECOND_START),
            (second.uuid, "2001-01-01T00:00:00"),
        ]

        known = await known_dives(
            async_db,
            user_id=diver.id,
            serial=SERIAL,
            dives=[ListedDive(uuid=uuid, started_at=datetime.fromisoformat(start)) for uuid, start in listed],
        )

        assert [(row.uuid, row.dive_uuid) for row in known] == [(uuid, first.uuid) for uuid, _ in listed]
        for uuid, start in listed:
            dive = {**_reading(start, {"serial": SERIAL}), "uuid": str(uuid)}
            [row] = (await _import(async_db, diver, [("listed.divejson", _logbook(dive))])).dives
            assert (row.outcome, row.uuid) == (ImportDiveOutcome.UNCHANGED, first.uuid)
        assert await _dive_ids(async_db, diver) == [first.id]
