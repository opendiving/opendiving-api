"""Logbook import across the format change: the millisecond axis, the readouts and salinity
on the recording, a date-only dive, the notes cap - and a document a known writer produced
before any of it, read the way that writer meant it (`reader.read_as_written`).
"""

import copy
import io
import json
import uuid as uuid_pkg
from typing import Any

import pytest
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.core.schemas import NOTES_MAX_LENGTH
from src.app.models.dive import Dive
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_recording import DiveRecording
from src.app.schemas.logbook_import import ImportNoteCode
from src.app.services.logbook_import import load_import, plan_import, write_import
from src.app.services.logbook_import.reader import read_as_written
from tests.conftest import db_available
from tests.helpers.generators import create_user

_DIVE_UUID = "019f0000-0000-7000-8000-00000000d1e0"


def _pre_change_export(**dive: Any) -> dict[str, Any]:
    """What this app exported before the change: its producer key on the diver, no axis
    marker at the root, the axis in seconds and the readouts on the dive."""
    return {
        "format": "divejson",
        "version": "1.0",
        "exported_at": "2026-09-01T10:00:00+00:00",
        "generator": {"name": "Open Diving", "version": "0.1.0"},
        "diver": {"name": "Diver", "extensions": {"opendiving": {"units": "metric"}}},
        "dives": [
            {
                "uuid": _DIVE_UUID,
                "number": 7,
                "started_at": "2026-08-01T10:00:00+02:00",
                "duration": 3000,
                "water_type": "en13319",
                "cns_start": 3.0,
                "cns_end": 9.0,
                "surface_pressure": 1.012,
                "cylinders": [{"volume": 12.0, "oxygen": 32.0, "start_pressure": 200.0, "po2_limit": 1.4}],
                "recordings": [
                    {
                        "device": {"brand": "Suunto"},
                        "profile": {
                            "duration": 3000,
                            "depth": {"times": [0, 60, 2940], "values": [0, 1800, 0]},
                            "pressures": [{"gas_number": 1, "times": [0, 2940], "values": [2000, 900]}],
                            "events": [{"time": 60, "type": "safety_stop"}],
                        },
                    }
                ],
                **dive,
            }
        ],
    }


class TestReadAsWritten:
    def test_this_apps_pre_change_export_is_read_as_it_was_written(self) -> None:
        document = _pre_change_export()

        notes = read_as_written(document)

        dive = document["dives"][0]
        recording = dive["recordings"][0]
        assert recording["profile"]["duration"] == 3_000_000
        assert recording["profile"]["depth"]["times"] == [0, 60_000, 2_940_000]
        assert recording["profile"]["pressures"][0]["times"] == [0, 2_940_000]
        assert recording["profile"]["events"] == [{"time": 60_000, "type": "safety_stop"}]
        assert (recording["cns_start"], recording["cns_end"], recording["surface_pressure"]) == (3.0, 9.0, 1.012)
        assert recording["salinity"] == "en13319"
        assert not {"cns_start", "cns_end", "surface_pressure", "water_type"} & set(dive)
        assert dive["cylinders"][0] == {"volume": 12.0, "oxygen": 32.0, "start_pressure": 200.0, "ppo2_limit": 1.4}
        # One line per kind of reading, and all four kinds were read.
        assert [note.code for note in notes] == [ImportNoteCode.READ_AS_WRITTEN] * 4

    def test_a_dive_with_readouts_and_no_recording_gets_one_of_readouts_alone(self) -> None:
        document = _pre_change_export(recordings=[])

        read_as_written(document)

        assert document["dives"][0]["recordings"] == [
            {"cns_start": 3.0, "cns_end": 9.0, "surface_pressure": 1.012, "salinity": "en13319"}
        ]

    def test_en13319_with_nothing_to_carry_it_is_dropped_and_said_so(self) -> None:
        document = _pre_change_export(recordings=[])
        for member in ("cns_start", "cns_end", "surface_pressure"):
            del document["dives"][0][member]

        notes = read_as_written(document)

        assert "water_type" not in document["dives"][0]
        assert document["dives"][0]["recordings"] == []
        dropped = [note for note in notes if note.code is ImportNoteCode.VALUE_DROPPED]
        assert [(note.collection, note.uuid) for note in dropped] == [("dives", _DIVE_UUID)]

    def test_this_apps_export_since_the_change_is_left_alone(self) -> None:
        document = _pre_change_export()
        document["extensions"] = {"opendiving": {"profile_axis": "milliseconds"}}
        before = copy.deepcopy(document)

        assert read_as_written(document) == []
        assert document == before

    def test_a_document_no_known_writer_produced_is_left_alone(self) -> None:
        """A readout on the dive is then an undefined member the reader ignores (§5.6)."""
        document = _pre_change_export()
        del document["diver"]
        before = copy.deepcopy(document)

        assert read_as_written(document) == []
        assert document == before

    @pytest.mark.parametrize(
        ("version", "read_as_seconds"),
        [("0.2.0", True), ("0.9.0", True), ("0.12.0", True), ("0.12", True), ("0.13.0", False), ("1.0.0", False)],
    )
    def test_the_converters_documents_are_keyed_on_its_version_as_a_tuple(
        self, version: str, read_as_seconds: bool
    ) -> None:
        """`"0.9.0" < "0.13.0"` is false as strings, which is why the version is parsed."""
        document = _pre_change_export()
        del document["diver"]
        document["generator"] = {"name": "divejson convert", "version": version}

        read_as_written(document)

        times = document["dives"][0]["recordings"][0]["profile"]["depth"]["times"]
        assert times == ([0, 60_000, 2_940_000] if read_as_seconds else [0, 60, 2940])

    def test_a_generator_name_that_is_not_the_converters_is_not_keyed_on(self) -> None:
        """The application's own `generator.name` is an operator setting, so it keys nothing."""
        document = _pre_change_export()
        del document["diver"]
        document["generator"] = {"name": "divejson", "version": "0.2.0"}

        assert read_as_written(document) == []


pytestmark_db = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _upload(data: bytes, filename: str = "logbook.divejson") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename, size=len(data))


async def _apply(db: AsyncSession, user_id: int, data: bytes, filename: str = "logbook.divejson") -> Any:
    with await load_import(_upload(data, filename)) as loaded:
        plan = await plan_import(db, user_id=user_id, loaded=loaded, resolution_ran=True)
        await write_import(db, user_id=user_id, loaded=loaded, plan=plan)
        await db.commit()
        return plan


def _document(dive: dict[str, Any]) -> bytes:
    body = {
        "format": "divejson",
        "version": "1.0",
        "exported_at": "2026-09-09T10:00:00+00:00",
        "dives": [{"uuid": str(uuid_pkg.uuid4()), "number": 1, "duration": 1800, **dive}],
    }
    return json.dumps(body).encode()


async def _recordings(db: AsyncSession, user_id: int) -> list[DiveRecording]:
    rows = await db.execute(
        select(DiveRecording).where(DiveRecording.user_id == user_id).order_by(DiveRecording.ordinal)
    )
    return list(rows.scalars().all())


@pytestmark_db
class TestImportingAcrossTheChange:
    @pytest.mark.asyncio
    async def test_a_pre_change_export_lands_as_its_writer_meant_it(self, db: Session, async_db: AsyncSession) -> None:
        user = create_user(db)

        plan = await _apply(async_db, user.id, json.dumps(_pre_change_export()).encode())

        assert ImportNoteCode.READ_AS_WRITTEN in {note.code for note in plan.notes}
        [recording] = await _recordings(async_db, user.id)
        assert (recording.cns_end, recording.surface_pressure_bar, recording.salinity) == (9.0, 1.012, "en13319")
        profile = (
            await async_db.execute(select(DiveProfile).where(DiveProfile.recording_id == recording.id))
        ).scalar_one()
        assert profile.duration == 3_000_000
        dive = (await async_db.execute(select(Dive).where(Dive.user_id == user.id))).scalar_one()
        assert dive.water_type is None
        cylinder = (await async_db.execute(select(DiveMixture).where(DiveMixture.dive_id == dive.id))).scalar_one()
        assert cylinder.po2_limit == 1.4

    @pytest.mark.asyncio
    async def test_the_axis_is_read_in_milliseconds(self, db: Session, async_db: AsyncSession) -> None:
        """And a dive's duration derived from the span is whole seconds of it."""
        user = create_user(db)
        document = _document(
            {
                "started_at": "2026-08-01T10:00:00+02:00",
                "recordings": [
                    {"profile": {"duration": 2_940_160, "depth": {"times": [160, 2_940_160], "values": [0, 1900]}}}
                ],
            }
        )
        body = json.loads(document)
        del body["dives"][0]["duration"]

        await _apply(async_db, user.id, json.dumps(body).encode())

        [recording] = await _recordings(async_db, user.id)
        data = (
            await async_db.execute(select(DiveProfile.data).where(DiveProfile.recording_id == recording.id))
        ).scalar_one()
        assert data["depth"]["t"] == [160, 2_940_160]
        assert recording.duration == 2940
        dive = (await async_db.execute(select(Dive).where(Dive.user_id == user.id))).scalar_one()
        assert dive.duration == 2940

    @pytest.mark.asyncio
    async def test_each_recordings_readouts_and_salinity_are_its_own(self, db: Session, async_db: AsyncSession) -> None:
        user = create_user(db)
        document = _document(
            {
                "started_at": "2026-08-01T10:00:00+02:00",
                "recordings": [
                    {"device": {"brand": "Suunto"}, "salinity": "salt", "cns_end": 9.0, "surface_pressure": 1.012},
                    # A recording of readouts alone is a record (§3 rule 4)...
                    {"otu_end": 22.0},
                    # ...and one carrying only a setting is not.
                    {"salinity": "en13319"},
                ],
            }
        )

        plan = await _apply(async_db, user.id, document)

        recordings = await _recordings(async_db, user.id)
        assert [(row.salinity, row.cns_end, row.otu_end, row.surface_pressure_bar) for row in recordings] == [
            ("salt", 9.0, None, 1.012),
            (None, None, 22.0, None),
        ]
        assert ImportNoteCode.VALUE_DROPPED in {note.code for note in plan.notes}

    @pytest.mark.asyncio
    async def test_a_date_only_dive_is_skipped_rather_than_placed_at_midnight(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        user = create_user(db)

        plan = await _apply(async_db, user.id, _document({"started_at": "2002-06-18"}))

        assert [note.code for note in plan.notes] == [ImportNoteCode.RECORD_SKIPPED]
        assert "no time of day" in plan.notes[0].message
        assert (await async_db.execute(select(Dive.id).where(Dive.user_id == user.id))).first() is None

    @pytest.mark.asyncio
    async def test_a_recordings_start_with_no_time_of_day_is_read_as_absent(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """A recording's start is a date-time; a bare date there falls back to the dive's,
        with a note, rather than reading as midnight."""
        user = create_user(db)
        document = _document(
            {"started_at": "2026-08-01T10:00:00+02:00", "recordings": [{"started_at": "2026-08-01", "cns_end": 4.0}]}
        )

        plan = await _apply(async_db, user.id, document)

        [recording] = await _recordings(async_db, user.id)
        assert recording.start_time is not None
        assert (recording.utc_offset_minutes, recording.start_time.hour) == (120, 8)
        assert ImportNoteCode.VALUE_DROPPED in {note.code for note in plan.notes}

    @pytest.mark.asyncio
    async def test_a_long_note_is_read_whole_and_one_past_the_cap_is_cut_there(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        user = create_user(db)
        long, too_long = "a" * 20_000, "b" * (NOTES_MAX_LENGTH + 5)
        body = json.loads(_document({"started_at": "2026-08-01T10:00:00+02:00", "notes": long}))
        body["dives"].append({**body["dives"][0], "uuid": str(uuid_pkg.uuid4()), "notes": too_long})

        plan = await _apply(async_db, user.id, json.dumps(body).encode())

        notes = sorted((await async_db.execute(select(Dive.notes).where(Dive.user_id == user.id))).scalars(), key=len)
        assert [len(text) for text in notes] == [20_000, NOTES_MAX_LENGTH]
        assert ImportNoteCode.VALUE_DROPPED in {note.code for note in plan.notes}


SSRF = b"""<divelog program='subsurface' version='3'>
<dives>
<dive number='1' date='2026-07-07' time='10:05:00' duration='61:00 min'>
  <divecomputer model='Shearwater Perdix' last-manual-time='61:00 min'>
  <depth max='12.4 m' mean='8.3 m' />
  <sample time='0:00 min' depth='0.0 m' />
  <sample time='31:35 min' depth='12.4 m' />
  <sample time='61:00 min' depth='0.0 m' />
  </divecomputer>
</dive>
<dive number='2' otu='9' cns='6%' date='2026-07-11' time='09:45:00' duration='58:00 min'>
  <divecomputer last-manual-time='58:00 min'>
  <depth max='14.9 m' mean='9.1 m' />
  </divecomputer>
</dive>
</dives>
</divelog>
"""


@pytestmark_db
class TestAConvertedUpload:
    @pytest.mark.asyncio
    async def test_it_arrives_on_the_millisecond_axis_with_its_readouts_on_a_recording(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The installed converter writes the format as it now stands, which is what proves the
        dependency floor moved: a `.ssrf` dive's `@cns` lands on a recording of readouts alone,
        and a sample at 31:35 lands at 1 895 000 ms."""
        user = create_user(db)

        await _apply(async_db, user.id, SSRF, filename="logbook.ssrf")

        profiles = (
            await async_db.execute(select(DiveProfile.data).join(Dive).where(Dive.user_id == user.id))
        ).scalars()
        assert [data["depth"]["t"] for data in profiles] == [[0, 1_895_000, 3_660_000]]
        readouts = [
            (row.cns_end, row.otu_end) for row in await _recordings(async_db, user.id) if row.cns_end is not None
        ]
        assert readouts == [(6.0, 9.0)]
