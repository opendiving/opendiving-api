"""An import of many files, against a live Postgres: its files imported one at a time.

`services/logbook_import/batch.py` plans each file of a batch against the logbook the files
before it left and writes it before the next, in one transaction - so what two files make of
each other is what the import already makes of a file and a stored recording. These pin that
it is: that one batch and the same files one at a time write the same logbook, that a
computer's two exports of one dive are one recording holding both files whichever way they
arrive, that the recording is the one the dive form stores, and that the report's rows say
what happened to each file and each dive.

The files are the package's own fixtures (`tests/fixtures/dive_files/`), beside inline
Suunto app JSON and UDDF for the cases a fixture does not cover.
"""

import hashlib
import io
import uuid as uuid_pkg
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import divejson
import pytest
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, undefer

from src.app.core.security import create_dive_file_token
from src.app.crud.crud_dive_mixtures import replace_mixtures_for_dive
from src.app.models.dive import Dive
from src.app.models.dive_file import DiveFile
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_recording import DiveRecording
from src.app.models.user import User
from src.app.schemas.dive_mixture import DiveMixtureCreate
from src.app.schemas.logbook_import import ImportDiveOutcome, ImportMemberNotKept, ImportNoteCode
from src.app.services import blob_store
from src.app.services.dive_files import store_recording_file
from src.app.services.dive_profiles import backfill_profiles
from src.app.services.dive_reader import read_prefill
from src.app.services.dive_recordings import DECO_MODEL_COLUMNS, DEVICE_COLUMNS, READOUT_COLUMNS
from src.app.services.logbook_import import BatchReport, import_batch, load_import
from tests.conftest import db_available
from tests.helpers.dive_files import suunto_json
from tests.helpers.generators import create_dive, create_user
from tests.helpers.import_parts import parts_of

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

FIXTURES = Path(__file__).parent / "fixtures" / "dive_files"

# What a recording is, for the door: who recorded it, how it was set, what it read out, and
# the two figures the strict gate compares, beside its start.
RECORDING_COLUMNS = (
    *DEVICE_COLUMNS.values(),
    *DECO_MODEL_COLUMNS.values(),
    *READOUT_COLUMNS,
    "mode",
    "salinity",
    "duration",
    "max_depth",
    "start_time",
    "utc_offset_minutes",
    "ordinal",
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


OCEAN_FIT = _fixture("suunto-ocean-2026.fit")
OCEAN_JSON = _fixture("suunto-ocean-2026.json")
TWO_TANK_FIT = _fixture("suunto-ocean.fit")
TWO_TANK_JSON = _fixture("suunto-ocean.json")
PAIR = [("dive.fit", OCEAN_FIT), ("dive.json", OCEAN_JSON)]


def _unique_export(start: str = "2026-09-10T09:30:00.000+03:00") -> bytes:
    """A Suunto app export no other account of the suite's database has imported.

    A file's dive takes its identifier from its bytes, and an identifier another account
    already holds is minted afresh on every run - so a test about identity, a link or a
    restore needs bytes of its own, which a fixture shared across the suite cannot be.
    """
    return suunto_json(
        start=start,
        serial=str(uuid_pkg.uuid4().int)[:12],
        samples=((0, 0.0), (300, 12.0), (1500, 9.0), (1800, 0.0)),
    )


def _perdix_uddf(*, number: int = 8, start: str = "2026-09-08T15:17:50") -> bytes:
    """A Perdix 3's own UDDF of the Ocean pair's dive - a second computer on the same wrist.

    Its dive states the diver's number, notes and site, as a logbook's does, and one
    cylinder of the Ocean's 33 % mix; its samples span the Ocean's hour to within a minute,
    so the strict gate calls the two computers' records one dive.
    """
    waypoints = "".join(
        f"<waypoint><depth>{depth}</depth><divetime>{time}</divetime></waypoint>"
        for time, depth in ((0, 0.0), (600, 18.6), (1800, 12.0), (3400, 5.0), (3440, 0.0))
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<uddf xmlns="http://www.streit.cc/uddf/3.2/" version="3.2.3">
  <generator><name>Shearwater Cloud Desktop</name><type>logbook</type><version>2.12.10</version></generator>
  <diver><owner><equipment>
    <divecomputer id="dc1"><name>Perdix 3</name>
      <manufacturer id="sw"><name>Shearwater Research, Inc</name></manufacturer>
      <model>Perdix 3</model><serialnumber>D9772626</serialnumber></divecomputer>
  </equipment></owner></diver>
  <divesite><site id="site-1"><name>Porvoo Wall</name></site></divesite>
  <gasdefinitions><mix id="ean33"><name>EAN33</name><o2>0.33</o2><he>0</he></mix></gasdefinitions>
  <profiledata><repetitiongroup id="rg-1">
    <dive id="perdix-dive-8">
      <informationbeforedive>
        <link ref="site-1"/>
        <divenumber>{number}</divenumber>
        <datetime>{start}</datetime>
        <equipmentused><link ref="dc1"/></equipmentused>
      </informationbeforedive>
      <tankdata><link ref="ean33"/>
        <tankpressurebegin>20000000</tankpressurebegin><tankpressureend>6000000</tankpressureend></tankdata>
      <samples>{waypoints}</samples>
      <informationafterdive>
        <greatestdepth>18.6</greatestdepth><diveduration>3440</diveduration>
        <notes><para>Along the wall.</para></notes>
      </informationafterdive>
    </dive>
  </repetitiongroup></profiledata>
</uddf>
""".encode()


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


@pytest.fixture
def volume(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backfill run ends by dropping the touched accounts' dive caches, and there is no
    Redis here to drop them in."""
    monkeypatch.setattr("src.app.services.cache_invalidation.invalidate_dive_caches", AsyncMock())


async def _import(db: AsyncSession, user: User, files: list[tuple[str, bytes]], *, apply: bool = True) -> BatchReport:
    """One request's worth of files, previewed or applied as the routes do."""
    with await load_import(parts_of(*files)) as batch:
        report = await import_batch(db, user_id=user.id, batch=batch, apply=apply)
        if apply:
            await db.commit()
        return report


async def _one_at_a_time(db: AsyncSession, user: User, files: list[tuple[str, bytes]]) -> None:
    for file in files:
        await _import(db, user, [file])


async def _attach(db: AsyncSession, user: User, dive: Dive, content: bytes, name: str) -> None:
    """`POST /dive/{uuid}/recordings`, with the token the parse would have minted."""
    fmt = divejson.sniff(content[: divejson.SNIFF_BYTES])
    assert fmt is not None
    await store_recording_file(
        db,
        user_id=user.id,
        user_uuid=user.uuid,
        dive_id=dive.id,
        upload=UploadFile(filename=name, file=io.BytesIO(content)),
        file_token=create_dive_file_token(
            user_uuid=user.uuid, sha256=hashlib.sha256(content).hexdigest(), parser_key=fmt
        ),
    )


async def _on_the_form(db: AsyncSession, sync_db: Session, user: User, files: list[tuple[str, bytes]]) -> Dive:
    """The dive form: the first file's prefill saved as a dive, then each file attached."""
    dive = create_dive(sync_db, user)
    _, parsed = read_prefill(files[0][1])
    await replace_mixtures_for_dive(
        db=db, dive_id=dive.id, mixtures=[DiveMixtureCreate(**row.model_dump()) for row in parsed.mixtures]
    )
    await db.commit()
    for name, content in files:
        await _attach(db, user, dive, content, name)
    return dive


async def _dive_ids(db: AsyncSession, user: User) -> list[int]:
    rows = await db.execute(
        select(Dive.id).where(Dive.user_id == user.id, Dive.is_deleted.is_(False)).order_by(Dive.start_time)
    )
    return list(rows.scalars())


async def _recording(db: AsyncSession, recording: DiveRecording) -> dict[str, Any]:
    """A recording as the door compares it: its columns, its files in attach order, and its
    profile's samples and key."""
    files = await db.execute(
        select(DiveFile.sha256, DiveFile.parser_key, DiveFile.content_type)
        .where(DiveFile.recording_id == recording.id)
        .order_by(DiveFile.id)
    )
    profile = (
        await db.execute(
            select(DiveProfile)
            .where(DiveProfile.recording_id == recording.id)
            .options(undefer(DiveProfile.data))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return {
        "columns": {column: getattr(recording, column) for column in RECORDING_COLUMNS},
        "files": [tuple(row) for row in files],
        "profile": None
        if profile is None
        else {
            "data": profile.data,
            "parser_key": profile.parser_key,
            "source_sha256": profile.source_sha256,
            "reader_version": profile.reader_version,
            "extractor_version": profile.extractor_version,
            "duration": profile.duration,
        },
    }


async def _dive(db: AsyncSession, dive_id: int) -> dict[str, Any]:
    """A dive as two imports are compared: its figures, its cylinders' labels and mixes, and
    its recordings - row ids and identifiers aside."""
    dive = (
        await db.execute(select(Dive).where(Dive.id == dive_id).execution_options(populate_existing=True))
    ).scalar_one()
    recordings = (
        await db.execute(
            select(DiveRecording)
            .where(DiveRecording.dive_id == dive_id)
            .order_by(DiveRecording.ordinal)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    cylinders = await db.execute(
        select(DiveMixture.oxygen, DiveMixture.gas_number, DiveMixture.start_pressure)
        .where(DiveMixture.dive_id == dive_id)
        .order_by(DiveMixture.id)
    )
    return {
        "dive": (dive.dive_number, dive.start_time, dive.utc_offset_minutes, dive.duration, dive.max_depth),
        "fixes": (dive.entry_latitude, dive.entry_longitude, dive.exit_latitude, dive.exit_longitude),
        "cylinders": [tuple(row) for row in cylinders],
        "recordings": [await _recording(db, recording) for recording in list(recordings)],
    }


async def _logbook(db: AsyncSession, user: User) -> list[dict[str, Any]]:
    return [await _dive(db, dive_id) for dive_id in await _dive_ids(db, user)]


async def _count(db: AsyncSession, model: Any, user: User) -> int:
    return int((await db.execute(select(func.count()).select_from(model).where(model.user_id == user.id))).scalar_one())


class TestThePair:
    """The owner's own case: a Suunto Ocean's FIT and JSON of one dive."""

    @pytest.mark.asyncio
    async def test_the_pair_in_one_import_is_one_dive_with_both_files_kept(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)

        report = await _import(async_db, diver, PAIR)

        [dive] = await _logbook(async_db, diver)
        [recording] = dive["recordings"]
        assert [parser_key for _, parser_key, _ in recording["files"]] == ["fit", "suunto_json"]
        assert recording["columns"]["device_serial"] == "253810000400"
        assert recording["columns"]["device_model"] == "Suunto Ocean"
        assert recording["columns"]["device_name"] == "Porvoo"
        assert recording["profile"]["parser_key"] == "fit"
        assert recording["profile"]["data"]["pressure"], "the JSON's pressure channel joins the FIT's"
        assert [label for _, label, _ in dive["cylinders"]] == [0]
        assert [series["gas_number"] for series in recording["profile"]["data"]["pressure"]] == [0]
        assert dive["dive"][0] == 0, "no dive number is invented"

        assert [row.kept for row in report.members] == [True, True]
        [row] = report.dives
        assert row.outcome is ImportDiveOutcome.CREATED
        assert row.members == [0, 1]
        assert row.device is not None and row.device.serial == "253810000400"
        assert not {note.code for note in report.notes} & {
            ImportNoteCode.RECORDING_FILLED,
            ImportNoteCode.RECORDING_ATTACHED,
        }, "the logbook does not already have a recording the diver dropped a moment ago"


def _ssrf_of_two_computers_as_two_dives() -> bytes:
    """One Subsurface logbook holding two dives the strict gate would call one - the diver
    kept the Ocean's and the Perdix's records of one dive apart."""
    return b"""<divelog program='subsurface' version='3'>
<dives>
<dive number='1' date='2026-09-08' time='15:17:38' duration='50:51 min'>
  <divecomputer model='Suunto Ocean' deviceid='9c1f04ab' date='2026-09-08' time='15:17:38'>
  <depth max='19.04 m' />
  <sample time='0:00 min' depth='0.0 m' />
  <sample time='10:00 min' depth='19.04 m' />
  <sample time='50:51 min' depth='0.0 m' />
  </divecomputer>
</dive>
<dive number='2' date='2026-09-08' time='15:18:10' duration='50:19 min'>
  <divecomputer model='Shearwater Perdix 3' deviceid='d9772626' date='2026-09-08' time='15:18:10'>
  <depth max='19.6 m' />
  <sample time='0:00 min' depth='0.0 m' />
  <sample time='9:28 min' depth='19.6 m' />
  <sample time='50:19 min' depth='0.0 m' />
  </divecomputer>
</dive>
</dives>
</divelog>
"""


class TestTheBatchIsItsFilesInOrder:
    """A batch writes what its files would write imported one at a time, in its order."""

    FILES = [
        *PAIR,
        ("two-tank.fit", TWO_TANK_FIT),
        ("two-tank.json", TWO_TANK_JSON),
        ("d5.json", _fixture("suunto-d5.json")),
        ("perdix.uddf", _perdix_uddf()),
    ]

    @staticmethod
    def _in_batch_order(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
        """Logbooks ahead of a computer's files, each by name - the order the batch reads in."""
        return sorted(files, key=lambda file: (not file[0].endswith(".uddf"), file[0]))

    @pytest.mark.asyncio
    async def test_one_request_and_one_file_at_a_time_write_the_same_logbook(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        together, apart = create_user(db), create_user(db)

        await _import(async_db, together, self.FILES)
        await _one_at_a_time(async_db, apart, self._in_batch_order(self.FILES))

        assert await _logbook(async_db, together) == await _logbook(async_db, apart)
        assert len(await _dive_ids(async_db, together)) == 3

    @pytest.mark.asyncio
    async def test_the_preview_counts_what_the_apply_writes(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        """Each file is counted as its own import would count it, after the files before it
        - so the pair's JSON is a dive skipped, its recording having filled the FIT's."""
        diver = create_user(db)

        preview = await _import(async_db, diver, self.FILES, apply=False)
        assert await _dive_ids(async_db, diver) == [], "a preview writes nothing"
        result = await _import(async_db, diver, self.FILES)

        [dives] = [report for report in result.collections if report.collection == "dives"]
        assert (dives.created, dives.skipped) == (3, 3)
        assert preview.collections == result.collections
        assert [(row.outcome, row.members) for row in preview.dives] == [
            (row.outcome, row.members) for row in result.dives
        ]
        stored = (await async_db.execute(select(Dive.uuid).where(Dive.user_id == diver.id))).scalars()
        assert {row.uuid for row in result.dives} == set(stored)

    @pytest.mark.asyncio
    async def test_two_dives_of_one_logbook_stay_two_whatever_the_gates_would_say(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        """Two dives of one file are never matched against each other: the diver kept them
        apart."""
        diver = create_user(db)

        await _import(async_db, diver, [("log.ssrf", _ssrf_of_two_computers_as_two_dives())])

        assert len(await _dive_ids(async_db, diver)) == 2

    @pytest.mark.asyncio
    async def test_a_loose_file_beside_the_archive_of_its_dive_adds_nothing(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        from datetime import UTC, datetime

        from src.app.services.export import load_export_bundle
        from src.app.services.export.archive import write_archive

        source, destination = create_user(db), create_user(db)
        await _import(async_db, source, PAIR)
        bundle = await load_export_bundle(async_db, user_id=source.id)
        spool = await write_archive(async_db, bundle, exported_at=datetime.now(UTC))
        archive = spool.read()
        spool.close()

        report = await _import(async_db, destination, [("logbook.zip", archive), ("dive.fit", OCEAN_FIT)])

        [dive] = await _logbook(async_db, destination)
        [recording] = dive["recordings"]
        assert [parser_key for _, parser_key, _ in recording["files"]] == ["fit", "suunto_json"]
        rows = {row.name: row for row in report.members}
        assert rows["logbook.zip"].format == "archive"
        assert (rows["dive.fit"].kept, rows["dive.fit"].not_kept) == (False, ImportMemberNotKept.ALREADY_STORED)


class TestIdempotence:
    @pytest.mark.asyncio
    async def test_the_same_batch_again_writes_nothing(self, volume: Any, async_db: AsyncSession, db: Session) -> None:
        """Each file's dive is in the logbook under the identifier its bytes gave it - or,
        for a pair's second file, reached through the gates onto a recording that holds it
        already."""
        diver = create_user(db)
        files = [
            ("a.json", _unique_export()),
            ("b.json", _unique_export("2026-09-11T09:30:00.000+03:00")),
            *PAIR,
        ]
        await _import(async_db, diver, files)
        before = await _logbook(async_db, diver)
        counts = [await _count(async_db, model, diver) for model in (Dive, DiveRecording, DiveFile)]
        profiles = list(
            (
                await async_db.execute(
                    select(DiveProfile.id, DiveProfile.updated_at)
                    .join(Dive, Dive.id == DiveProfile.dive_id)
                    .where(Dive.user_id == diver.id)
                    .order_by(DiveProfile.id)
                )
            ).all()
        )
        keys = set(blob_store.iter_keys())

        report = await _import(async_db, diver, files)

        assert await _logbook(async_db, diver) == before
        assert [await _count(async_db, model, diver) for model in (Dive, DiveRecording, DiveFile)] == counts
        assert (
            list(
                (
                    await async_db.execute(
                        select(DiveProfile.id, DiveProfile.updated_at)
                        .join(Dive, Dive.id == DiveProfile.dive_id)
                        .where(Dive.user_id == diver.id)
                        .order_by(DiveProfile.id)
                    )
                ).all()
            )
            == profiles
        ), "no profile is rewritten"
        assert set(blob_store.iter_keys()) == keys, "no object is written"
        assert [row.outcome for row in report.dives][:2] == [ImportDiveOutcome.LINKED, ImportDiveOutcome.LINKED]
        assert {row.not_kept for row in report.members} == {ImportMemberNotKept.ALREADY_STORED}
        assert {row.kept for row in report.members} == {False}

    @pytest.mark.asyncio
    async def test_the_same_pair_twice_in_one_import_is_one_dive(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)

        report = await _import(async_db, diver, [*PAIR, ("copy of dive.fit", OCEAN_FIT)])

        [dive] = await _logbook(async_db, diver)
        assert len(dive["recordings"][0]["files"]) == 2
        [row] = report.dives
        assert row.outcome is ImportDiveOutcome.CREATED and sorted(row.members) == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_two_bare_fit_files_of_two_dives_are_two_dives(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        """A dive-computer file's dive takes its identity from its bytes, so a second dive's
        file is never "already in your logbook" because it is a file of the same format."""
        together, apart = create_user(db), create_user(db)

        await _import(async_db, together, [("a.fit", OCEAN_FIT), ("b.fit", TWO_TANK_FIT)])
        await _import(async_db, apart, [("dive.fit", OCEAN_FIT)])
        second = await _import(async_db, apart, [("dive.fit", TWO_TANK_FIT)])

        assert len(await _dive_ids(async_db, together)) == 2
        assert len(await _dive_ids(async_db, apart)) == 2
        assert [row.outcome for row in second.dives] == [ImportDiveOutcome.CREATED]

    @pytest.mark.asyncio
    async def test_the_identity_follows_the_bytes_and_not_the_name(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        export = _unique_export()
        [first] = (await _import(async_db, diver, [("dive.json", export)])).dives

        [again] = (await _import(async_db, diver, [("Suunto/renamed.json", export)])).dives

        assert again.outcome is ImportDiveOutcome.LINKED
        assert again.uuid == first.uuid


class TestTheDoorDoesNotMatter:
    """The same files in the same order give equal recordings whether they arrive in one
    batch, in two imports or on the dive form - stored files, device, settings, readouts,
    gate figures, samples, labels and the profile's key - and the dive's cylinders equal
    labels."""

    @staticmethod
    async def _recordings_and_labels(db: AsyncSession, dive_id: int) -> tuple[Any, Any]:
        dive = await _dive(db, dive_id)
        return dive["recordings"], [label for _, label, _ in dive["cylinders"]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fit", "json"),
        [(OCEAN_FIT, OCEAN_JSON), (TWO_TANK_FIT, TWO_TANK_JSON)],
        ids=["one cylinder", "two cylinders"],
    )
    @pytest.mark.parametrize("fit_first", [True, False], ids=["FIT first", "JSON first"])
    async def test_one_batch_two_imports_and_the_form_store_one_recording(
        self, volume: Any, async_db: AsyncSession, db: Session, fit: bytes, json: bytes, fit_first: bool
    ) -> None:
        files = [("a.fit", fit), ("b.json", json)] if fit_first else [("a.json", json), ("b.fit", fit)]
        batch, days, form = create_user(db), create_user(db), create_user(db)

        await _import(async_db, batch, files)
        await _one_at_a_time(async_db, days, files)
        on_the_form = await _on_the_form(async_db, db, form, files)

        [batch_dive], [days_dive] = await _dive_ids(async_db, batch), await _dive_ids(async_db, days)
        from_the_batch = await self._recordings_and_labels(async_db, batch_dive)
        assert from_the_batch == await self._recordings_and_labels(async_db, days_dive)
        assert from_the_batch == await self._recordings_and_labels(async_db, on_the_form.id)
        [recording] = from_the_batch[0]
        assert recording["profile"]["parser_key"] == ("fit" if fit_first else "suunto_json")

    @pytest.mark.asyncio
    async def test_a_one_dive_uddf_imported_and_picked_on_the_form_are_one_recording(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        files = [("perdix.uddf", _perdix_uddf())]
        imported, form = create_user(db), create_user(db)

        report = await _import(async_db, imported, files)
        on_the_form = await _on_the_form(async_db, db, form, files)

        [dive_id] = await _dive_ids(async_db, imported)
        assert await self._recordings_and_labels(async_db, dive_id) == await self._recordings_and_labels(
            async_db, on_the_form.id
        )
        assert report.members[0].kept


class TestReproducible:
    @pytest.mark.asyncio
    async def test_a_forced_backfill_rewrites_no_profile_an_import_wrote(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        """A pair, a second computer beside a one-dive UDDF, and a two-cylinder pair: every
        profile the import stored is what the re-derivation stores from the same files."""
        diver = create_user(db)
        await _import(
            async_db,
            diver,
            [("perdix.uddf", _perdix_uddf()), *PAIR, ("two-tank.fit", TWO_TANK_FIT), ("two-tank.json", TWO_TANK_JSON)],
        )
        before = await _logbook(async_db, diver)

        await backfill_profiles(async_db, force=True)

        assert await _logbook(async_db, diver) == before


class TestTwoComputers:
    FILES = [("perdix.uddf", _perdix_uddf()), *PAIR]

    @pytest.mark.asyncio
    async def test_two_computers_in_one_import_are_one_dive_with_two_recordings(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        """The logbook's dive ranks first and keeps its number, notes and site; the Ocean's
        recording joins it second, its pressure channel naming a cylinder the dive has."""
        diver = create_user(db)

        report = await _import(async_db, diver, self.FILES)

        [dive_id] = await _dive_ids(async_db, diver)
        dive = await _dive(async_db, dive_id)
        perdix, ocean = dive["recordings"]
        assert [parser_key for _, parser_key, _ in perdix["files"]] == ["uddf"]
        assert [parser_key for _, parser_key, _ in ocean["files"]] == ["fit", "suunto_json"]
        assert dive["dive"][0] == 8
        stored = (await async_db.execute(select(Dive).where(Dive.id == dive_id))).scalar_one()
        assert stored.notes == "Along the wall."
        labels = {label for _, label, _ in dive["cylinders"]}
        assert {series["gas_number"] for series in ocean["profile"]["data"]["pressure"]} <= labels
        [row] = report.dives
        assert (row.outcome, row.members) == (ImportDiveOutcome.CREATED, [0, 1, 2])
        assert row.device is not None and row.device.model == "Perdix 3"

    @pytest.mark.asyncio
    async def test_on_two_days_they_are_the_same_dive(self, volume: Any, async_db: AsyncSession, db: Session) -> None:
        together, apart = create_user(db), create_user(db)

        await _import(async_db, together, self.FILES)
        await _import(async_db, apart, self.FILES[:1])
        report = await _import(async_db, apart, self.FILES[1:])

        assert await _logbook(async_db, together) == await _logbook(async_db, apart)
        [row] = report.dives
        assert (row.outcome, row.files_added, row.recordings_added) == (ImportDiveOutcome.UPDATED, 2, 1)
        assert ImportNoteCode.RECORDING_ATTACHED in {note.code for note in report.notes}


class TestTheFilesKept:
    @pytest.mark.asyncio
    async def test_a_logbook_of_several_dives_stores_no_file_and_says_why(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)

        report = await _import(async_db, diver, [("log.ssrf", _ssrf_of_two_computers_as_two_dives())])

        assert await _count(async_db, DiveFile, diver) == 0
        assert (report.members[0].kept, report.members[0].not_kept) == (False, ImportMemberNotKept.SEVERAL_DIVES)

    @pytest.mark.asyncio
    async def test_a_file_of_two_computers_records_stores_no_file(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)

        report = await _import(async_db, diver, [("two.ssrf", _fixture("two-computers.ssrf"))])

        assert await _count(async_db, DiveFile, diver) == 0
        assert report.members[0].not_kept is ImportMemberNotKept.SEVERAL_RECORDINGS

    @pytest.mark.asyncio
    async def test_a_file_over_the_size_cap_converts_and_is_not_stored(
        self, volume: Any, async_db: AsyncSession, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.app.services.logbook_import import reader

        monkeypatch.setattr(reader, "MAX_DIVE_FILE_SIZE", len(OCEAN_FIT) - 1)
        diver = create_user(db)

        report = await _import(async_db, diver, [("dive.fit", OCEAN_FIT)])

        [dive] = await _logbook(async_db, diver)
        assert dive["recordings"][0]["files"] == []
        assert dive["recordings"][0]["profile"]["parser_key"] == "divejson_import"
        assert report.members[0].not_kept is ImportMemberNotKept.TOO_LARGE

    @pytest.mark.asyncio
    async def test_a_file_another_recording_holds_is_skipped_with_the_note(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        """One set of bytes is one row of one recording, so a copy the gates do not place on
        that recording is not stored again."""
        diver = create_user(db)
        holder = create_dive(db, diver)
        await _attach(async_db, diver, holder, OCEAN_FIT, "dive.fit")
        # Moved off the file's own clock, so no gate places the import on it.
        recording = (
            await async_db.execute(select(DiveRecording).where(DiveRecording.dive_id == holder.id))
        ).scalar_one()
        assert recording.start_time is not None
        recording.start_time = recording.start_time.replace(year=2020)
        await async_db.commit()

        report = await _import(async_db, diver, [("dive.fit", OCEAN_FIT)])

        assert await _count(async_db, DiveFile, diver) == 1
        assert report.members[0].not_kept is ImportMemberNotKept.ALREADY_STORED
        assert ImportNoteCode.FILE_SKIPPED in {note.code for note in report.notes}
        assert [row.outcome for row in report.dives] == [ImportDiveOutcome.CREATED]

    @pytest.mark.asyncio
    async def test_a_file_the_recording_it_reaches_holds_adds_nothing_and_notes_nothing(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        await _on_the_form(async_db, db, diver, [("dive.fit", OCEAN_FIT)])

        report = await _import(async_db, diver, [("dive.fit", OCEAN_FIT)])

        assert await _count(async_db, DiveFile, diver) == 1
        assert report.members[0].not_kept is ImportMemberNotKept.ALREADY_STORED
        assert ImportNoteCode.FILE_SKIPPED not in {note.code for note in report.notes}
        [row] = report.dives
        assert row.outcome is ImportDiveOutcome.SKIPPED and row.reason is not None


class TestAFileOnAFileLessRecording:
    @pytest.mark.asyncio
    async def test_its_profile_becomes_the_files(self, volume: Any, async_db: AsyncSession, db: Session) -> None:
        """A recording a logbook brought with its samples and no file, then given its
        computer's file, takes that file's profile under the file's key - as the form's
        attach gives it one."""
        import json as json_module

        diver = create_user(db)
        converted = divejson.convert(OCEAN_FIT, format="fit").document
        second = json_module.loads(json_module.dumps(converted["dives"][0]))
        second["uuid"] = "0a8f6c1e-3b8d-4c2a-9d1e-5f4a3b2c1d0e"
        second["started_at"] = "2026-09-09T10:00:00+03:00"
        second["recordings"][0]["started_at"] = "2026-09-09T10:00:00+03:00"
        converted["dives"].append(second)
        await _import(async_db, diver, [("logbook.divejson", json_module.dumps(converted).encode())])

        report = await _import(async_db, diver, [("dive.fit", OCEAN_FIT)])

        first, _ = await _logbook(async_db, diver)
        [recording] = first["recordings"]
        assert [parser_key for _, parser_key, _ in recording["files"]] == ["fit"]
        assert recording["profile"]["parser_key"] == "fit"
        assert recording["profile"]["reader_version"] is not None
        [row] = report.dives
        assert (row.outcome, row.files_added) == (ImportDiveOutcome.UPDATED, 1)


class TestRowsAndRefusals:
    @pytest.mark.asyncio
    async def test_a_file_nothing_reads_is_a_refused_row_and_the_rest_imports(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        from src.app.services.dive_reader import formats_this_build_reads

        diver = create_user(db)

        report = await _import(async_db, diver, [*PAIR, ("notes.pdf", b"%PDF-1.7 not a logbook")])

        assert len(await _dive_ids(async_db, diver)) == 1
        refused = next(row for row in report.members if row.name == "notes.pdf")
        assert refused.format is None and refused.refusal is not None
        assert formats_this_build_reads() in refused.refusal

    @pytest.mark.asyncio
    async def test_a_run_in_fit_is_a_refused_row_carrying_the_package_s_message(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        from tests.helpers.fit import dive_fit_file

        diver = create_user(db)

        report = await _import(async_db, diver, [*PAIR, ("run.fit", dive_fit_file(sport="running"))])

        run = next(row for row in report.members if row.name == "run.fit")
        assert run.format == "fit"
        assert run.refusal is not None and "running activity, not a dive" in run.refusal
        assert len(await _dive_ids(async_db, diver)) == 1

    @pytest.mark.asyncio
    async def test_a_suunto_export_of_a_run_is_a_refused_row_saying_it_records_no_dive(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)

        report = await _import(async_db, diver, [*PAIR, ("run.json", _fixture("not-a-dive.json"))])

        run = next(row for row in report.members if row.name == "run.json")
        assert run.refusal is not None and "records no dive" in run.refusal

    @pytest.mark.asyncio
    async def test_a_zip_of_the_pair_is_opened_and_is_the_pair(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        loose, zipped = create_user(db), create_user(db)

        await _import(async_db, loose, PAIR)
        report = await _import(
            async_db, zipped, [("Suunto.zip", _zip({f"Suunto/{name}": data for name, data in PAIR}))]
        )

        assert await _logbook(async_db, loose) == await _logbook(async_db, zipped)
        rows = report.members
        zip_row = next(row for row in rows if row.format == "zip")
        assert zip_row.opened == 2 and zip_row.container is None
        members = [row for row in rows if row.container is not None]
        assert [row.name for row in members] == ["Suunto/dive.fit", "Suunto/dive.json"]
        assert {row.container for row in members} == {rows.index(zip_row)}
        assert {row.part for row in rows} == {0}
        files = (
            await async_db.execute(select(DiveFile.original_filename).where(DiveFile.user_id == zipped.id))
        ).scalars()
        assert sorted(files) == ["dive.fit", "dive.json"]

    @pytest.mark.asyncio
    async def test_a_zip_inside_a_zip_is_a_refused_row(self, volume: Any, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)

        report = await _import(
            async_db,
            diver,
            [("outer.zip", _zip({"dive.fit": OCEAN_FIT, "inner.zip": _zip({"dive.json": OCEAN_JSON})}))],
        )

        inner = next(row for row in report.members if row.name == "inner.zip")
        assert inner.refusal is not None and "on its own" in inner.refusal
        assert len(await _dive_ids(async_db, diver)) == 1

    @pytest.mark.asyncio
    async def test_hidden_and_empty_files_are_ignored(self, volume: Any, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)

        report = await _import(
            async_db,
            diver,
            [
                *PAIR,
                (".DS_Store", b"\x00\x00\x00\x01Bud1"),
                ("empty.fit", b""),
                (
                    "more.zip",
                    _zip(
                        {
                            "__MACOSX/._dive.fit": b"x",
                            ".hidden": b"x",
                            "blank.json": b"",
                            "d5.json": _fixture("suunto-d5.json"),
                        }
                    ),
                ),
            ],
        )

        assert sorted(row.name for row in report.members) == ["d5.json", "dive.fit", "dive.json", "more.zip"]
        assert next(row for row in report.members if row.name == "more.zip").opened == 1

    @pytest.mark.asyncio
    async def test_an_import_no_file_of_which_reads_answers_as_its_first_file(self) -> None:
        from src.app.services.logbook_import import UnsupportedImportError
        from src.app.services.logbook_import.reader import MalformedImportError

        with pytest.raises(UnsupportedImportError):
            await load_import(parts_of(("a.pdf", b"%PDF-1.7"), ("b.txt", b"not a logbook")))
        with pytest.raises(MalformedImportError, match="records no dive"):
            await load_import(parts_of(("run.json", _fixture("not-a-dive.json"))))


class TestTheDiveRows:
    @pytest.mark.asyncio
    async def test_a_dive_deleted_since_is_brought_back(self, volume: Any, async_db: AsyncSession, db: Session) -> None:
        from src.app.services.dive_files import delete_files_for_dive

        diver = create_user(db)
        files = [("dive.json", _unique_export())]
        [created] = (await _import(async_db, diver, files)).dives
        dive = (await async_db.execute(select(Dive).where(Dive.uuid == created.uuid))).scalar_one()
        await delete_files_for_dive(async_db, dive_id=dive.id, commit=False)
        dive.is_deleted = True
        await async_db.commit()

        preview = await _import(async_db, diver, files, apply=False)
        [restored] = (await _import(async_db, diver, files)).dives

        assert [row.outcome for row in preview.dives] == [ImportDiveOutcome.RESTORED]
        assert (restored.outcome, restored.uuid) == (ImportDiveOutcome.RESTORED, created.uuid)
        [back] = await _logbook(async_db, diver)
        assert len(back["recordings"][0]["files"]) == 1

    @pytest.mark.asyncio
    async def test_the_result_carries_the_preview_s_uuids(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        files = [("a.json", _unique_export()), ("b.json", _unique_export("2026-09-11T09:30:00.000+03:00"))]

        preview = await _import(async_db, diver, files, apply=False)
        result = await _import(async_db, diver, files)

        assert [row.uuid for row in result.dives] == [row.uuid for row in preview.dives]
        assert None not in [row.uuid for row in result.dives]

    @pytest.mark.asyncio
    async def test_a_skipped_dive_carries_its_reason_and_its_own_figures(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        import json as json_module

        diver = create_user(db)
        document = {
            "format": "divejson",
            "version": "1.0",
            "exported_at": "2026-09-30T10:00:00Z",
            "dives": [{"uuid": "5d0c9a6e-7f1b-4c3d-8e2a-1b0c9d8e7f6a", "max_depth": 12.5}],
        }

        report = await _import(async_db, diver, [("log.divejson", json_module.dumps(document).encode())])

        [row] = report.dives
        assert (row.outcome, row.uuid, row.max_depth) == (ImportDiveOutcome.SKIPPED, None, 12.5)
        assert row.reason is not None and "start time" in row.reason


class TestAFolderAfterItsFitFiles:
    @pytest.mark.asyncio
    async def test_the_fit_links_and_the_json_adds_its_file(
        self, volume: Any, async_db: AsyncSession, db: Session
    ) -> None:
        """The FIT files imported one day and the whole folder dropped the next: each dive
        gains its JSON, reached through the gates onto the recording its FIT made."""
        diver = create_user(db)
        await _import(async_db, diver, PAIR[:1])

        report = await _import(async_db, diver, PAIR)

        [dive] = await _logbook(async_db, diver)
        assert [parser_key for _, parser_key, _ in dive["recordings"][0]["files"]] == ["fit", "suunto_json"]
        [row] = report.dives
        assert (row.outcome, row.files_added, row.recordings_added, row.members) == (
            ImportDiveOutcome.UPDATED,
            1,
            0,
            [0, 1],
        )


class TestOneArchive:
    @pytest.mark.asyncio
    async def test_a_second_archive_is_a_refused_row(self, volume: Any, async_db: AsyncSession, db: Session) -> None:
        from datetime import UTC, datetime

        from src.app.services.export import load_export_bundle
        from src.app.services.export.archive import write_archive

        source, destination = create_user(db), create_user(db)
        await _import(async_db, source, [("dive.json", _unique_export())])
        bundle = await load_export_bundle(async_db, user_id=source.id)
        spool = await write_archive(async_db, bundle, exported_at=datetime.now(UTC))
        archive = spool.read()
        spool.close()

        report = await _import(async_db, destination, [("a.zip", archive), ("b.zip", archive)])

        first, second = (next(row for row in report.members if row.name == name) for name in ("a.zip", "b.zip"))
        assert (first.format, first.refusal) == ("archive", None)
        assert second.format == "archive" and second.refusal is not None and "on its own" in second.refusal
        assert report.archive
        assert len(await _dive_ids(async_db, destination)) == 1
