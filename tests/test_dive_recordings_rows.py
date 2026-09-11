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
from src.app.schemas.dive import RecordingUpdateRequest
from src.app.schemas.dive_profile import ProfileProvenance
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


async def _readings(db: AsyncSession, dive: Dive) -> tuple[float | None, float | None]:
    """`(cns_end, otu_end)`. The pair rather than the one reading, because the two answer
    different halves of the same question: `cns_end` is a figure a file can also yield and
    `otu_end` one nothing here parses, so a rewrite that replaced the first and cleared the
    second is visible as two different wrong numbers rather than as one."""
    row = (await db.execute(select(Dive.cns_end, Dive.otu_end).where(Dive.id == dive.id))).one()
    return row.cns_end, row.otu_end


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
    """A converted logbook import, as `services/logbook_import/writer.py` leaves it: the
    document's figures on the dive row, beside a primary recording that holds samples and no
    bytes. Nothing on this instance can re-derive either number."""
    recording = create_dive_recording(sync_db, diver, dive, ordinal=0)
    if start is not None:
        await db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(start_time=start, utc_offset_minutes=180)
        )
    await _insert_profile(db, dive, recording, parser_key=IMPORT_PARSER_KEY)
    await db.execute(update(Dive).where(Dive.id == dive.id).values(cns_end=cns_end, otu_end=otu_end))
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
    async def test_the_upload_that_creates_a_recording_fills_nothing(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """Nothing arrived on a recording that already existed, because there wasn't one:
        this dive has no recording yet, so the upload makes it, and the dive's cylinders came
        off the form this very parse pre-filled. A fill would only put back a blank the diver
        had just cleared. **Not "a recording's first file"** - two tests below are that and
        one of them fills."""
        self._seed_cylinder(db, dive, gas_number=0, start_pressure=200.0)

        await _attach(async_db, diver, dive, _export(cylinder="<Oxygen>33</Oxygen>"), filename="ocean.xml")

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
        export = _export(cylinder="<Oxygen>33</Oxygen>")
        await _attach(async_db, diver, dive, export, filename="ocean.xml")

        await _attach(async_db, diver, dive, export, filename="ocean.xml")

        assert (await self._cylinder(async_db, dive)).oxygen is None

    @pytest.mark.asyncio
    async def test_the_first_file_of_an_imported_recording_does_fill(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """**`fresh` is "this upload created the recording", not "it had no files"**, and this
        is where the two answers differ.

        A converted logbook import creates a recording with samples, no device and no bytes.
        When the diver later attaches the export it was converted from, that is the
        recording's *first* file and still a second reading of a record the dive already
        describes - so it fills. A `fresh` that keyed on the file count would take the
        outright branch here and the cylinder would keep its blank.
        """
        recording = create_dive_recording(db, diver, dive)
        await async_db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(start_time=datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), utc_offset_minutes=180)
        )
        await async_db.commit()
        self._seed_cylinder(db, dive, gas_number=0, start_pressure=200.0)

        await _attach(async_db, diver, dive, _export(cylinder="<Oxygen>33</Oxygen>"), filename="ocean.xml")

        # One recording still, so the file landed on the imported one rather than beside it.
        assert [row.id for row in await _recordings(async_db, dive)] == [recording.id]
        assert (await self._cylinder(async_db, dive)).oxygen == 33.0


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


class TestDeletingASecondComputersFile:
    """Emptying a recording the dive's readings never came off must not touch them.

    The single-recording cases above are the ones the deletion path was written for, and on
    those the outright rewrite is the point. These are the multi-recording ones, where the
    same rewrite has nothing to re-derive *from* and everything to lose: the recording being
    emptied is a second computer's, the figures on the dive belong to the primary, and
    `refresh_tech_scalars` would have rewritten them from a recording this deletion did not
    touch.
    """

    @pytest.mark.asyncio
    async def test_a_file_less_primarys_document_figures_survive_it(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """The shape a converted logbook import creates: a primary holding samples and no
        bytes, with the document's figures on the dive. An outright rewrite here reads an
        empty extraction off that primary and writes every field `None`, so deleting the
        second computer's export would clear two numbers the document supplied and nothing on
        this instance can produce again.
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
        primary's *first* file, which fills and cannot overwrite, so the dive keeps the
        document's `cns_end` and the `otu_end` no parser here reads at all. Rewriting outright
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
        await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="primary.xml")
        other = _export(start="2026-09-08T15:19:38.67+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")
        stored = await _attach(async_db, diver, dive, other, filename="second.xml")
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
        computer's recording is promoted into ordinal 0, and the dive's reading becomes that
        machine's rather than being left at a number no recording of this dive claims.
        """
        first = await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="first.xml")
        other = _export(start="2026-09-08T15:19:38.67+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")
        second = await _attach(async_db, diver, dive, other, filename="second.xml")
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
        first = await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="first.xml")
        other = _export(start="2026-09-08T15:19:38.67+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")
        second = await _attach(async_db, diver, dive, other, filename="second.xml")
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
        await _attach(async_db, diver, dive, _export(cns_end=9.0), filename="first.xml")
        other = _export(start="2026-09-08T15:19:38.67+03:00", cns_end=44.0).replace(b"253810000400", b"999999999999")
        stored = await _attach(async_db, diver, dive, other, filename="second.xml")
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
        *no file kept: imported through the converter* against *merged from two recordings*.
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
    async def test_a_profile_read_off_a_file_says_file_rather_than_which_parser(
        self, volume: Any, async_db: AsyncSession, diver: User, dive: Dive
    ) -> None:
        """Which parser read which file is already on `files[].parser_key`, where it is a fact
        about that file. The profile answers the three-way question and nothing else, so the
        open, growing set of parser keys never reaches this member."""
        await _attach(async_db, diver, dive, _export(samples=_samples((0, "0"), (10, "5"))))

        read = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]

        assert read[0].profile is not None
        assert read[0].profile.provenance is ProfileProvenance.FILE
        assert [row.parser_key for row in read[0].files] == [SuuntoXmlParser.key]

    @pytest.mark.asyncio
    async def test_a_recording_whose_source_named_no_computer_reports_no_device(
        self, async_db: AsyncSession, db: Session, diver: User, dive: Dive
    ) -> None:
        """`null` rather than an object of six nulls, on `_drop_empty_device`'s terms: a
        recording whose source said nothing must not come back claiming it named one."""
        create_dive_recording(db, diver, dive)

        read = (await get_recordings_for_dives(async_db, dive_ids=[dive.id]))[dive.id]

        assert read[0].device is None
