"""The recording lifecycle against a live Postgres: where a file lands, and what goes with it.

`test_dive_recordings.py` pins the three gates, which are pure. This pins what happens
around them, and every one of these facts needs a real database to be true at all: the
ordinal's unique index, the two `ON DELETE CASCADE`s from `dive_recording`, and the
`COALESCE` writes that make the fill rule one statement rather than a read and a write.

Same skip-if-unreachable guard and same write-real-rows-and-leave-them convention as
`test_dive_check_constraints.py`; see the note there.
"""

import hashlib
import io
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import UploadFile
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.v1 import dives as dives_module
from src.app.api.v1.dives import erase_dive_recording, patch_dive_recording
from src.app.core.security import create_dive_file_token
from src.app.core.utils.datetime_offset import combine_start_time
from src.app.models.dive import Dive
from src.app.models.dive_file import DiveFile
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_recording import DiveRecording
from src.app.models.user import User
from src.app.schemas.dive import DecoAlgorithm, DiveListSort, DiveMode, DiveRead, RecordingUpdateRequest
from src.app.schemas.dive_profile import ProfileProvenance
from src.app.schemas.parsed_dive import ParsedDecoModel
from src.app.services import blob_store
from src.app.services.dive_files import (
    delete_dive_file,
    fill_tech_scalars,
    read_unread_file_fixes,
    store_recording_file,
)
from src.app.services.dive_profiles import IMPORT_PARSER_KEY, MERGE_PARSER_KEY, NormalizedProfile, ProfileSeries
from src.app.services.dive_recordings import (
    delete_recording,
    fill_recording_settings,
    fill_start,
    get_recordings_for_dives,
    make_primary,
    next_ordinal,
    renumber_ordinals,
)
from tests.conftest import db_available
from tests.helpers.dive_files import suunto_json
from tests.helpers.generators import create_dive, create_dive_recording, create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _export(
    *,
    cns_end: float | None = None,
    samples: tuple[tuple[float, float] | tuple[float, float, int, int], ...] = (),
    start: str = "2026-09-08T15:17:38.670+03:00",
    cylinder: dict[str, Any] | None = None,
) -> bytes:
    """A minimal Suunto app JSON export - see `tests/helpers/dive_files.py` for why that one."""
    return suunto_json(start=start, cns_end=cns_end, gases=() if cylinder is None else (cylinder,), samples=samples)


def _samples(*depths: tuple[int, str]) -> tuple[tuple[float, float], ...]:
    return tuple((t, float(d)) for t, d in depths)


async def _readings(db: AsyncSession, dive: Dive) -> tuple[float | None, float | None]:
    """The primary recording's `(cns_end, otu_end)` - the readings the dive page shows.

    The pair rather than the one reading, because the two answer different halves of the
    same question: `cns_end` is a figure a file can also yield and `otu_end` one nothing here
    parses, so a rewrite that replaced the first and cleared the second is visible as two
    different wrong numbers rather than as one. Read back through a query rather than
    `refresh`: the `dive` fixture belongs to the *sync* session that seeded it.
    """
    row = (
        await db.execute(
            select(DiveRecording.cns_end, DiveRecording.otu_end).where(
                DiveRecording.dive_id == dive.id, DiveRecording.ordinal == 0
            )
        )
    ).one_or_none()
    return (None, None) if row is None else (row.cns_end, row.otu_end)


async def _cns_end(db: AsyncSession, dive: Dive) -> float | None:
    return (await _readings(db, dive))[0]


@pytest.fixture
def volume(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def diver(db: Session) -> User:
    return create_user(db)


@pytest.fixture
def dive(db: Session, diver: User) -> Dive:
    return create_dive(db, diver)


@pytest.fixture
def routed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the real recording handlers with Redis out of the way. Both end by dropping the
    caches, and there is no Redis here to drop them in."""
    monkeypatch.setattr(dives_module, "invalidate_dive_caches", AsyncMock())


async def _attach(db: AsyncSession, diver: User, dive: Dive, content: bytes, *, filename: str = "export.json") -> Any:
    return await store_recording_file(
        db,
        user_id=diver.id,
        user_uuid=diver.uuid,
        dive_id=dive.id,
        upload=UploadFile(filename=filename, file=io.BytesIO(content)),
        file_token=create_dive_file_token(
            user_uuid=diver.uuid, sha256=hashlib.sha256(content).hexdigest(), parser_key="suunto_json"
        ),
    )


async def _recordings(db: AsyncSession, dive: Dive) -> list[DiveRecording]:
    rows = await db.execute(
        select(DiveRecording).where(DiveRecording.dive_id == dive.id).order_by(DiveRecording.ordinal)
    )
    return list(rows.scalars().all())


async def _erase_recording(db: AsyncSession, diver: User, dive: Dive, rid: uuid_pkg.UUID) -> None:
    await erase_dive_recording(
        request=Mock(),
        uuid=dive.uuid,
        rid=rid,
        current_user={"id": diver.id, "uuid": diver.uuid, "is_superuser": False},
        db=db,
    )


async def _insert_profile(db: AsyncSession, dive: Dive, recording: DiveRecording, *, parser_key: str) -> None:
    """Samples against a recording that never had a file, written straight in - which is what
    logbook import does and what a merge leaves behind, neither of which this module owns."""
    await db.execute(
        insert(DiveProfile).values(
            recording_id=recording.id,
            dive_id=dive.id,
            source_sha256="b" * 64,
            parser_key=parser_key,
            extractor_version=3,
            duration=2940,
            depth_sample_count=2,
            data=NormalizedProfile(depth=ProfileSeries(t=[0, 10], v=[0, 1900])).to_data(),
            uuid=recording.uuid,
            created_at=datetime.now(UTC),
        )
    )


async def _import_recording(
    db: AsyncSession,
    sync_db: Session,
    diver: User,
    dive: Dive,
    *,
    cns_end: float,
    otu_end: float,
    start: datetime | None = None,
) -> Any:
    """A logbook import of a document, as `services/logbook_import/writer.py` leaves it: the
    document's figures on a primary recording that holds samples and no bytes. Nothing on
    this instance can re-derive either number."""
    recording = create_dive_recording(sync_db, diver, dive, ordinal=0)
    values: dict[str, Any] = {"cns_end": cns_end, "otu_end": otu_end}
    if start is not None:
        values |= {"start_time": start, "utc_offset_minutes": 180}
    await db.execute(update(DiveRecording).where(DiveRecording.id == recording.id).values(**values))
    await _insert_profile(db, dive, recording, parser_key=IMPORT_PARSER_KEY)
    await db.commit()
    return recording


class TestWhereAFileLands:
    @pytest.mark.asyncio
    async def test_the_first_file_makes_a_primary_recording(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        stored = await _attach(async_db, diver, dive, _export(cns_end=9.0, samples=_samples((0, "0"), (10, "5"))))

        recordings = await _recordings(async_db, dive)
        assert [row.ordinal for row in recordings] == [0]
        assert recordings[0].id == stored.recording_id
        assert recordings[0].device_serial == "253810000400"
        assert recordings[0].device_brand == "Suunto"
        # The samples' span and deepest reading, the import's rule - not the header's logged
        # 1800 s and 19.04 m, which a recording imported from the same file would not carry.
        assert (recordings[0].duration, recordings[0].max_depth) == (10, 5.0)

    @pytest.mark.asyncio
    async def test_a_second_export_of_one_computer_fills_the_same_recording(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """The whole point of a recording. Two spellings of one record, and the second
        contributes what the first was silent about without overwriting anything."""
        first = await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="dive.json")
        # Half a second later by its own clock, and it records an exposure reading the first
        # did not. Same serial, so the same-recording gate takes it.
        second = await _attach(
            async_db,
            diver,
            dive,
            _export(start="2026-09-08T15:17:39.170+03:00", samples=_samples((0, "0"), (10, "5"))),
            filename="dive.fit.json",
        )

        assert second.recording_id == first.recording_id
        assert [row.ordinal for row in await _recordings(async_db, dive)] == [0]
        files = (
            (await async_db.execute(select(DiveFile).where(DiveFile.recording_id == first.recording_id)))
            .scalars()
            .all()
        )
        assert len(files) == 2
        # The first file's reading survives, and the samples only the second carries arrive.
        assert await _cns_end(async_db, dive) == 9.0
        profile = (
            await async_db.execute(select(DiveProfile).where(DiveProfile.recording_id == first.recording_id))
        ).scalar_one()
        assert profile.depth_sample_count == 2

    @pytest.mark.asyncio
    async def test_a_second_computer_gets_a_recording_of_its_own(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """Two minutes apart and a different serial: a second computer, appended after the
        first, and carrying its own readouts - a second machine's CNS clock is its own
        device's arithmetic, and the primary's stays what it was."""
        await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="first.json")
        other = _export(start="2026-09-08T15:19:38.670+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")

        stored = await _attach(async_db, diver, dive, other, filename="second.json")

        recordings = await _recordings(async_db, dive)
        assert [row.ordinal for row in recordings] == [0, 1]
        assert recordings[1].id == stored.recording_id
        assert await _cns_end(async_db, dive) == 9.0
        assert recordings[1].cns_end == 44.0

    @pytest.mark.asyncio
    async def test_the_read_shape_carries_each_recordings_readouts_and_none_on_the_dive(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="first.json")
        other = _export(start="2026-09-08T15:19:38.670+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")
        await _attach(async_db, diver, dive, other, filename="second.json")

        recordings = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]

        assert [(row.cns_end, row.salinity) for row in recordings] == [(9.0, None), (44.0, None)]
        assert "cns_end" not in DiveRead.model_fields

    @pytest.mark.asyncio
    async def test_re_uploading_repairs_a_file_that_vanished_from_the_volume(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """ "Just upload it again" is the natural repair after a partial volume loss, and the
        order of the two halves is what makes it work: the bytes go back **before** anything
        reads the recording's files. Re-deriving a recording loads every file it holds and
        raises on bytes that are gone, so repairing second would fail on exactly the
        condition this path exists to fix.
        """
        content = _export(cns_end=9.0, samples=_samples((0, "0")))
        stored = await _attach(async_db, diver, dive, content)
        key = (
            await async_db.execute(select(DiveFile.storage_key).where(DiveFile.recording_id == stored.recording_id))
        ).scalar_one()
        (volume / key).unlink()

        again = await _attach(async_db, diver, dive, content)

        assert again.recording_id == stored.recording_id
        assert await blob_store.get(key) == content

    @pytest.mark.asyncio
    async def test_the_same_bytes_twice_are_a_no_op(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        content = _export(cns_end=9.0)

        first = await _attach(async_db, diver, dive, content)
        again = await _attach(async_db, diver, dive, content)

        assert again.recording_id == first.recording_id
        assert again.file_uuid == first.file_uuid
        assert len(await _recordings(async_db, dive)) == 1


class TestFillingTheDivesCylinders:
    """The cylinder half of the fill rule, against the rows a diver actually owns.

    Here rather than beside the pure `fill_from_pair` tests because the interesting part is
    the pairing: the parsed cylinders come off the recording's files, the stored ones off the
    dive, and only a real `UPDATE` shows that the blanks moved and the rest did not.
    """

    @staticmethod
    def _seed_cylinder(db: Session, dive: Dive, **columns: Any) -> None:
        db.add(DiveMixture(dive_id=dive.id, **columns))
        db.commit()

    @staticmethod
    async def _cylinder(db: AsyncSession, dive: Dive) -> DiveMixture:
        return (await db.execute(select(DiveMixture).where(DiveMixture.dive_id == dive.id))).scalar_one()

    @pytest.mark.asyncio
    async def test_a_second_file_fills_the_blank_mix_and_leaves_the_pressures(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """The corpus pair, as a dive: the Suunto's JSON records this cylinder's pressures
        and no gas fraction anywhere, and the same computer's FIT records `oxygen` 33 and no
        pressures. The stored `start_pressure` is deliberately *not* the second file's, so
        the assertion is about the rule rather than about two numbers that agree.
        """
        self._seed_cylinder(db, dive, gas_number=0, start_pressure=200.0, end_pressure=47.47)

        await _attach(
            async_db,
            diver,
            dive,
            _export(cns_end=9.0, cylinder={"StartPressure": 20734000, "EndPressure": 4747000}),
            filename="ocean.json",
        )
        await _attach(
            async_db,
            diver,
            dive,
            _export(
                start="2026-09-08T15:17:39.170+03:00",
                samples=_samples((0, "0"), (10, "5")),
                cylinder={"Oxygen": 0.33},
            ),
            filename="ocean-fit.json",
        )

        assert [row.ordinal for row in await _recordings(async_db, dive)] == [0]
        cylinder = await self._cylinder(async_db, dive)
        assert cylinder.oxygen == 33.0
        assert (cylinder.start_pressure, cylinder.end_pressure) == (200.0, 47.47)
        # The label the dive's stored pressure channels are attributed under. Neither file
        # labels a cylinder - neither carries a channel or a switch to point one at - so the
        # labelling has nothing to move it to.
        assert cylinder.gas_number == 0

    @pytest.mark.asyncio
    async def test_the_upload_that_creates_the_primary_fills_nothing(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """This dive has no recording yet, so the upload makes its primary, and the dive's
        cylinders came off the form this very parse pre-filled. A fill would only put back a
        blank the diver had just cleared. **Not "a recording's first file"** - two tests
        below are that and one of them fills, and a recording past the first that an upload
        creates fills too."""
        self._seed_cylinder(db, dive, gas_number=0, start_pressure=200.0)

        await _attach(async_db, diver, dive, _export(cylinder={"Oxygen": 0.33}), filename="ocean.json")

        assert (await self._cylinder(async_db, dive)).oxygen is None

    @pytest.mark.asyncio
    async def test_re_uploading_the_creating_file_does_not_undo_an_edit(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """ "Just upload it again" must not put back a value the diver deliberately cleared.

        The same bytes reach `_repeat_upload`, which re-derives opportunistically - and this
        export carries no samples, so no profile row exists and `should_extract` answers
        "extract" on *every* such upload rather than only after a version bump. Nothing new
        arrived, so nothing may fill: the file's `oxygen` 33 is exactly what the diver removed
        from the form, and reading it straight back off those bytes is the edit undone.
        """
        self._seed_cylinder(db, dive, gas_number=0, start_pressure=200.0)
        export = _export(cylinder={"Oxygen": 0.33})
        await _attach(async_db, diver, dive, export, filename="ocean.json")

        await _attach(async_db, diver, dive, export, filename="ocean.json")

        assert (await self._cylinder(async_db, dive)).oxygen is None

    @pytest.mark.asyncio
    async def test_the_first_file_of_an_imported_recording_does_fill(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """**`CREATED` is "this upload created the recording", not "it had no files"**, and
        this is where the two answers differ.

        A logbook import of a document creates a recording with samples, no device and no
        bytes. When the diver later attaches the export it was converted from, that is the
        recording's *first* file and still a second reading of a record the dive already
        describes - it `JOINED` the recording, so it fills. Keyed on the file count it would
        count as the file that created the primary, and the cylinder would keep its blank.
        """
        recording = create_dive_recording(db, diver, dive)
        await async_db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(start_time=datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), utc_offset_minutes=180)
        )
        await async_db.commit()
        self._seed_cylinder(db, dive, gas_number=0, start_pressure=200.0)

        await _attach(async_db, diver, dive, _export(cylinder={"Oxygen": 0.33}), filename="ocean.json")

        # One recording still, so the file landed on the imported one rather than beside it.
        assert [row.id for row in await _recordings(async_db, dive)] == [recording.id]
        assert (await self._cylinder(async_db, dive)).oxygen == 33.0


class TestASecondComputersPressureOnUnlabelledCylinders:
    """A dive logged from a FIT alone has cylinders the reader labelled nothing, since no
    channel points at them. A second computer that does carry tank pressure attaches beside
    it, and its channel has to name one of the dive's cylinders."""

    @pytest.mark.asyncio
    async def test_the_matched_row_takes_the_label_the_channel_names(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        db.add(DiveMixture(dive_id=dive.id, gas_number=None, oxygen=21.0, helium=0.0))
        db.commit()
        await _attach(async_db, diver, dive, _export(samples=_samples((0, "5"), (10, "5"))), filename="ocean.json")
        second = await _attach(
            async_db,
            diver,
            dive,
            suunto_json(
                start="2026-09-08T15:17:40.000+03:00",
                serial="999999999999",
                samples=((0, 5.0, 0, 20_000_000), (10, 5.0, 0, 19_900_000)),
            ),
            filename="second.json",
        )

        labels = (
            (await async_db.execute(select(DiveMixture.gas_number).where(DiveMixture.dive_id == dive.id)))
            .scalars()
            .all()
        )
        data = (
            await async_db.execute(select(DiveProfile.data).where(DiveProfile.recording_id == second.recording_id))
        ).scalar_one()
        assert [row.ordinal for row in await _recordings(async_db, dive)] == [0, 1]
        assert labels != [None]
        assert [series["gas_number"] for series in data["pressure"]] == labels


class TestFillingAStart:
    """The two start columns are one value, and filling half of them corrupts the other.

    A NULL `utc_offset_minutes` means `start_time` holds a *wall clock labelled UTC* rather
    than an instant. Writing an offset beside it without moving the clock silently
    reinterprets a column nobody rewrote - and it is reachable by the ordinary route: a
    recording logbook import created from a Shearwater UDDF carries no offset, and the same
    computer's next export carries one.
    """

    WALL_CLOCK = datetime(2026, 9, 8, 15, 18, 10, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_an_offset_arriving_later_moves_the_clock_onto_a_real_instant(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        recording = create_dive_recording(db, diver, dive)
        await async_db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(start_time=self.WALL_CLOCK, utc_offset_minutes=None)
        )
        await async_db.commit()

        await fill_start(
            async_db,
            recording_id=recording.id,
            start_time=self.WALL_CLOCK - timedelta(hours=3),
            utc_offset_minutes=180,
        )
        await async_db.commit()

        row = (
            await async_db.execute(
                select(DiveRecording.start_time, DiveRecording.utc_offset_minutes).where(
                    DiveRecording.id == recording.id
                )
            )
        ).one()
        assert row.utc_offset_minutes == 180
        # The instant moved back three hours, so the clock face the diver read is unchanged.
        assert row.start_time == self.WALL_CLOCK - timedelta(hours=3)
        assert combine_start_time(row.start_time, row.utc_offset_minutes).replace(tzinfo=None) == (
            self.WALL_CLOCK.replace(tzinfo=None)
        )

    @pytest.mark.asyncio
    async def test_a_start_already_known_is_never_overwritten(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """Fill-only, like every other fill: a second file two seconds off must not move the
        record's start onto its own clock."""
        recording = create_dive_recording(db, diver, dive)
        await async_db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(start_time=self.WALL_CLOCK, utc_offset_minutes=180)
        )
        await async_db.commit()

        await fill_start(
            async_db, recording_id=recording.id, start_time=self.WALL_CLOCK + timedelta(seconds=2), utc_offset_minutes=0
        )
        await async_db.commit()

        row = (
            await async_db.execute(
                select(DiveRecording.start_time, DiveRecording.utc_offset_minutes).where(
                    DiveRecording.id == recording.id
                )
            )
        ).one()
        assert (row.start_time, row.utc_offset_minutes) == (self.WALL_CLOCK, 180)

    @pytest.mark.asyncio
    async def test_a_recording_with_no_start_takes_the_incoming_one_whole(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        recording = create_dive_recording(db, diver, dive)
        await async_db.execute(update(DiveRecording).where(DiveRecording.id == recording.id).values(start_time=None))
        await async_db.commit()

        await fill_start(async_db, recording_id=recording.id, start_time=self.WALL_CLOCK, utc_offset_minutes=180)
        await async_db.commit()

        row = (
            await async_db.execute(
                select(DiveRecording.start_time, DiveRecording.utc_offset_minutes).where(
                    DiveRecording.id == recording.id
                )
            )
        ).one()
        assert (row.start_time, row.utc_offset_minutes) == (self.WALL_CLOCK, 180)


class TestADateOnlyDive:
    """A dive whose source recorded its day and no time of day, and whose recording stated no
    start of its own - the row logbook import leaves (spec §6.4a). A file attached later of
    the same computer is a second reading of that recording, not a second computer."""

    DAY = datetime(2026, 9, 8, tzinfo=UTC)

    async def _date_only(self, db: AsyncSession, dive: Dive, recording: DiveRecording, **device: str) -> None:
        await db.execute(
            update(Dive)
            .where(Dive.id == dive.id)
            .values(start_time=self.DAY, utc_offset_minutes=None, start_date_only=True)
        )
        await db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(start_time=None, utc_offset_minutes=None, cns_end=12.0, **device)
        )
        await db.commit()

    async def _dive_start(self, db: AsyncSession, dive: Dive) -> tuple[Any, ...]:
        row = (
            await db.execute(
                select(Dive.start_time, Dive.utc_offset_minutes, Dive.start_date_only).where(Dive.id == dive.id)
            )
        ).one()
        return tuple(row)

    @pytest.mark.asyncio
    async def test_the_file_fills_the_recording_with_no_start_and_gives_it_the_files(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        recording = create_dive_recording(db, diver, dive)
        await self._date_only(async_db, dive, recording)

        stored = await _attach(async_db, diver, dive, _export(samples=_samples((0, "0"), (10, "5"))))

        assert stored.recording_id == recording.id
        [row] = await _recordings(async_db, dive)
        assert (row.start_time, row.utc_offset_minutes) == (datetime(2026, 9, 8, 12, 17, 38, 670000, tzinfo=UTC), 180)
        assert (row.device_serial, row.cns_end) == ("253810000400", 12.0)
        # The dive's own start is the diver's record and stays a day.
        assert await self._dive_start(async_db, dive) == (self.DAY, None, True)

    @pytest.mark.asyncio
    async def test_another_computers_file_is_a_recording_of_its_own(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        recording = create_dive_recording(db, diver, dive)
        await self._date_only(async_db, dive, recording, device_brand="Shearwater", device_serial="D9772626")

        stored = await _attach(async_db, diver, dive, _export())

        recordings = await _recordings(async_db, dive)
        assert [(row.ordinal, row.start_time is None) for row in recordings] == [(0, True), (1, False)]
        assert recordings[1].id == stored.recording_id
        assert await self._dive_start(async_db, dive) == (self.DAY, None, True)


class TestFillingTheModeAndTheDecoModel:
    """The same never-overwrite rule the device columns follow, one object across.

    A second file of one recording contributes what the first did not carry - a Suunto's
    JSON names the model its FIT has no room for - and takes nothing from it. The rejected
    alternative is "the later file wins", which loses a value the first file recorded.
    """

    @pytest.mark.asyncio
    async def test_a_later_file_fills_what_the_first_did_not_carry(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        recording = create_dive_recording(db, diver, dive)
        await async_db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(mode=None, deco_gf_low=50, deco_gf_high=85)
        )
        await async_db.commit()

        await fill_recording_settings(
            async_db,
            recording_id=recording.id,
            mode=DiveMode.OPEN_CIRCUIT,
            deco_model=ParsedDecoModel(name="Suunto Fused2 RGBM", conservatism=-1),
        )
        await async_db.commit()

        row = await self._settings(async_db, recording.id)
        assert row.mode == "open_circuit"
        assert row.deco_name == "Suunto Fused2 RGBM"
        assert row.deco_conservatism == -1
        assert (row.deco_gf_low, row.deco_gf_high) == (50, 85)

    @pytest.mark.asyncio
    async def test_a_value_the_first_file_recorded_is_never_overwritten(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        recording = create_dive_recording(db, diver, dive)
        await async_db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(mode="freedive", deco_algorithm="rgbm", deco_gf_low=50, deco_gf_high=85)
        )
        await async_db.commit()

        await fill_recording_settings(
            async_db,
            recording_id=recording.id,
            mode=DiveMode.OPEN_CIRCUIT,
            deco_model=ParsedDecoModel(algorithm=DecoAlgorithm.BUHLMANN, gf_low=30, gf_high=70),
        )
        await async_db.commit()

        row = await self._settings(async_db, recording.id)
        assert (row.mode, row.deco_algorithm) == ("freedive", "rgbm")
        assert (row.deco_gf_low, row.deco_gf_high) == (50, 85)

    @pytest.mark.asyncio
    async def test_a_file_that_recorded_neither_writes_nothing(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        recording = create_dive_recording(db, diver, dive)

        await fill_recording_settings(async_db, recording_id=recording.id, mode=None, deco_model=None)
        await async_db.commit()

        row = await self._settings(async_db, recording.id)
        assert (row.mode, row.deco_algorithm, row.deco_name) == (None, None, None)

    @pytest.mark.asyncio
    async def test_the_dive_read_reports_both_and_null_where_nothing_recorded_one(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """`null` for the whole model rather than five nulls, on `_read_device`'s terms: a
        recording whose files said nothing about it must not come back claiming an empty
        one."""
        recording = create_dive_recording(db, diver, dive)
        bare = create_dive_recording(db, diver, dive, ordinal=1)
        await async_db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(mode="open_circuit", deco_algorithm="buhlmann", deco_gf_low=50, deco_gf_high=85)
        )
        await async_db.commit()

        read = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]

        first = next(one for one in read if one.uuid == recording.uuid)
        assert first.mode == "open_circuit"
        assert first.deco_model is not None
        assert (first.deco_model.algorithm, first.deco_model.gf_low, first.deco_model.gf_high) == ("buhlmann", 50, 85)
        second = next(one for one in read if one.uuid == bare.uuid)
        assert (second.mode, second.deco_model) == (None, None)

    @staticmethod
    async def _settings(async_db: AsyncSession, recording_id: int) -> Any:
        return (
            await async_db.execute(
                select(
                    DiveRecording.mode,
                    DiveRecording.deco_algorithm,
                    DiveRecording.deco_name,
                    DiveRecording.deco_gf_low,
                    DiveRecording.deco_gf_high,
                    DiveRecording.deco_conservatism,
                ).where(DiveRecording.id == recording_id)
            )
        ).one()


class TestDeletingAFile:
    @pytest.mark.asyncio
    async def test_the_last_file_takes_its_recording_and_the_dives_readings(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """A recording whose profile was only ever read off that file can never be re-derived
        or checked against anything, so it goes with it - and so do the readings that came
        off it."""
        stored = await _attach(async_db, diver, dive, _export(cns_end=9.0, samples=_samples((0, "0"))))
        file_id = (
            await async_db.execute(select(DiveFile.id).where(DiveFile.recording_id == stored.recording_id))
        ).scalar_one()

        await delete_dive_file(async_db, file_id=file_id)

        assert await _recordings(async_db, dive) == []
        assert await _cns_end(async_db, dive) is None

    @pytest.mark.asyncio
    async def test_a_recording_whose_profile_no_file_can_re_yield_survives_it(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """Reachable through `POST /dives/merge`, which lets a merged recording keep whatever
        files either part had. Deleting the last of them must not take samples nothing on
        this instance can produce again - the same principle both backfills follow.
        """
        stored = await _attach(async_db, diver, dive, _export(cns_end=9.0, samples=_samples((0, "0"))))
        # Stand in for the merge: the samples stay, their provenance says no file made them.
        await async_db.execute(
            update(DiveProfile)
            .where(DiveProfile.recording_id == stored.recording_id)
            .values(parser_key=MERGE_PARSER_KEY)
        )
        await async_db.commit()
        file_id = (
            await async_db.execute(select(DiveFile.id).where(DiveFile.recording_id == stored.recording_id))
        ).scalar_one()

        await delete_dive_file(async_db, file_id=file_id)

        recordings = await _recordings(async_db, dive)
        assert [row.id for row in recordings] == [stored.recording_id]
        assert (
            await async_db.execute(select(DiveProfile.id).where(DiveProfile.recording_id == stored.recording_id))
        ).scalar_one_or_none() is not None


class TestAProfileNoFileCanReYield:
    """A re-derivation over a recording's files leaves a merged profile alone, and a document's
    unless the files bring samples of their own."""

    MERGED = NormalizedProfile(depth=ProfileSeries(t=[0, 10_000, 20_000, 30_000], v=[0, 1200, 2400, 0]))
    SAMPLES = _samples((0, "0"), (10, "5"))

    async def _merged(self, db: AsyncSession, recording_id: int) -> uuid_pkg.UUID:
        """Stand in for `POST /dives/merge`: samples no file holds, and the gate figures
        they were stored with. Returns the profile's ETag identity."""
        await db.execute(
            update(DiveProfile)
            .where(DiveProfile.recording_id == recording_id)
            .values(parser_key=MERGE_PARSER_KEY, data=self.MERGED.to_data(), duration=30_000)
        )
        await db.execute(
            update(DiveRecording).where(DiveRecording.id == recording_id).values(duration=30, max_depth=24.0)
        )
        await db.commit()
        return await self._identity(db, recording_id)

    @staticmethod
    async def _identity(db: AsyncSession, recording_id: int) -> uuid_pkg.UUID:
        return (await db.execute(select(DiveProfile.uuid).where(DiveProfile.recording_id == recording_id))).scalar_one()

    async def _assert_merged(self, db: AsyncSession, recording_id: int, identity: uuid_pkg.UUID) -> None:
        profile = (
            await db.execute(
                select(DiveProfile.parser_key, DiveProfile.data, DiveProfile.uuid).where(
                    DiveProfile.recording_id == recording_id
                )
            )
        ).one()
        figures = (
            await db.execute(
                select(DiveRecording.duration, DiveRecording.max_depth).where(DiveRecording.id == recording_id)
            )
        ).one()
        assert (profile.parser_key, profile.data, profile.uuid) == (MERGE_PARSER_KEY, self.MERGED.to_data(), identity)
        assert tuple(figures) == (30, 24.0)

    @pytest.mark.asyncio
    async def test_deleting_one_of_a_merged_recordings_two_files_keeps_its_samples(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """The file left is one half of what the merge joined: re-reading it would replace the
        whole dive's samples with that half's, and the deleted bytes go after the commit."""
        stored = await _attach(async_db, diver, dive, _export(cns_end=9.0, samples=self.SAMPLES), filename="a.json")
        second = await _attach(async_db, diver, dive, _export(cns_end=11.0, samples=self.SAMPLES), filename="b.json")
        assert second.recording_id == stored.recording_id
        identity = await self._merged(async_db, stored.recording_id)
        file_id = (await async_db.execute(select(DiveFile.id).where(DiveFile.uuid == second.file_uuid))).scalar_one()

        await delete_dive_file(async_db, file_id=file_id)

        await self._assert_merged(async_db, stored.recording_id, identity)
        assert await _cns_end(async_db, dive) == 9.0

    @pytest.mark.asyncio
    async def test_a_header_only_file_joining_a_merged_recording_keeps_its_samples_and_fills(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """No sampled span to compare, so the header-only export passes the same-recording gate
        and joins - and still supplies the readout the recording had none of."""
        stored = await _attach(async_db, diver, dive, _export(samples=self.SAMPLES), filename="a.json")
        identity = await self._merged(async_db, stored.recording_id)

        joined = await _attach(async_db, diver, dive, _export(cns_end=7.0), filename="header.json")

        assert joined.recording_id == stored.recording_id
        await self._assert_merged(async_db, stored.recording_id, identity)
        assert await _cns_end(async_db, dive) == 7.0

    @pytest.mark.asyncio
    async def test_a_header_only_file_joining_a_merged_recording_still_fills_the_dives_cylinder(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """An arrival, whatever the recording keeps: the dive's blank mix fills."""
        stored = await _attach(async_db, diver, dive, _export(samples=self.SAMPLES), filename="a.json")
        identity = await self._merged(async_db, stored.recording_id)
        db.add(DiveMixture(dive_id=dive.id, gas_number=3, start_pressure=200.0, end_pressure=50.0))
        db.commit()

        await _attach(async_db, diver, dive, _export(cylinder={"Oxygen": 0.32}), filename="header.json")

        await self._assert_merged(async_db, stored.recording_id, identity)
        cylinder = (await async_db.execute(select(DiveMixture).where(DiveMixture.dive_id == dive.id))).scalar_one()
        assert (cylinder.oxygen, cylinder.gas_number) == (32.0, 3)

    @pytest.mark.asyncio
    async def test_a_labelled_cylinder_joining_a_merged_recording_does_not_renumber_the_dives(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """The merged samples' channels name the dive's labels as they stand, so a joining
        file's transmitter, which the reader labels 0, must not move the dive's 3 onto it."""
        stored = await _attach(async_db, diver, dive, _export(samples=self.SAMPLES), filename="a.json")
        identity = await self._merged(async_db, stored.recording_id)
        db.add(DiveMixture(dive_id=dive.id, gas_number=3))
        db.commit()
        # The merged span, so the same-recording gate admits it.
        transmitter = ((0, 0.0, 0, 20_000_000), (30, 0.0, 0, 15_000_000))

        joined = await _attach(async_db, diver, dive, _export(samples=transmitter), filename="b.json")

        assert joined.recording_id == stored.recording_id
        await self._assert_merged(async_db, stored.recording_id, identity)
        labels = (
            await async_db.execute(select(DiveMixture.gas_number).where(DiveMixture.dive_id == dive.id))
        ).scalars()
        assert list(labels) == [3]

    @pytest.mark.asyncio
    async def test_a_header_only_file_joining_an_imported_recording_keeps_the_documents_samples(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        imported = await _import_recording(
            async_db, db, diver, dive, cns_end=12.5, otu_end=31.0, start=datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC)
        )

        joined = await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="header.json")

        assert joined.recording_id == imported.id
        assert (
            await async_db.execute(select(DiveProfile.parser_key).where(DiveProfile.recording_id == imported.id))
        ).scalar_one() == IMPORT_PARSER_KEY

    @pytest.mark.asyncio
    async def test_a_sampled_file_joining_an_imported_recording_replaces_the_documents_samples(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """A whole second reading of the record, which the import writer's fill relies on."""
        imported = await _import_recording(
            async_db, db, diver, dive, cns_end=12.5, otu_end=31.0, start=datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC)
        )
        # The document's span is the file's, as it is for a document converted from it.
        await async_db.execute(
            update(DiveProfile).where(DiveProfile.recording_id == imported.id).values(duration=10_000)
        )
        await async_db.commit()

        joined = await _attach(async_db, diver, dive, _export(samples=self.SAMPLES), filename="export.json")

        assert joined.recording_id == imported.id
        assert (
            await async_db.execute(select(DiveProfile.parser_key).where(DiveProfile.recording_id == imported.id))
        ).scalar_one() == "suunto_json"


class TestDeletingASecondComputersFile:
    """Emptying a second computer's recording must not touch the primary's readings.

    The single-recording cases above are the ones the deletion path was written for, and on
    those the outright rewrite is the point. These are the multi-recording ones, where the
    recording being emptied is a second computer's and the figures the dive shows belong to
    the primary.
    """

    @pytest.mark.asyncio
    async def test_a_file_less_primarys_document_figures_survive_it(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """The shape a logbook import creates from a document: a primary holding samples and
        no bytes, with the document's figures on it. Nothing on this instance can produce either
        number again, so deleting the second computer's export must leave them.
        """
        imported = await _import_recording(async_db, db, diver, dive, cns_end=12.5, otu_end=31.0)
        # Months from the imported recording's start, so it lands beside it rather than in it.
        stored = await _attach(async_db, diver, dive, _export(cns_end=44.0, samples=_samples((0, "0"))))
        assert [row.ordinal for row in await _recordings(async_db, dive)] == [0, 1]
        file_id = (
            await async_db.execute(select(DiveFile.id).where(DiveFile.recording_id == stored.recording_id))
        ).scalar_one()

        await delete_dive_file(async_db, file_id=file_id)

        assert [row.id for row in await _recordings(async_db, dive)] == [imported.id]
        assert await _readings(async_db, dive) == (12.5, 31.0)

    @pytest.mark.asyncio
    async def test_a_figure_the_primarys_own_file_does_not_yield_survives_it(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """A primary with files is no protection either, and this is the sharper case.

        The diver imported a document, then attached the export it was converted from - the
        primary's *first* file, which fills and cannot overwrite, so the primary keeps the
        document's `cns_end` and the `otu_end` this file does not carry at all. Rewriting outright
        off that file would replace the first with the file's own 9.0 and clear the second,
        on the strength of deleting an unrelated recording's export.
        """
        await _import_recording(
            async_db,
            db,
            diver,
            dive,
            cns_end=12.5,
            otu_end=31.0,
            start=datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC),
        )
        await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="primary.json")
        other = _export(start="2026-09-08T15:19:38.670+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")
        stored = await _attach(async_db, diver, dive, other, filename="second.json")
        assert [row.ordinal for row in await _recordings(async_db, dive)] == [0, 1]
        file_id = (
            await async_db.execute(select(DiveFile.id).where(DiveFile.recording_id == stored.recording_id))
        ).scalar_one()

        await delete_dive_file(async_db, file_id=file_id)

        assert await _readings(async_db, dive) == (12.5, 31.0)

    @pytest.mark.asyncio
    async def test_the_primarys_own_last_file_still_re_derives_from_the_promotion(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """The other half of the guard: where the deletion *does* reach the primary, the
        rewrite is owed and still runs. The primary goes with its last file, the second
        computer's recording is promoted into ordinal 0, and the reading the dive shows becomes
        that machine's own.
        """
        first = await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="first.json")
        other = _export(start="2026-09-08T15:19:38.670+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")
        second = await _attach(async_db, diver, dive, other, filename="second.json")
        file_id = (
            await async_db.execute(select(DiveFile.id).where(DiveFile.recording_id == first.recording_id))
        ).scalar_one()

        await delete_dive_file(async_db, file_id=file_id)

        assert [(row.id, row.ordinal) for row in await _recordings(async_db, dive)] == [(second.recording_id, 0)]
        assert await _cns_end(async_db, dive) == 44.0


class TestDeletingARecording:
    """`DELETE /dive/{uuid}/recording/{rid}`, which is the only way to remove a file-less one.

    Through the real handler rather than a service: the question these pin is *which*
    recording was removed, and that is decided in the route - before the delete renumbers the
    ordinals and takes the answer with it.
    """

    @pytest.mark.asyncio
    async def test_erasing_a_second_computer_leaves_the_dives_readings_alone(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive, routed: None
    ) -> None:
        imported = await _import_recording(async_db, db, diver, dive, cns_end=12.5, otu_end=31.0)
        stored = await _attach(async_db, diver, dive, _export(cns_end=44.0, samples=_samples((0, "0"))))
        second = next(row for row in await _recordings(async_db, dive) if row.id == stored.recording_id)

        await _erase_recording(async_db, diver, dive, second.uuid)

        assert [row.id for row in await _recordings(async_db, dive)] == [imported.id]
        assert await _readings(async_db, dive) == (12.5, 31.0)

    @pytest.mark.asyncio
    async def test_erasing_the_primary_re_derives_from_whatever_is_promoted(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive, routed: None
    ) -> None:
        first = await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="first.json")
        other = _export(start="2026-09-08T15:19:38.670+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")
        second = await _attach(async_db, diver, dive, other, filename="second.json")
        primary = next(row for row in await _recordings(async_db, dive) if row.id == first.recording_id)

        await _erase_recording(async_db, diver, dive, primary.uuid)

        assert [row.id for row in await _recordings(async_db, dive)] == [second.recording_id]
        assert await _cns_end(async_db, dive) == 44.0


class TestPromotingARecording:
    """`PATCH /dive/{uuid}/recording/{rid}`, the third route the readings follow.

    Its own class rather than a member of the two above, because it deletes nothing: a
    promotion has touched the primary by definition - it is what it just did - so it is the
    one caller that always re-derives, and the question the deletion routes answer does not
    arise here.
    """

    @pytest.mark.asyncio
    async def test_promoting_a_second_computer_re_derives_from_it(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive, routed: None
    ) -> None:
        """The readings follow the recording the diver made primary, and come off that
        machine's own file rather than staying at the one the dive was showing."""
        await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="first.json")
        other = _export(start="2026-09-08T15:19:38.670+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")
        stored = await _attach(async_db, diver, dive, other, filename="second.json")
        second = next(row for row in await _recordings(async_db, dive) if row.id == stored.recording_id)

        await patch_dive_recording(
            request=Mock(),
            uuid=dive.uuid,
            rid=second.uuid,
            values=RecordingUpdateRequest(primary=True),
            current_user={"id": diver.id, "uuid": diver.uuid, "is_superuser": False},
            db=async_db,
        )

        assert [(row.id, row.ordinal) for row in await _recordings(async_db, dive)][0] == (stored.recording_id, 0)
        assert await _cns_end(async_db, dive) == 44.0


FIXTURES = Path(__file__).parent / "fixtures" / "dive_files"
# One Ocean dive's two exports - the JSON has both fixes, the FIT an exit alone - and another
# dive's JSON, months earlier, which lands as a recording of its own.
OCEAN_JSON = (FIXTURES / "suunto-ocean-2026.json").read_bytes()
OCEAN_FIT = (FIXTURES / "suunto-ocean-2026.fit").read_bytes()
OTHER_JSON = (FIXTURES / "suunto-ocean.json").read_bytes()
OCEAN_ENTRY, OCEAN_EXIT, FIT_EXIT = (28.496525, 34.5168), (28.496447, 34.516765), (28.496523, 34.516778)
OTHER_ENTRY, OTHER_EXIT = (28.567251, 34.533257), (28.56723, 34.533233)
NO_FIX = (None, None)
DOCUMENT = (27.0, 33.0)


async def _attach_as(db: AsyncSession, diver: User, dive: Dive, content: bytes, parser_key: str) -> Any:
    return await store_recording_file(
        db,
        user_id=diver.id,
        user_uuid=diver.uuid,
        dive_id=dive.id,
        upload=UploadFile(filename=f"export.{parser_key}", file=io.BytesIO(content)),
        file_token=create_dive_file_token(
            user_uuid=diver.uuid, sha256=hashlib.sha256(content).hexdigest(), parser_key=parser_key
        ),
    )


async def _dive_fixes(db: AsyncSession, dive: Dive) -> tuple[tuple[float | None, float | None], ...]:
    row = (
        await db.execute(
            select(Dive.entry_latitude, Dive.entry_longitude, Dive.exit_latitude, Dive.exit_longitude).where(
                Dive.id == dive.id
            )
        )
    ).one()
    return (row[0], row[1]), (row[2], row[3])


async def _set_dive_fixes(db: AsyncSession, dive: Dive, entry: tuple[float, float]) -> None:
    """A position only a logbook document gave: nothing on this instance can re-derive it."""
    await db.execute(update(Dive).where(Dive.id == dive.id).values(entry_latitude=entry[0], entry_longitude=entry[1]))
    await db.commit()


async def _file_id(db: AsyncSession, recording_id: int) -> int:
    return (await db.execute(select(DiveFile.id).where(DiveFile.recording_id == recording_id))).scalar_one()


async def _promote(db: AsyncSession, diver: User, dive: Dive, rid: uuid_pkg.UUID) -> None:
    await patch_dive_recording(
        request=Mock(),
        uuid=dive.uuid,
        rid=rid,
        values=RecordingUpdateRequest(primary=True),
        current_user={"id": diver.id, "uuid": diver.uuid, "is_superuser": False},
        db=db,
    )


class TestTheDivesFixes:
    """Each fix is the first of the dive's files' that has one: recordings by ordinal, each
    one's files in attach order, whichever recording that is - and the dive read lists each
    file's own in that same order, so a client can apply the rule to a set it has not saved."""

    @pytest.mark.asyncio
    async def test_a_second_computers_fix_is_the_dives_where_the_primary_has_none(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        await _attach(async_db, diver, dive, _export(start="2026-01-02T10:00:00.000+02:00"), filename="no-fix.json")
        await _attach_as(async_db, diver, dive, OTHER_JSON, "suunto_json")

        assert await _dive_fixes(async_db, dive) == (OTHER_ENTRY, OTHER_EXIT)
        recordings = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]
        assert [
            [(file.entry_latitude, file.entry_longitude) for file in recording.files] for recording in recordings
        ] == [[NO_FIX], [OTHER_ENTRY]]

    @pytest.mark.asyncio
    async def test_within_a_recording_each_fix_is_the_first_file_that_has_it(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """Taken one fix at a time: the FIT came first and has an exit and no entry."""
        await _attach_as(async_db, diver, dive, OCEAN_FIT, "fit")
        await _attach_as(async_db, diver, dive, OCEAN_JSON, "suunto_json")

        assert len(await _recordings(async_db, dive)) == 1
        assert await _dive_fixes(async_db, dive) == (OCEAN_ENTRY, FIT_EXIT)

    @pytest.mark.asyncio
    async def test_deleting_the_file_that_gave_them_falls_to_the_next_and_then_to_none(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        first = await _attach_as(async_db, diver, dive, OCEAN_JSON, "suunto_json")
        second = await _attach_as(async_db, diver, dive, OTHER_JSON, "suunto_json")
        assert await _dive_fixes(async_db, dive) == (OCEAN_ENTRY, OCEAN_EXIT)

        await delete_dive_file(async_db, file_id=await _file_id(async_db, first.recording_id))
        assert await _dive_fixes(async_db, dive) == (OTHER_ENTRY, OTHER_EXIT)

        await delete_dive_file(async_db, file_id=await _file_id(async_db, second.recording_id))
        assert await _dive_fixes(async_db, dive) == (NO_FIX, NO_FIX)

    @pytest.mark.asyncio
    async def test_a_file_added_clears_a_fix_no_file_has(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """The cost of one rule: a dive holding files takes its fixes from them alone."""
        await _set_dive_fixes(async_db, dive, DOCUMENT)

        await _attach(async_db, diver, dive, _export(), filename="no-fix.json")

        assert await _dive_fixes(async_db, dive) == (NO_FIX, NO_FIX)

    @pytest.mark.asyncio
    async def test_promoting_a_recording_puts_its_files_first(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive, routed: None
    ) -> None:
        await _attach_as(async_db, diver, dive, OCEAN_JSON, "suunto_json")
        stored = await _attach_as(async_db, diver, dive, OTHER_JSON, "suunto_json")
        second = next(row for row in await _recordings(async_db, dive) if row.id == stored.recording_id)

        await _promote(async_db, diver, dive, second.uuid)

        assert await _dive_fixes(async_db, dive) == (OTHER_ENTRY, OTHER_EXIT)

    @pytest.mark.asyncio
    async def test_erasing_a_recording_that_held_no_file_keeps_a_documents_fix(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive, routed: None
    ) -> None:
        await _import_recording(async_db, db, diver, dive, cns_end=12.5, otu_end=31.0)
        second = create_dive_recording(db, diver, dive, ordinal=1)
        await _set_dive_fixes(async_db, dive, DOCUMENT)

        await _erase_recording(async_db, diver, dive, second.uuid)

        assert await _dive_fixes(async_db, dive) == (DOCUMENT, NO_FIX)

    @pytest.mark.asyncio
    async def test_erasing_a_recording_that_held_the_fixing_file_derives_again(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive, routed: None
    ) -> None:
        await _attach(async_db, diver, dive, _export(start="2026-01-02T10:00:00.000+02:00"), filename="no-fix.json")
        stored = await _attach_as(async_db, diver, dive, OTHER_JSON, "suunto_json")
        second = next(row for row in await _recordings(async_db, dive) if row.id == stored.recording_id)

        await _erase_recording(async_db, diver, dive, second.uuid)

        assert await _dive_fixes(async_db, dive) == (NO_FIX, NO_FIX)


class TestADocumentsFixFillsOnlyAFileLessDive:
    @pytest.mark.asyncio
    async def test_a_dive_with_no_file_takes_it(self, async_db: AsyncSession, dive: Dive) -> None:
        await fill_tech_scalars(async_db, dive_id=dive.id, scalars={"entry_latitude": 27.0, "entry_longitude": 33.0})
        await async_db.commit()

        assert await _dive_fixes(async_db, dive) == (DOCUMENT, NO_FIX)

    @pytest.mark.asyncio
    async def test_a_dive_holding_a_file_does_not(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        await _attach(async_db, diver, dive, _export(), filename="no-fix.json")

        await fill_tech_scalars(async_db, dive_id=dive.id, scalars={"entry_latitude": 27.0, "entry_longitude": 33.0})
        await async_db.commit()

        assert await _dive_fixes(async_db, dive) == (NO_FIX, NO_FIX)


class TestReadingUnreadFixes:
    """The data path for files stored before their fixes were, or restored without a read."""

    @staticmethod
    async def _unread(db: AsyncSession, dive: Dive, **values: Any) -> None:
        await db.execute(
            update(DiveFile)
            .where(DiveFile.dive_id == dive.id)
            .values(
                entry_latitude=None,
                entry_longitude=None,
                exit_latitude=None,
                exit_longitude=None,
                fixes_read=False,
                **values,
            )
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_a_stored_files_fixes_are_read_and_become_the_dives(
        self, volume: Any, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        monkeypatch.setattr("src.app.services.cache_invalidation.invalidate_dive_caches", AsyncMock())
        await _attach_as(async_db, diver, dive, OCEAN_JSON, "suunto_json")
        await self._unread(async_db, dive)
        await _set_dive_fixes(async_db, dive, DOCUMENT)

        while await read_unread_file_fixes(async_db, limit=50) == 50:
            pass

        row = (await async_db.execute(select(DiveFile).where(DiveFile.dive_id == dive.id))).scalar_one()
        await async_db.refresh(row)
        assert (row.fixes_read, row.entry_latitude, row.exit_longitude) == (True, OCEAN_ENTRY[0], OCEAN_EXIT[1])
        assert await _dive_fixes(async_db, dive) == (OCEAN_ENTRY, OCEAN_EXIT)

    @pytest.mark.asyncio
    async def test_a_file_this_build_cannot_read_is_marked_read_and_clears_nothing(
        self, volume: Any, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """Marked, or the worker's loop selects it forever; and the dive keeps a document's fix."""
        monkeypatch.setattr("src.app.services.cache_invalidation.invalidate_dive_caches", AsyncMock())
        await _attach_as(async_db, diver, dive, OCEAN_JSON, "suunto_json")
        await self._unread(async_db, dive, parser_key=IMPORT_PARSER_KEY)
        await _set_dive_fixes(async_db, dive, DOCUMENT)

        while await read_unread_file_fixes(async_db, limit=50) == 50:
            pass

        fixes_read = (
            await async_db.execute(select(DiveFile.fixes_read).where(DiveFile.dive_id == dive.id))
        ).scalar_one()
        assert fixes_read is True
        assert await _dive_fixes(async_db, dive) == (DOCUMENT, OCEAN_EXIT)


class TestOrdinals:
    @pytest.mark.asyncio
    async def test_deleting_the_primary_promotes_the_next(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        first = create_dive_recording(db, diver, dive, ordinal=0)
        second = create_dive_recording(db, diver, dive, ordinal=1)
        third = create_dive_recording(db, diver, dive, ordinal=2)

        await delete_recording(async_db, recording_id=first.id, dive_id=dive.id, commit=True)

        assert [(row.id, row.ordinal) for row in await _recordings(async_db, dive)] == [(second.id, 0), (third.id, 1)]

    @pytest.mark.asyncio
    async def test_promotion_keeps_the_rest_in_order(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """Through a temporary negative slot: `ux_dive_recording_dive_id_ordinal` is checked
        per statement, so shifting 1 -> 0 while 0 still exists is a violation even when the
        row at 0 is about to move too."""
        first = create_dive_recording(db, diver, dive, ordinal=0)
        second = create_dive_recording(db, diver, dive, ordinal=1)
        third = create_dive_recording(db, diver, dive, ordinal=2)

        await make_primary(async_db, recording_id=third.id, dive_id=dive.id)
        await async_db.commit()

        assert [(row.id, row.ordinal) for row in await _recordings(async_db, dive)] == [
            (third.id, 0),
            (first.id, 1),
            (second.id, 2),
        ]

    @pytest.mark.asyncio
    async def test_renumbering_closes_a_gap(self, async_db: AsyncSession, db: Session, diver: User, dive: Dive) -> None:
        first = create_dive_recording(db, diver, dive, ordinal=0)
        third = create_dive_recording(db, diver, dive, ordinal=7)

        await renumber_ordinals(async_db, dive_id=dive.id)
        await async_db.commit()

        assert [(row.id, row.ordinal) for row in await _recordings(async_db, dive)] == [(first.id, 0), (third.id, 1)]

    @pytest.mark.asyncio
    async def test_the_next_slot_is_read_rather_than_counted(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """A count would collide with a gap. Nothing should leave one - `delete_recording`
        renumbers - and reading the maximum is what keeps that an invariant rather than a
        dependency."""
        create_dive_recording(db, diver, dive, ordinal=0)
        create_dive_recording(db, diver, dive, ordinal=4)

        assert await next_ordinal(async_db, dive_id=dive.id) == 5


class TestTheDiveRead:
    @pytest.mark.asyncio
    async def test_a_recording_with_no_files_is_first_class(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """What logbook import creates from a document, and what every dive of a logbook of
        several dives looks like: a device, a start, samples and no bytes."""
        recording = create_dive_recording(db, diver, dive)
        await async_db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(device_brand="Shearwater Research, Inc", device_model="Perdix 3", device_serial="D9772626")
        )
        await _insert_profile(async_db, dive, recording, parser_key=IMPORT_PARSER_KEY)
        await async_db.commit()

        read = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]

        assert len(read) == 1
        assert read[0].files == []
        assert read[0].device is not None and read[0].device.serial == "D9772626"
        assert read[0].profile is not None and read[0].profile.duration == 2940

    @pytest.mark.asyncio
    async def test_the_two_file_less_recordings_are_told_apart_by_provenance(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """The dive read's only means of telling them apart, and the reason the member exists.
        Both carry samples and an empty `files`, and the sentence a client shows differs:
        samples an import took from a document against samples a merge put together.
        """
        imported = create_dive_recording(db, diver, dive, ordinal=0)
        merged = create_dive_recording(db, diver, dive, ordinal=1)
        await _insert_profile(async_db, dive, imported, parser_key=IMPORT_PARSER_KEY)
        await _insert_profile(async_db, dive, merged, parser_key=MERGE_PARSER_KEY)
        await async_db.commit()

        read = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]

        assert [row.files for row in read] == [[], []]
        assert [row.profile.provenance for row in read if row.profile is not None] == [
            ProfileProvenance.DIVEJSON_IMPORT,
            ProfileProvenance.MERGE,
        ]

    @pytest.mark.asyncio
    async def test_a_profile_read_off_a_file_says_file_rather_than_which_format(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """Which format each file was read as is already on `files[].parser_key`, where it is a
        fact about that file. The profile answers the three-way question and nothing else, so
        the open set of format ids never reaches this member."""
        await _attach(async_db, diver, dive, _export(samples=_samples((0, "0"), (10, "5"))))

        read = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]

        assert read[0].profile is not None
        assert read[0].profile.provenance is ProfileProvenance.FILE
        assert [row.parser_key for row in read[0].files] == ["suunto_json"]

    @pytest.mark.asyncio
    async def test_a_recording_whose_source_named_no_computer_reports_no_device(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """`null` rather than an object of six nulls, on `_drop_empty_device`'s terms: a
        recording whose source said nothing must not come back claiming it named one."""
        create_dive_recording(db, diver, dive)

        read = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]

        assert read[0].device is None


class TestTheDiveList:
    @pytest.mark.asyncio
    async def test_each_row_counts_its_recordings(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        create_dive_recording(db, diver, dive)
        create_dive_recording(db, diver, dive, ordinal=1)
        hand_logged = create_dive(db, diver)

        page = await dives_module._cached_read_dives.__wrapped__(  # type: ignore[attr-defined]
            request=None,
            user_id=diver.id,
            user_uuid=diver.uuid,
            db=async_db,
            page=1,
            items_per_page=10,
            trip_id=None,
            course_id=None,
            dive_site_id=None,
            gear_item_id=None,
            species_id=None,
            person_id=None,
            tag_id=None,
            dive_type=None,
            sort=DiveListSort.DATE,
        )

        counts = {row["uuid"]: row["recording_count"] for row in page["data"]}
        assert counts == {dive.uuid: 2, hand_logged.uuid: 0}
