"""One reader against a live Postgres: the door does not matter, and a label is the reader's.

Every fact here needs real rows to be true at all - the attach and the import each writing a
recording of their own, the dive's cylinders renumbered in place, a sibling's stored profile
rewritten under a fresh identity - so these run against the suite's database, with the same
skip-if-unreachable guard and write-real-rows-and-leave-them convention as
`test_dive_check_constraints.py`.

The files are the package's own fixtures (`tests/fixtures/dive_files/`), beside the inline
Suunto app JSON `tests/helpers/dive_files.py` writes for a second computer.
"""

import hashlib
import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import divejson
import pytest
from fastapi import UploadFile
from sqlalchemy import select, update
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
from src.app.services import blob_store
from src.app.services.dive_files import StoredRecordingFile, delete_dive_file, store_recording_file
from src.app.services.dive_profiles import READER_VERSION, backfill_profiles, get_profile_version
from src.app.services.dive_reader import read_prefill
from src.app.services.dive_recordings import DECO_MODEL_COLUMNS, DEVICE_COLUMNS, READOUT_COLUMNS
from src.app.services.logbook_import import plan_import, write_import
from tests.conftest import db_available
from tests.helpers.dive_files import suunto_json
from tests.helpers.generators import create_dive, create_user
from tests.helpers.import_parts import load_one

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

FIXTURES = Path(__file__).parent / "fixtures" / "dive_files"

# What a recording is, for the door: who recorded it, how it was set, what it read out, and
# the two figures the strict gate compares. The samples are compared whole, beside these.
DOOR_COLUMNS = (
    *DEVICE_COLUMNS.values(),
    *DECO_MODEL_COLUMNS.values(),
    *READOUT_COLUMNS,
    "mode",
    "salinity",
    "duration",
    "max_depth",
)

# A second computer on the D5 dive: its own clock five minutes on, its own serial, one gas
# at 49 % that its transmitter reported on - which the reader labels `0` in its own file.
SECOND_COMPUTER = suunto_json(
    start="2025-06-03T12:20:30.000+02:00",
    serial="999999999999",
    name="Suunto D5 backup",
    gases=[{"Oxygen": 0.49, "Helium": 0, "StartPressure": 20000000, "EndPressure": 15000000}],
    samples=((0, 1.0, 1, 20000000), (10, 5.0, 1, 19000000), (20, 6.0, 1, 15000000)),
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def volume(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backfill run ends by dropping the touched accounts' dive caches, and there is no
    Redis here to drop them in."""
    monkeypatch.setattr("src.app.services.cache_invalidation.invalidate_dive_caches", AsyncMock())


@pytest.fixture
def diver(db: Session) -> User:
    return create_user(db)


@pytest.fixture
def dive(db: Session, diver: User) -> Dive:
    return create_dive(db, diver)


async def _attach(db: AsyncSession, diver: User, dive: Dive, content: bytes) -> StoredRecordingFile:
    """`POST /dive/{uuid}/recordings`, with the token the parse would have minted."""
    fmt = divejson.sniff(content[: divejson.SNIFF_BYTES])
    assert fmt is not None
    return await store_recording_file(
        db,
        user_id=diver.id,
        user_uuid=diver.uuid,
        dive_id=dive.id,
        upload=UploadFile(filename=f"dive.{fmt}", file=io.BytesIO(content)),
        file_token=create_dive_file_token(
            user_uuid=diver.uuid, sha256=hashlib.sha256(content).hexdigest(), parser_key=fmt
        ),
    )


async def _save_the_form(db: AsyncSession, dive: Dive, content: bytes) -> None:
    """The dive's cylinders as the form saves them: the parse's own, unedited."""
    _, parsed = read_prefill(content)
    await replace_mixtures_for_dive(
        db=db, dive_id=dive.id, mixtures=[DiveMixtureCreate(**row.model_dump()) for row in parsed.mixtures]
    )


async def _cylinders(db: AsyncSession, dive: Dive) -> list[tuple[float | None, int | None]]:
    rows = await db.execute(
        select(DiveMixture.oxygen, DiveMixture.gas_number)
        .where(DiveMixture.dive_id == dive.id)
        .order_by(DiveMixture.id)
    )
    return [(row.oxygen, row.gas_number) for row in rows]


async def _recordings(db: AsyncSession, dive_id: int) -> list[DiveRecording]:
    rows = await db.execute(
        select(DiveRecording).where(DiveRecording.dive_id == dive_id).order_by(DiveRecording.ordinal)
    )
    return list(rows.scalars().all())


async def _profile(db: AsyncSession, recording_id: int) -> DiveProfile:
    stmt = (
        select(DiveProfile)
        .where(DiveProfile.recording_id == recording_id)
        .options(undefer(DiveProfile.data))
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one()


def _pressure_labels(profile: DiveProfile) -> list[int]:
    return [series["gas_number"] for series in profile.data.get("pressure", [])]


def _switch_labels(profile: DiveProfile) -> list[int]:
    return [event["gas_number"] for event in profile.data.get("events", []) if event["type"] == "gas_switch"]


async def _file_id(db: AsyncSession, dive: Dive, fmt: str) -> int:
    return (
        await db.execute(select(DiveFile.id).where(DiveFile.dive_id == dive.id, DiveFile.parser_key == fmt))
    ).scalar_one()


def _labels_are_unique(cylinders: list[tuple[float | None, int | None]]) -> bool:
    labels = [label for _, label in cylinders if label is not None]
    return len(labels) == len(set(labels))


class TestTheDoorDoesNotMatter:
    """The recording an attach creates from a file is the recording an import creates from the
    same file - device, settings, readouts, channels, labels and gate figures."""

    @staticmethod
    async def _import(db: AsyncSession, diver: User, content: bytes, name: str) -> None:
        with await load_one(content, name) as loaded:
            plan = await plan_import(db, user_id=diver.id, loaded=loaded, resolution_ran=True)
            await write_import(db, user_id=diver.id, loaded=loaded, plan=plan)
            await db.commit()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["suunto-ocean-2026.fit", "suunto-ocean-2026.json"])
    async def test_an_attached_file_and_an_imported_one_are_one_recording(
        self, volume: Any, async_db: AsyncSession, db: Session, name: str
    ) -> None:
        content = _fixture(name)
        attaching = create_user(db)
        attached_dive = create_dive(db, attaching)
        importing = create_user(db)

        await _attach(async_db, attaching, attached_dive, content)
        await self._import(async_db, importing, content, name)

        [attached] = await _recordings(async_db, attached_dive.id)
        imported_dive = (await async_db.execute(select(Dive.id).where(Dive.user_id == importing.id))).scalar_one()
        [imported] = await _recordings(async_db, imported_dive)
        assert {column: getattr(attached, column) for column in DOOR_COLUMNS} == {
            column: getattr(imported, column) for column in DOOR_COLUMNS
        }
        assert (await _profile(async_db, attached.id)).data == (await _profile(async_db, imported.id)).data


class TestAPairOfFilesAgreesOnItsLabels:
    """A Suunto's FIT and JSON of one dive are one recording, and its transmitter's channel
    names the dive's cylinder whichever file came first. The FIT labels nothing - it has no
    channel for a label to point at - and the JSON labels the cylinder its transmitter was on."""

    PAIRS = {
        "one cylinder": ("suunto-ocean-2026.fit", "suunto-ocean-2026.json"),
        # Its JSON records no mix, so its two cylinders join the FIT's two by position, and
        # the channel lands on the cylinder at the position the JSON lists it: the first.
        "two cylinders": ("suunto-ocean.fit", "suunto-ocean.json"),
    }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pair", PAIRS)
    @pytest.mark.parametrize("fit_first", [True, False], ids=["FIT first", "JSON first"])
    async def test_the_channel_names_the_cylinder_through_a_delete_and_a_forced_backfill(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive, pair: str, fit_first: bool
    ) -> None:
        fit, json_export = (_fixture(name) for name in self.PAIRS[pair])
        first, second = (fit, json_export) if fit_first else (json_export, fit)
        await _save_the_form(async_db, dive, first)

        await _attach(async_db, diver, dive, first)
        await _attach(async_db, diver, dive, second)

        [recording] = await _recordings(async_db, dive.id)
        assert _pressure_labels(await _profile(async_db, recording.id)) == [0]
        cylinders = await _cylinders(async_db, dive)
        assert cylinders[0][1] == 0
        assert _labels_are_unique(cylinders)

        await delete_dive_file(async_db, file_id=await _file_id(async_db, dive, "fit"))

        assert _pressure_labels(await _profile(async_db, recording.id)) == [0]
        assert (await _cylinders(async_db, dive))[0][1] == 0

        await backfill_profiles(async_db, force=True, parser_key="suunto_json")

        assert _pressure_labels(await _profile(async_db, recording.id)) == [0]
        assert await _cylinders(async_db, dive) == cylinders


class TestEveryPathThatStoresAnExtractionLabels:
    """The labelling is one function, and each of the re-derivation's three callers runs it -
    so a dive under a previous reader's labels comes out on this one's wherever it is touched
    before the backfill reaches it."""

    @staticmethod
    async def _under_old_labels(db: AsyncSession, dive: Dive, recording_id: int, labels: dict[float, int]) -> None:
        """What a previous reader left on the D5 dive: its two cylinders labelled its own way,
        the profile's channel and switches under the same numbers, and a row it wrote with the
        previous extractor. `labels` is oxygen -> label, and the reader's `0` and `1` are the
        21 % and the 49 % in that order."""
        for oxygen, label in labels.items():
            await db.execute(
                update(DiveMixture)
                .where(DiveMixture.dive_id == dive.id, DiveMixture.oxygen == oxygen)
                .values(gas_number=label)
            )
        previous = {0: labels[21.0], 1: labels[49.0]}
        profile = await _profile(db, recording_id)
        data = dict(profile.data)
        data["pressure"] = [
            series | {"gas_number": previous[series["gas_number"]]} for series in data.get("pressure", [])
        ]
        data["events"] = [
            event | {"gas_number": previous[event["gas_number"]]} if event.get("gas_number") is not None else event
            for event in data.get("events", [])
        ]
        await db.execute(
            update(DiveProfile)
            .where(DiveProfile.recording_id == recording_id)
            .values(data=data, extractor_version=5, reader_version=None)
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_a_form_the_previous_build_prefilled_is_renumbered_by_its_own_attach(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """Prefilled at `[1, 2]` before the deploy and saved after it: the attach is what puts
        the dive on the reader's labels."""
        content = _fixture("suunto-d5.json")
        await _save_the_form(async_db, dive, content)
        await async_db.execute(
            update(DiveMixture).where(DiveMixture.dive_id == dive.id, DiveMixture.oxygen == 21.0).values(gas_number=1)
        )
        await async_db.execute(
            update(DiveMixture).where(DiveMixture.dive_id == dive.id, DiveMixture.oxygen == 49.0).values(gas_number=2)
        )
        await async_db.commit()

        stored = await _attach(async_db, diver, dive, content)

        assert await _cylinders(async_db, dive) == [(21.0, 0), (49.0, 1)]
        profile = await _profile(async_db, stored.recording_id)
        assert _pressure_labels(profile) == [0]
        assert _switch_labels(profile) == [0, 1]

    @pytest.mark.asyncio
    async def test_a_repeat_upload_renumbers_a_dive_the_backfill_has_not_reached(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        content = _fixture("suunto-d5.json")
        await _save_the_form(async_db, dive, content)
        stored = await _attach(async_db, diver, dive, content)
        await self._under_old_labels(async_db, dive, stored.recording_id, {21.0: 1, 49.0: 2})

        await _attach(async_db, diver, dive, content)

        assert await _cylinders(async_db, dive) == [(21.0, 0), (49.0, 1)]
        profile = await _profile(async_db, stored.recording_id)
        assert (_pressure_labels(profile), profile.reader_version) == ([0], READER_VERSION)

    @pytest.mark.asyncio
    async def test_deleting_a_file_renumbers_from_what_is_left(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        json_export = _fixture("suunto-ocean-2026.json")
        await _save_the_form(async_db, dive, json_export)
        await _attach(async_db, diver, dive, json_export)
        stored = await _attach(async_db, diver, dive, _fixture("suunto-ocean-2026.fit"))
        await async_db.execute(update(DiveMixture).where(DiveMixture.dive_id == dive.id).values(gas_number=1))
        await async_db.commit()

        await delete_dive_file(async_db, file_id=await _file_id(async_db, dive, "fit"))

        assert [label for _, label in await _cylinders(async_db, dive)] == [0]
        assert _pressure_labels(await _profile(async_db, stored.recording_id)) == [0]


class TestTheBackfillRelabels:
    @pytest.mark.asyncio
    async def test_it_renumbers_a_dive_stored_under_the_old_labels(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """The D5-shape JSON dive at `[1, 2]` with its channel on `1` comes out at `[0, 1]`
        with the channel on `0`. A cylinder the file does not have whose old label the new
        numbering claims is cleared, and a hand-added unlabelled one is left alone."""
        content = _fixture("suunto-d5.json")
        await _save_the_form(async_db, dive, content)
        stored = await _attach(async_db, diver, dive, content)
        db.add_all(
            [
                DiveMixture(dive_id=dive.id, oxygen=32.0, helium=0.0, gas_number=0),
                DiveMixture(dive_id=dive.id, oxygen=36.0, helium=0.0),
            ]
        )
        db.commit()
        await TestEveryPathThatStoresAnExtractionLabels._under_old_labels(
            async_db, dive, stored.recording_id, {21.0: 1, 49.0: 2}
        )
        assert _pressure_labels(await _profile(async_db, stored.recording_id)) == [1]

        await backfill_profiles(async_db)

        cylinders = await _cylinders(async_db, dive)
        assert cylinders == [(21.0, 0), (49.0, 1), (32.0, None), (36.0, None)]
        assert _labels_are_unique(cylinders)
        profile = await _profile(async_db, stored.recording_id)
        assert _pressure_labels(profile) == [0]
        assert set(_switch_labels(profile)) <= {label for _, label in cylinders}

    @pytest.mark.asyncio
    async def test_a_second_computer_keeps_its_mapped_labels_through_a_forced_backfill(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """The second computer labels its 49 % tank `0` in its own file; the dive calls that
        tank `1`. Its recording is mapped onto the dive's list at attach, and a forced
        re-derivation maps it the same way rather than storing the file's own numbering."""
        primary = _fixture("suunto-d5.json")
        await _save_the_form(async_db, dive, primary)
        await _attach(async_db, diver, dive, primary)
        second = await _attach(async_db, diver, dive, SECOND_COMPUTER)
        assert [row.ordinal for row in await _recordings(async_db, dive.id)] == [0, 1]
        assert _pressure_labels(await _profile(async_db, second.recording_id)) == [1]

        await backfill_profiles(async_db, force=True, parser_key="suunto_json")

        assert _pressure_labels(await _profile(async_db, second.recording_id)) == [1]
        assert await _cylinders(async_db, dive) == [(21.0, 0), (49.0, 1)]

    @pytest.mark.asyncio
    async def test_a_sibling_relabelled_without_new_bytes_gets_a_new_etag_and_nothing_else_does(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """The ETag is the row's identity, which every write of samples renews. A sibling whose
        channel the primary's renumbering rewrites reads no bytes, so a key made of what the
        samples are a function of could not move - and a client would draw its old labels
        against the renumbered cylinders."""
        primary = _fixture("suunto-d5.json")
        await _save_the_form(async_db, dive, primary)
        stored = await _attach(async_db, diver, dive, primary)
        second = await _attach(async_db, diver, dive, SECOND_COMPUTER)
        await TestEveryPathThatStoresAnExtractionLabels._under_old_labels(
            async_db, dive, stored.recording_id, {21.0: 1, 49.0: 2}
        )
        sibling = await _profile(async_db, second.recording_id)
        await async_db.execute(
            update(DiveProfile)
            .where(DiveProfile.recording_id == second.recording_id)
            .values(data=dict(sibling.data) | {"pressure": [{**sibling.data["pressure"][0], "gas_number": 2}]})
        )
        await async_db.commit()
        before = await get_profile_version(async_db, recording_id=second.recording_id)

        await backfill_profiles(async_db)

        relabelled = await get_profile_version(async_db, recording_id=second.recording_id)
        assert relabelled != before
        assert _pressure_labels(await _profile(async_db, second.recording_id)) == [1]
        primary_version = await get_profile_version(async_db, recording_id=stored.recording_id)

        await backfill_profiles(async_db)

        assert await get_profile_version(async_db, recording_id=second.recording_id) == relabelled
        assert await get_profile_version(async_db, recording_id=stored.recording_id) == primary_version
