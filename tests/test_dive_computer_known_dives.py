"""Which of a computer's listed dives the logbook holds, against real Postgres.

Each clause that makes a listed dive held has a test, and so does each way it must not fire:
a wrong "held" hides a dive the logbook lacks from the diver who would download it.
"""

import json
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.v1 import dives as dives_module
from src.app.api.v1.dive_computers import read_dive_computer_known_dives
from src.app.api.v1.dives import _cached_read_dive
from src.app.models.dive import Dive
from src.app.models.dive_file import DiveFile
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_recording import DiveRecording
from src.app.models.user import User
from src.app.schemas.dive_computer import MAX_LISTED_DIVES, DiveComputerListing
from src.app.services import blob_store
from src.app.services.dive_profiles import IMPORT_PARSER_KEY, MERGE_PARSER_KEY
from src.app.services.libdivecomputer import LIBDIVECOMPUTER
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_recording, create_user
from tests.helpers.held_imports import caller
from tests.test_dive_merge_rows import FIRST_START, SECOND_START, _merge, _two_halves

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

PERDIX = "D9772626"
OCEAN = "253810000400"
# The worked row: a recording stored as a wall clock with no offset, and one stored as an instant.
WALL_CLOCK = datetime(2026, 10, 26, 15, 3, 23, tzinfo=UTC)
INSTANT = datetime(2026, 10, 26, 12, 3, 23, tzinfo=UTC)


@pytest.fixture(autouse=True)
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


def _recorded(db: Session, user: User, serial: str | None, start: datetime | None, offset: int | None = None) -> Dive:
    """A dive of its own with one recording of `serial`, its start stored as the column pair."""
    dive = create_dive(db, user)
    recording: DiveRecording = create_dive_recording(db, user, dive)
    recording.device_serial, recording.start_time, recording.utc_offset_minutes = serial, start, offset
    db.commit()
    return dive


def _file(
    db: Session, recording: DiveRecording, parser_key: str, *, fallback_start: datetime | None = None
) -> DiveFile:
    """A stored file on a recording, tagged as a computer's original where its key says so."""
    digest = uuid_pkg.uuid4().hex * 2
    tagged = parser_key == LIBDIVECOMPUTER
    row = DiveFile(
        user_id=recording.user_id,
        recording_id=recording.id,
        dive_id=recording.dive_id,
        sha256=digest,
        content_type="application/octet-stream",
        byte_size=1,
        stored_byte_size=1,
        original_filename="dive.bin",
        parser_key=parser_key,
        storage_key=blob_store.new_key("dive-files", sha256=digest),
        descriptor_vendor="Suunto" if tagged else None,
        descriptor_product="Ocean" if tagged else None,
        descriptor_model=1 if tagged else None,
        fallback_start=fallback_start,
    )
    db.add(row)
    db.commit()
    return row


def _recording_of(db: Session, dive: Dive) -> DiveRecording:
    return db.execute(select(DiveRecording).where(DiveRecording.dive_id == dive.id)).scalar_one()


def _merged(db: Session, user: User, serial: str, start: datetime, offset: int, span_ms: int) -> DiveRecording:
    """A dive whose one recording is a merge's of `serial`, its samples spanning `span_ms`."""
    recording = _recording_of(db, _recorded(db, user, serial, start, offset))
    db.add(
        DiveProfile(
            recording_id=recording.id,
            dive_id=recording.dive_id,
            source_sha256="m" * 64,
            parser_key=MERGE_PARSER_KEY,
            extractor_version=1,
            duration=span_ms,
            depth_sample_count=2,
            data={"depth": {"t": [0, span_ms // 1000], "v": [100, 100]}},
        )
    )
    db.commit()
    return recording


def _listing(serial: str, *dives: tuple[uuid_pkg.UUID, str]) -> DiveComputerListing:
    """The body as the wire carries it."""
    return DiveComputerListing.model_validate_json(
        json.dumps({"serial": serial, "dives": [{"uuid": str(uuid), "started_at": start} for uuid, start in dives]})
    )


async def _known(
    db: AsyncSession, user: User, listing: DiveComputerListing
) -> list[tuple[uuid_pkg.UUID, uuid_pkg.UUID]]:
    rows = await read_dive_computer_known_dives(request=Mock(), listing=listing, current_user=caller(user), db=db)
    return [(row.uuid, row.dive_uuid) for row in rows]


async def _originals(db: AsyncSession, user: User, serial: str, *starts: str) -> dict[str, bool]:
    """`has_original` for each of these starts that is held, each listed under a uuid of its own."""
    listed = {uuid_pkg.uuid4(): start for start in starts}
    rows = await read_dive_computer_known_dives(
        request=Mock(), listing=_listing(serial, *listed.items()), current_user=caller(user), db=db
    )
    return {listed[row.uuid]: row.has_original for row in rows}


async def _held(db: AsyncSession, user: User, serial: str, *starts: str) -> list[str]:
    """Which of these starts, each listed under a uuid of its own, are held."""
    listed = {uuid_pkg.uuid4(): start for start in starts}
    known = dict(await _known(db, user, _listing(serial, *listed.items())))
    return [start for uuid, start in listed.items() if uuid in known]


class TestTheClockRule:
    @pytest.mark.asyncio
    async def test_the_worked_row(self, async_db: AsyncSession, db: Session) -> None:
        """A wall clock stored without an offset is compared as a wall clock with every listed
        start, within two seconds."""
        diver = create_user(db)
        held_by = _recorded(db, diver, PERDIX, WALL_CLOCK)

        assert await _held(
            async_db,
            diver,
            PERDIX,
            "2026-10-26T15:03:23",
            "2026-10-26T15:03:25",
            "2026-10-26T15:03:23+03:00",
            "2026-10-26T15:03:26",
            "2026-10-26T12:03:23Z",
        ) == ["2026-10-26T15:03:23", "2026-10-26T15:03:25", "2026-10-26T15:03:23+03:00"]
        listed = uuid_pkg.uuid4()
        assert await _known(async_db, diver, _listing(PERDIX, (listed, "2026-10-26T15:03:21"))) == [
            (listed, held_by.uuid)
        ]

    @pytest.mark.asyncio
    async def test_two_starts_with_offsets_are_compared_as_instants(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        _recorded(db, diver, PERDIX, INSTANT, 180)

        assert await _held(
            async_db,
            diver,
            PERDIX,
            "2026-10-26T12:03:23Z",
            "2026-10-26T14:03:24+02:00",
            "2026-10-26T15:03:23Z",
            "2026-10-26T15:03:23",
        ) == ["2026-10-26T12:03:23Z", "2026-10-26T14:03:24+02:00", "2026-10-26T15:03:23"]

    @pytest.mark.asyncio
    async def test_a_wall_clock_is_found_across_the_widest_offsets(self, async_db: AsyncSession, db: Session) -> None:
        """Fourteen hours and twelve between the stored column and the listed instant."""
        diver = create_user(db)
        _recorded(db, diver, PERDIX, WALL_CLOCK)

        assert await _held(async_db, diver, PERDIX, "2026-10-26T15:03:23+14:00", "2026-10-26T15:03:23-12:00") == [
            "2026-10-26T15:03:23+14:00",
            "2026-10-26T15:03:23-12:00",
        ]


class TestTheSerial:
    @pytest.mark.asyncio
    async def test_a_serial_differing_in_case_and_surrounding_spaces_is_one_computer(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        _recorded(db, diver, " d9772626 ", WALL_CLOCK)

        assert await _held(async_db, diver, PERDIX, "2026-10-26T15:03:23") == ["2026-10-26T15:03:23"]
        assert await _held(async_db, diver, "  D9772626", "2026-10-26T15:03:23") == ["2026-10-26T15:03:23"]

    @pytest.mark.asyncio
    async def test_the_same_start_under_another_serial_or_none_is_not_held(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        _recorded(db, diver, OCEAN, WALL_CLOCK)
        _recorded(db, diver, None, WALL_CLOCK)
        _recorded(db, diver, "   ", WALL_CLOCK)

        assert await _held(async_db, diver, PERDIX, "2026-10-26T15:03:23") == []
        assert await _held(async_db, diver, "   ", "2026-10-26T15:03:23") == []

    @pytest.mark.asyncio
    async def test_a_recording_with_no_start_holds_nothing(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        _recorded(db, diver, PERDIX, None)

        assert await _held(async_db, diver, PERDIX, "2026-10-26T15:03:23", "2026-06-01T09:00:00Z") == []


class TestTheDivesUuid:
    @pytest.mark.asyncio
    async def test_a_live_dive_of_the_uuid_holds_it_whatever_the_serial_and_start(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        dive = create_dive(db, diver)

        assert await _known(async_db, diver, _listing(OCEAN, (dive.uuid, "2001-01-01T00:00:00"))) == [
            (dive.uuid, dive.uuid)
        ]

    @pytest.mark.asyncio
    async def test_a_deleted_dive_holds_neither_its_uuid_nor_its_start(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        gone = _recorded(db, diver, PERDIX, WALL_CLOCK)
        db.execute(update(Dive).where(Dive.id == gone.id).values(is_deleted=True))
        db.commit()

        assert await _known(async_db, diver, _listing(PERDIX, (gone.uuid, "2026-10-26T15:03:23"))) == []

    @pytest.mark.asyncio
    async def test_a_dive_merged_into_another_holds_its_uuid_for_the_dive_it_joined_in_as_many_queries(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        """Merged twice over, and the start listed nowhere near either: the uuid alone holds it,
        for the dive the merges ended in, and a merged dive deleted afterwards holds nothing."""
        diver = create_user(db)
        survivor, middle, latest = create_dive(db, diver), create_dive(db, diver), create_dive(db, diver)
        gone, merged_into_gone = create_dive(db, diver), create_dive(db, diver)
        for dive, into in ((middle, survivor), (latest, middle), (merged_into_gone, gone)):
            db.execute(update(Dive).where(Dive.id == dive.id).values(is_deleted=True, merged_into_id=into.id))
        db.execute(update(Dive).where(Dive.id == gone.id).values(is_deleted=True))
        db.commit()
        listed = [(dive.uuid, "2001-01-01T00:00:00") for dive in (latest, middle, merged_into_gone)]
        statements: list[str] = []

        def record(*args: Any) -> None:
            statements.append(args[2])

        await _known(async_db, diver, _listing(PERDIX, listed[0]))
        engine = async_db.bind.sync_engine  # type: ignore[union-attr]
        event.listen(engine, "before_cursor_execute", record)
        try:
            await _known(async_db, diver, _listing(PERDIX, listed[0]))
            for_one = len(statements)
            statements.clear()
            known = await _known(async_db, diver, _listing(PERDIX, *listed))
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert known == [(latest.uuid, survivor.uuid), (middle.uuid, survivor.uuid)]
        assert len(statements) == for_one

    @pytest.mark.asyncio
    async def test_another_account_holds_nothing_of_the_caller_s(self, async_db: AsyncSession, db: Session) -> None:
        diver, someone = create_user(db), create_user(db)
        theirs = _recorded(db, someone, PERDIX, WALL_CLOCK)

        assert await _known(async_db, diver, _listing(PERDIX, (theirs.uuid, "2026-10-26T15:03:23"))) == []


class TestAMerge:
    @pytest.mark.asyncio
    async def test_both_records_of_a_merged_dive_are_held_and_nothing_past_its_samples(
        self, merging: None, async_db: AsyncSession, db: Session
    ) -> None:
        """Before the merge a start a minute into the first record is no recording's: that
        record's samples span three minutes, and only a merge's span holds. After it, the
        first record's start, the second's and that minute are held by the surviving dive, the
        second under the uuid of the dive the merge deleted too."""
        diver = create_user(db)
        first, second = await _two_halves(async_db, db, diver)
        a_minute_in = (datetime.fromisoformat(FIRST_START) + timedelta(seconds=60)).isoformat()
        assert await _held(async_db, diver, OCEAN, a_minute_in) == []

        await _merge(async_db, diver, first, second)
        span = (
            await async_db.execute(
                select(DiveProfile.duration)
                .join(DiveRecording, DiveRecording.id == DiveProfile.recording_id)
                .where(DiveRecording.dive_id == first.id, DiveProfile.parser_key == MERGE_PARSER_KEY)
            )
        ).scalar_one()
        end = datetime.fromisoformat(FIRST_START) + timedelta(milliseconds=span)

        known = dict(
            await _known(
                async_db,
                diver,
                _listing(
                    OCEAN,
                    (listed_first := uuid_pkg.uuid4(), FIRST_START),
                    (second.uuid, SECOND_START),
                    (listed_inside := uuid_pkg.uuid4(), a_minute_in),
                    (listed_end := uuid_pkg.uuid4(), end.isoformat()),
                    (listed_past := uuid_pkg.uuid4(), (end + timedelta(seconds=10)).isoformat()),
                ),
            )
        )

        assert known == {
            listed_first: first.uuid,
            second.uuid: first.uuid,
            listed_inside: first.uuid,
            listed_end: first.uuid,
        }
        assert listed_past not in known


class TestTheOriginal:
    """Whether the holding recording keeps the computer's original: true by one rule, false
    everywhere else, since a wrong true leaves the original off its dive for good."""

    @pytest.mark.asyncio
    async def test_a_recording_holding_the_computer_s_original_has_it(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        _file(db, _recording_of(db, _recorded(db, diver, OCEAN, INSTANT, 180)), LIBDIVECOMPUTER)

        assert await _originals(async_db, diver, OCEAN, "2026-10-26T15:03:23+03:00") == {
            "2026-10-26T15:03:23+03:00": True
        }

    @pytest.mark.asyncio
    async def test_an_unlabelled_bin_an_export_and_no_file_are_no_original(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        _file(db, _recording_of(db, _recorded(db, diver, OCEAN, INSTANT, 180)), IMPORT_PARSER_KEY)
        _file(db, _recording_of(db, _recorded(db, diver, OCEAN, INSTANT + timedelta(days=1), 180)), "suunto_json")
        _recorded(db, diver, OCEAN, INSTANT + timedelta(days=2), 180)

        assert await _originals(
            async_db,
            diver,
            OCEAN,
            "2026-10-26T15:03:23+03:00",
            "2026-10-27T15:03:23+03:00",
            "2026-10-28T15:03:23+03:00",
        ) == {
            "2026-10-26T15:03:23+03:00": False,
            "2026-10-27T15:03:23+03:00": False,
            "2026-10-28T15:03:23+03:00": False,
        }

    @pytest.mark.asyncio
    async def test_an_original_on_another_recording_of_the_dive_is_not_this_computer_s(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        dive = _recorded(db, diver, PERDIX, INSTANT, 180)
        other: DiveRecording = create_dive_recording(db, diver, dive, ordinal=1)
        other.device_serial, other.start_time, other.utc_offset_minutes = OCEAN, INSTANT, 180
        db.commit()
        _file(db, other, LIBDIVECOMPUTER)

        assert await _originals(async_db, diver, PERDIX, "2026-10-26T15:03:23+03:00") == {
            "2026-10-26T15:03:23+03:00": False
        }

    @pytest.mark.asyncio
    async def test_a_merge_has_the_original_of_the_record_whose_start_its_file_carries(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        """The merge holds both records' starts. Its file's fallback start is the first's, so the
        first has its original and the second does not; a listed wall clock cannot be put beside
        that instant, and a merge whose original carries no fallback start has none told."""
        diver = create_user(db)
        _file(db, _merged(db, diver, OCEAN, INSTANT, 180, 3_600_000), LIBDIVECOMPUTER, fallback_start=INSTANT)
        untold = _merged(db, diver, OCEAN, INSTANT + timedelta(days=1), 180, 3_600_000)
        _file(db, untold, LIBDIVECOMPUTER)

        assert await _originals(
            async_db,
            diver,
            OCEAN,
            "2026-10-26T15:03:24+03:00",
            "2026-10-26T15:07:06+03:00",
            "2026-10-26T15:03:23",
            "2026-10-27T15:03:23+03:00",
        ) == {
            "2026-10-26T15:03:24+03:00": True,
            "2026-10-26T15:07:06+03:00": False,
            "2026-10-26T15:03:23": False,
            "2026-10-27T15:03:23+03:00": False,
        }

    @pytest.mark.asyncio
    async def test_a_dive_held_by_its_uuid_is_told_by_its_recordings_of_the_serial(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        """Merged away, it is answered from the surviving dive's recordings; a live dive of the
        uuid with no original, or with one only from another computer, has none."""
        diver = create_user(db)
        survivor = _recorded(db, diver, OCEAN, INSTANT, 180)
        _file(db, _recording_of(db, survivor), LIBDIVECOMPUTER)
        merged_away = create_dive(db, diver)
        db.execute(update(Dive).where(Dive.id == merged_away.id).values(is_deleted=True, merged_into_id=survivor.id))
        db.commit()
        bare = _recorded(db, diver, OCEAN, INSTANT + timedelta(days=1), 180)
        elsewhere = _recorded(db, diver, PERDIX, INSTANT + timedelta(days=2), 180)
        _file(db, _recording_of(db, elsewhere), LIBDIVECOMPUTER)

        rows = await read_dive_computer_known_dives(
            request=Mock(),
            listing=_listing(
                OCEAN,
                (merged_away.uuid, "2001-01-01T00:00:00"),
                (bare.uuid, "2001-01-01T00:00:00"),
                (elsewhere.uuid, "2001-01-01T00:00:00"),
            ),
            current_user=caller(diver),
            db=async_db,
        )

        assert [(row.uuid, row.dive_uuid, row.has_original) for row in rows] == [
            (merged_away.uuid, survivor.uuid, True),
            (bare.uuid, bare.uuid, False),
            (elsewhere.uuid, elsewhere.uuid, False),
        ]


class TestTheAnswer:
    @pytest.mark.asyncio
    async def test_the_answer_is_in_the_order_listed_and_each_uuid_once(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        early = _recorded(db, diver, PERDIX, WALL_CLOCK)
        late = _recorded(db, diver, PERDIX, WALL_CLOCK + timedelta(days=1))
        one, two, missing = uuid_pkg.uuid4(), uuid_pkg.uuid4(), uuid_pkg.uuid4()

        assert await _known(
            async_db,
            diver,
            _listing(
                PERDIX,
                (two, "2026-10-27T15:03:23"),
                (missing, "2026-10-28T15:03:23"),
                (one, "2026-10-26T15:03:23"),
                (two, "2026-10-27T15:03:23"),
            ),
        ) == [(two, late.uuid), (one, early.uuid)]

    @pytest.mark.asyncio
    async def test_an_empty_list_and_an_account_holding_none_answer_an_empty_list(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        assert await _known(async_db, diver, _listing(PERDIX)) == []
        assert await _held(async_db, diver, PERDIX, "2026-10-26T15:03:23") == []

    @pytest.mark.asyncio
    async def test_it_reads_in_as_many_queries_for_one_dive_as_for_two_thousand_and_writes_nothing(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        for day in range(5):
            dive = _recorded(db, diver, PERDIX, WALL_CLOCK + timedelta(days=day))
            if day % 2:
                _file(db, _recording_of(db, dive), LIBDIVECOMPUTER)
        _recorded(db, diver, " d9772626", WALL_CLOCK + timedelta(days=9))
        one = [(uuid_pkg.uuid4(), "2026-10-26T15:03:23")]
        many = [
            (uuid_pkg.uuid4(), (WALL_CLOCK + timedelta(hours=index)).replace(tzinfo=None).isoformat())
            for index in range(MAX_LISTED_DIVES)
        ]
        statements: list[str] = []

        def record(*args: Any) -> None:
            statements.append(args[2])

        # Once before counting, so the connection's own setup is not counted.
        await _known(async_db, diver, _listing(PERDIX, *one))
        engine = async_db.bind.sync_engine  # type: ignore[union-attr]
        event.listen(engine, "before_cursor_execute", record)
        try:
            await _known(async_db, diver, _listing(PERDIX, *one))
            for_one = len(statements)
            statements.clear()
            known = await read_dive_computer_known_dives(
                request=Mock(), listing=_listing(PERDIX, *many), current_user=caller(diver), db=async_db
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert len(statements) == for_one == 3
        assert all(statement.lstrip().upper().startswith(("SELECT", "WITH")) for statement in statements)
        assert len(known) == 6
        assert sum(row.has_original for row in known) == 2


class TestWhatIsA422:
    def _body(self, **members: Any) -> str:
        return json.dumps(
            {"serial": PERDIX, "dives": [{"uuid": str(uuid_pkg.uuid4()), "started_at": "2026-10-26T15:03:23"}]}
            | members
        )

    @pytest.mark.parametrize(
        "members",
        [
            {"serial": ""},
            {"serial": "x" * 65},
            {"dives": [{"uuid": str(uuid_pkg.uuid4()), "started_at": "2026-10-26"}]},
            {"dives": [{"uuid": str(uuid_pkg.uuid4()), "started_at": "2026-10-26T15:03:23", "depth": 47.7}]},
            {"dives": [{"uuid": str(uuid_pkg.uuid4()), "started_at": "2026-10-26T15:03:23"}] * (MAX_LISTED_DIVES + 1)},
            {"fingerprint": "6adf6bbb"},
        ],
        ids=["empty serial", "long serial", "bare date", "unknown row member", "2,001 rows", "unknown member"],
    )
    def test_the_body_is_refused(self, members: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            DiveComputerListing.model_validate_json(self._body(**members))

    def test_two_thousand_rows_and_a_64_character_serial_are_taken(self) -> None:
        listing = DiveComputerListing.model_validate_json(
            self._body(
                serial="x" * 64,
                dives=[{"uuid": str(uuid_pkg.uuid4()), "started_at": "2026-10-26T15:03:23"}] * MAX_LISTED_DIVES,
            )
        )
        assert len(listing.dives) == MAX_LISTED_DIVES
