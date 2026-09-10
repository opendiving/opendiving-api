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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import UploadFile
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.core.security import create_dive_file_token
from src.app.core.utils.datetime_offset import combine_start_time
from src.app.models.dive import Dive
from src.app.models.dive_file import DiveFile
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_recording import DiveRecording
from src.app.models.user import User
from src.app.services import blob_store
from src.app.services.dive_files import delete_dive_file, store_recording_file
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser
from src.app.services.dive_profiles import IMPORT_PARSER_KEY, MERGE_PARSER_KEY, NormalizedProfile, ProfileSeries
from src.app.services.dive_recordings import (
    delete_recording,
    fill_start,
    get_recordings_for_dives,
    make_primary,
    next_ordinal,
    renumber_ordinals,
)
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_recording, create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"


def _export(
    *,
    cns_end: float | None = None,
    samples: str = "",
    start: str = "2026-09-08T15:17:38.67+03:00",
    cylinder: str = "",
) -> bytes:
    """A minimal Suunto XML export. `SuuntoXmlParser` is the cheapest of the three and the
    one whose bytes can be written inline, which is what keeps these tests about the
    recording rather than about a format."""
    exposure = "" if cns_end is None else f"<CnsEnd>{cns_end}</CnsEnd>"
    mixtures = "" if not cylinder else f"<DiveMixtures><DiveMixture>{cylinder}</DiveMixture></DiveMixtures>"
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><StartTime>{start}</StartTime><Duration>1800</Duration>
<MaxDepth>19.04</MaxDepth><SerialNumber>253810000400</SerialNumber>{exposure}
{mixtures}{samples}</Dive>
""".encode()


def _samples(*depths: tuple[int, str]) -> str:
    inner = "".join(f"<Dive.Sample><Time>{t}</Time><Depth>{d}</Depth></Dive.Sample>" for t, d in depths)
    return f"<DiveSamples>{inner}</DiveSamples>"


async def _cns_end(db: AsyncSession, dive: Dive) -> float | None:
    """Read back through a query rather than `refresh`: the `dive` fixture belongs to the
    *sync* session that seeded it, and is not persistent in the async one under test."""
    return (await db.execute(select(Dive.cns_end).where(Dive.id == dive.id))).scalar_one()


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


async def _attach(db: AsyncSession, diver: User, dive: Dive, content: bytes, *, filename: str = "export.xml") -> Any:
    return await store_recording_file(
        db,
        user_id=diver.id,
        user_uuid=diver.uuid,
        dive_id=dive.id,
        upload=UploadFile(filename=filename, file=io.BytesIO(content)),
        file_token=create_dive_file_token(
            user_uuid=diver.uuid, sha256=hashlib.sha256(content).hexdigest(), parser_key=SuuntoXmlParser.key
        ),
    )


async def _recordings(db: AsyncSession, dive: Dive) -> list[DiveRecording]:
    rows = await db.execute(
        select(DiveRecording).where(DiveRecording.dive_id == dive.id).order_by(DiveRecording.ordinal)
    )
    return list(rows.scalars().all())


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
        # The device's own logged figures, off the header - not the samples' span. The two
        # differ on a real file and the column's meaning is "a duration the gate can
        # compare", not one number with one meaning.
        assert (recordings[0].duration, recordings[0].max_depth) == (1800, 19.04)

    @pytest.mark.asyncio
    async def test_a_second_export_of_one_computer_fills_the_same_recording(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """The whole point of a recording. Two spellings of one record, and the second
        contributes what the first was silent about without overwriting anything."""
        first = await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="dive.xml")
        # Half a second later by its own clock, and it records an exposure reading the first
        # did not. Same serial, so the same-recording gate takes it.
        second = await _attach(
            async_db,
            diver,
            dive,
            _export(start="2026-09-08T15:17:39.17+03:00", samples=_samples((0, "0"), (10, "5"))),
            filename="dive.fit.xml",
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
        first. The dive's exposure readings stay the primary recording's - a second machine's
        CNS clock is its own device's arithmetic."""
        await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="first.xml")
        other = _export(start="2026-09-08T15:19:38.67+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")

        stored = await _attach(async_db, diver, dive, other, filename="second.xml")

        recordings = await _recordings(async_db, dive)
        assert [row.ordinal for row in recordings] == [0, 1]
        assert recordings[1].id == stored.recording_id
        assert await _cns_end(async_db, dive) == 9.0

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
        assert (volume / key).read_bytes() == content

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

    Here rather than beside the pure `fill_mixture_fields` tests because the interesting part
    is the join: the parsed cylinders come off the recording's files, the stored ones off the
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
            _export(cns_end=9.0, cylinder="<StartPressure>207340</StartPressure><EndPressure>47470</EndPressure>"),
            filename="ocean.xml",
        )
        await _attach(
            async_db,
            diver,
            dive,
            _export(
                start="2026-09-08T15:17:39.17+03:00",
                samples=_samples((0, "0"), (10, "5")),
                cylinder="<Oxygen>33</Oxygen>",
            ),
            filename="ocean-fit.xml",
        )

        assert [row.ordinal for row in await _recordings(async_db, dive)] == [0]
        cylinder = await self._cylinder(async_db, dive)
        assert cylinder.oxygen == 33.0
        assert (cylinder.start_pressure, cylinder.end_pressure) == (200.0, 47.47)
        # The label the dive's stored pressure channels are attributed under. A second file's
        # own numbering must never rename it - this format counts from 1, the Ocean from 0.
        assert cylinder.gas_number == 0

    @pytest.mark.asyncio
    async def test_a_recordings_first_file_fills_nothing(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """The same branch `store_tech_scalars`/`fill_tech_scalars` turn on, applied to the
        cylinders. A recording's first file has nothing to add: the dive's rows came off the
        form this very parse pre-filled, so a fill here would only put back a blank the diver
        had just cleared."""
        self._seed_cylinder(db, dive, gas_number=0, start_pressure=200.0)

        await _attach(async_db, diver, dive, _export(cylinder="<Oxygen>33</Oxygen>"), filename="ocean.xml")

        assert (await self._cylinder(async_db, dive)).oxygen is None


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
        """What logbook import creates from a converted document, and what every UDDF and
        `.ssrf` dive in the app looks like: a device, a start, samples and no bytes."""
        recording = create_dive_recording(db, diver, dive)
        await async_db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(device_brand="Shearwater Research, Inc", device_model="Perdix 3", device_serial="D9772626")
        )
        await async_db.execute(
            insert(DiveProfile).values(
                recording_id=recording.id,
                dive_id=dive.id,
                source_sha256="b" * 64,
                parser_key=IMPORT_PARSER_KEY,
                extractor_version=3,
                duration=2940,
                depth_sample_count=2,
                data=NormalizedProfile(depth=ProfileSeries(t=[0, 10], v=[0, 1900])).to_data(),
                uuid=recording.uuid,
                created_at=datetime.now(UTC),
            )
        )
        await async_db.commit()

        read = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]

        assert len(read) == 1
        assert read[0].files == []
        assert read[0].device is not None and read[0].device.serial == "D9772626"
        assert read[0].profile is not None and read[0].profile.duration == 2940

    @pytest.mark.asyncio
    async def test_a_recording_whose_source_named_no_computer_reports_no_device(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """`null` rather than an object of six nulls, on `_drop_empty_device`'s terms: a
        recording whose source said nothing must not come back claiming it named one."""
        create_dive_recording(db, diver, dive)

        read = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]

        assert read[0].device is None
