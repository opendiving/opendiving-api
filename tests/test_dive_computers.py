"""An account's dive computers in order, and the recording that order shows, against real Postgres.

The list is read off the recordings and the diver's stored order. The write is pinned against
the one equivalence that defines it: a recording added to a dive whose computer stands first
leaves the dive exactly as the same write followed by *Show by default* leaves it, through the
import and through the dive form alike, and leaves it untouched otherwise.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from src.app.api.v1 import dives as dives_module
from src.app.api.v1 import logbook_import as import_routes
from src.app.api.v1.dive_computers import read_dive_computers, write_dive_computer_order
from src.app.api.v1.dives import patch_dive_recording
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.core.worker import functions as worker_functions
from src.app.models.dive import Dive
from src.app.models.dive_recording import DiveRecording
from src.app.models.user import User
from src.app.schemas.dive import RecordingUpdateRequest
from src.app.schemas.dive_computer import DiveComputerOrder
from src.app.schemas.logbook_import import ImportDiveOutcome, ImportNoteCode
from src.app.services import blob_store
from src.app.services.dive_computers import set_order
from src.app.services.logbook_import.batch import SHOWN_BY_DEFAULT, _Dives, _File
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_recording, create_user
from tests.helpers.held_imports import apply, apply_at_once, body_of, caller, upload
from tests.test_logbook_import_batch import (
    OCEAN_FIT,
    OCEAN_JSON,
    _attach,
    _dive_ids,
    _import,
    _logbook,
    _on_the_form,
    _perdix_uddf,
)

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

PERDIX = "D9772626"
OCEAN = "253810000400"
PERDIX_UDDF = _perdix_uddf()


@pytest.fixture(autouse=True)
def volume(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_limit(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("src.app.services.cache_invalidation.invalidate_dive_caches", AsyncMock())
    monkeypatch.setattr("src.app.services.cache_invalidation.invalidate_gear_caches", AsyncMock())
    monkeypatch.setattr(dives_module, "invalidate_dive_caches", AsyncMock())
    monkeypatch.setattr(import_routes, "enforce_rate_limit", no_limit)
    for name in (
        "invalidate_dive_caches",
        "invalidate_certification_caches",
        "invalidate_contact_caches",
        "invalidate_course_caches",
        "invalidate_gear_caches",
        "invalidate_dive_site_caches",
        "invalidate_trip_caches",
    ):
        monkeypatch.setattr(import_routes, name, AsyncMock())


@pytest.fixture(autouse=True)
def worker_sessions(async_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_functions, "local_session", async_sessions)


async def _listed(db: AsyncSession, user: User) -> list[tuple[str, str | None, int]]:
    rows = await read_dive_computers(request=Mock(), current_user=caller(user), db=db)
    return [(row.serial, row.model, row.position) for row in rows]


async def _order(db: AsyncSession, user: User, *serials: str) -> list[tuple[str, str | None, int]]:
    rows = await write_dive_computer_order(
        request=Mock(), order=DiveComputerOrder(serials=list(serials)), current_user=caller(user), db=db
    )
    return [(row.serial, row.model, row.position) for row in rows]


def _recorded(
    db: Session, user: User, serial: str | None, *, model: str, created: datetime, dive_start: datetime | None = None
) -> DiveRecording:
    """A recording of a dive of its own, received at `created`."""
    dive = create_dive(db, user)
    if dive_start is not None:
        dive.start_time = dive_start
    recording: DiveRecording = create_dive_recording(db, user, dive)
    recording.device_serial, recording.device_model, recording.created_at = serial, model, created
    db.commit()
    return recording


async def _recordings(db: AsyncSession, dive_id: int) -> list[tuple[str | None, int]]:
    rows = await db.execute(
        select(DiveRecording.device_serial, DiveRecording.ordinal)
        .where(DiveRecording.dive_id == dive_id)
        .order_by(DiveRecording.ordinal)
        .execution_options(populate_existing=True)
    )
    return [(row.device_serial, row.ordinal) for row in rows]


async def _make_primary(db: AsyncSession, user: User, dive_id: int, serial: str) -> None:
    """*Show by default*, on the recording of `serial`."""
    dive = (await db.execute(select(Dive).where(Dive.id == dive_id))).scalar_one()
    rid = (
        await db.execute(
            select(DiveRecording.uuid).where(DiveRecording.dive_id == dive_id, DiveRecording.device_serial == serial)
        )
    ).scalar_one()
    await patch_dive_recording(
        request=Mock(),
        uuid=dive.uuid,
        rid=rid,
        values=RecordingUpdateRequest(primary=True),
        current_user=caller(user),
        db=db,
    )


async def _log(db: AsyncSession, user: User, uuid: Any) -> dict[str, Any]:
    """`GET /import/{uuid}/notes`."""
    return body_of(await import_routes.read_logbook_import_notes(uuid=uuid, current_user=caller(user), db=db))


T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


class TestTheList:
    @pytest.mark.asyncio
    async def test_an_account_with_no_computer_answers_an_empty_list(self, async_db: AsyncSession, db: Session) -> None:
        assert await _listed(async_db, create_user(db)) == []

    @pytest.mark.asyncio
    async def test_one_row_per_serial_in_the_order_each_was_first_received(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        """Not by the dive's date: the Ocean's recording of the older dive arrived later. A serial
        in two spellings is one computer, labelled from its latest recording, and a recording
        with no serial, or a blank one, is in no row."""
        diver = create_user(db)
        _recorded(db, diver, PERDIX, model="Perdix 3", created=T0, dive_start=T0)
        _recorded(db, diver, OCEAN, model="Ocean", created=T0 + timedelta(days=1), dive_start=T0 - timedelta(days=90))
        _recorded(db, diver, None, model="Mystery", created=T0 - timedelta(days=5))
        _recorded(db, diver, "  ", model="Blank", created=T0 - timedelta(days=5))
        _recorded(db, diver, " d9772626 ", model="Shearwater Perdix 3", created=T0 + timedelta(days=2))

        assert await _listed(async_db, diver) == [(" d9772626 ", "Shearwater Perdix 3", 0), (OCEAN, "Ocean", 1)]

    @pytest.mark.asyncio
    async def test_a_tie_on_creation_falls_to_the_recording_s_id(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        _recorded(db, diver, OCEAN, model="Ocean", created=T0)
        _recorded(db, diver, PERDIX, model="Perdix 3", created=T0)

        assert [serial for serial, _, _ in await _listed(async_db, diver)] == [OCEAN, PERDIX]

    @pytest.mark.asyncio
    async def test_a_deleted_dive_and_another_account_name_no_computer(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver, someone = create_user(db), create_user(db)
        gone = _recorded(db, diver, PERDIX, model="Perdix 3", created=T0)
        db.execute(update(Dive).where(Dive.id == gone.dive_id).values(is_deleted=True))
        db.commit()
        _recorded(db, someone, OCEAN, model="Ocean", created=T0)

        assert await _listed(async_db, diver) == []

    @pytest.mark.asyncio
    async def test_the_placed_serials_come_first_a_serial_with_no_recording_among_them(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        _recorded(db, diver, PERDIX, model="Perdix 3", created=T0)
        _recorded(db, diver, OCEAN, model="Ocean", created=T0 + timedelta(days=1))

        answered = await _order(async_db, diver, " NEW-ONE ", "253810000400")

        assert answered == [("new-one", None, 0), (OCEAN, "Ocean", 1), (PERDIX, "Perdix 3", 2)]
        assert await _listed(async_db, diver) == answered
        stored = (await async_db.execute(select(User.dive_computer_order).where(User.id == diver.id))).scalar_one()
        assert stored == ["new-one", OCEAN]

    @pytest.mark.asyncio
    async def test_the_order_is_replaced_whole(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        _recorded(db, diver, PERDIX, model="Perdix 3", created=T0)
        _recorded(db, diver, OCEAN, model="Ocean", created=T0 + timedelta(days=1))
        await _order(async_db, diver, OCEAN)

        assert [serial for serial, _, _ in await _order(async_db, diver)] == [PERDIX, OCEAN]

    @pytest.mark.asyncio
    async def test_a_serial_twice_once_folded_is_a_422_and_stores_nothing(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        await _order(async_db, diver, OCEAN)

        for serials in ([PERDIX, " d9772626"], ["   "]):
            with pytest.raises(UnprocessableEntityException):
                await _order(async_db, diver, *serials)
            await async_db.rollback()

        assert [serial for serial, _, _ in await _listed(async_db, diver)] == [OCEAN]

    def test_the_body_is_bounded_and_takes_nothing_else(self) -> None:
        for body in ({"serials": [""]}, {"serials": ["x" * 65]}, {"serials": ["a"], "other": 1}):
            with pytest.raises(ValidationError):
                DiveComputerOrder.model_validate(body)
        with pytest.raises(ValidationError):
            DiveComputerOrder.model_validate({"serials": [str(n) for n in range(101)]})


async def _ordered(db: AsyncSession, user: User, *serials: str) -> None:
    await set_order(db, user_id=user.id, serials=list(serials))
    await db.commit()


class TestTheImport:
    """The Ocean's JSON names serial 253810000400, the Perdix's UDDF D9772626, and the strict gate
    calls their two records one dive."""

    @pytest.mark.asyncio
    async def test_the_first_computer_s_recording_is_shown_as_show_by_default_would_show_it(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        ordered, pressed = create_user(db), create_user(db)
        await _ordered(async_db, ordered, PERDIX, OCEAN)
        for diver in (ordered, pressed):
            await _import(async_db, diver, [("ocean.json", OCEAN_JSON)])

        report = await _import(async_db, ordered, [("perdix.uddf", PERDIX_UDDF)])
        await _import(async_db, pressed, [("perdix.uddf", PERDIX_UDDF)])
        [dive_id] = await _dive_ids(async_db, pressed)
        assert await _recordings(async_db, dive_id) == [(OCEAN, 0), (PERDIX, 1)], "no order, the first received"
        await _make_primary(async_db, pressed, dive_id, PERDIX)

        assert await _logbook(async_db, ordered) == await _logbook(async_db, pressed)
        [dive_id] = await _dive_ids(async_db, ordered)
        assert await _recordings(async_db, dive_id) == [(PERDIX, 0), (OCEAN, 1)]
        [row] = report.dives
        assert row.outcome is ImportDiveOutcome.MODIFIED
        shown = [note for note in row.notes if note.code == ImportNoteCode.RECORDING_SHOWN_BY_DEFAULT.value]
        assert [note.message for note in shown] == [SHOWN_BY_DEFAULT]

    @pytest.mark.asyncio
    async def test_a_computer_placed_lower_leaves_the_dive_as_it_was(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        await _ordered(async_db, diver, OCEAN, PERDIX)
        await _import(async_db, diver, [("ocean.json", OCEAN_JSON)])

        report = await _import(async_db, diver, [("perdix.uddf", PERDIX_UDDF)])

        [dive_id] = await _dive_ids(async_db, diver)
        assert await _recordings(async_db, dive_id) == [(OCEAN, 0), (PERDIX, 1)]
        assert ImportNoteCode.RECORDING_SHOWN_BY_DEFAULT.value not in {note.code for note in report.dives[0].notes}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("order", [(PERDIX, OCEAN), (OCEAN, PERDIX)])
    async def test_in_the_other_arrival_order_the_order_decides_too(
        self, async_db: AsyncSession, db: Session, order: tuple[str, str]
    ) -> None:
        diver = create_user(db)
        await _ordered(async_db, diver, *order)
        await _import(async_db, diver, [("perdix.uddf", PERDIX_UDDF)])

        await _import(async_db, diver, [("ocean.json", OCEAN_JSON)])

        [dive_id] = await _dive_ids(async_db, diver)
        assert [serial for serial, _ in await _recordings(async_db, dive_id)] == list(order)

    @pytest.mark.asyncio
    async def test_a_serial_placed_before_any_recording_of_it_keeps_its_place(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        """The phone's case: the new computer placed first as soon as it names itself, its first
        download arriving after."""
        diver = create_user(db)
        await _import(async_db, diver, [("ocean.json", OCEAN_JSON)])
        await _ordered(async_db, diver, "d9772626")

        await _import(async_db, diver, [("perdix.uddf", PERDIX_UDDF)])

        [dive_id] = await _dive_ids(async_db, diver)
        assert await _recordings(async_db, dive_id) == [(PERDIX, 0), (OCEAN, 1)]

    @pytest.mark.asyncio
    async def test_unplaced_the_computer_received_first_comes_first(self, async_db: AsyncSession, db: Session) -> None:
        """The Ocean recorded an earlier dive, so it stands before the Perdix, which made this one."""
        diver = create_user(db)
        _recorded(db, diver, OCEAN, model="Ocean", created=T0 - timedelta(days=30))
        [row] = (await _import(async_db, diver, [("perdix.uddf", PERDIX_UDDF)])).dives

        await _import(async_db, diver, [("ocean.json", OCEAN_JSON)])

        dive_id = (await async_db.execute(select(Dive.id).where(Dive.uuid == row.uuid))).scalar_one()
        assert await _recordings(async_db, dive_id) == [(OCEAN, 0), (PERDIX, 1)]

    @pytest.mark.asyncio
    async def test_a_recording_with_no_serial_is_never_moved(self, async_db: AsyncSession, db: Session) -> None:
        """The Ocean's FIT names no serial."""
        diver = create_user(db)
        await _ordered(async_db, diver, OCEAN, PERDIX)
        await _import(async_db, diver, [("perdix.uddf", PERDIX_UDDF)])

        await _import(async_db, diver, [("ocean.fit", OCEAN_FIT)])

        [dive_id] = await _dive_ids(async_db, diver)
        assert await _recordings(async_db, dive_id) == [(PERDIX, 0), (None, 1)]

    @pytest.mark.asyncio
    async def test_the_diver_s_own_choice_survives_a_later_file(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        await _ordered(async_db, diver, PERDIX, OCEAN)
        await _import(async_db, diver, [("ocean.json", OCEAN_JSON)])
        await _import(async_db, diver, [("perdix.uddf", PERDIX_UDDF)])
        [dive_id] = await _dive_ids(async_db, diver)
        await _make_primary(async_db, diver, dive_id, OCEAN)

        await _import(async_db, diver, [("ocean.fit", OCEAN_FIT)])

        assert await _recordings(async_db, dive_id) == [(OCEAN, 0), (PERDIX, 1)]

    @pytest.mark.asyncio
    async def test_changing_the_order_moves_no_stored_dive(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        await _import(async_db, diver, [("ocean.json", OCEAN_JSON)])
        await _import(async_db, diver, [("perdix.uddf", PERDIX_UDDF)])
        before = await _logbook(async_db, diver)

        await _order(async_db, diver, PERDIX, OCEAN)

        assert await _logbook(async_db, diver) == before

    @pytest.mark.asyncio
    async def test_a_dive_the_batch_creates_keeps_its_note_to_itself(self, async_db: AsyncSession, db: Session) -> None:
        """Within one import the Perdix's logbook, read first, creates the dive and the Ocean's
        file joins it first; the row is new, and which of its recordings it shows is part of it."""
        diver = create_user(db)
        await _ordered(async_db, diver, OCEAN, PERDIX)

        report = await _import(async_db, diver, [("ocean.json", OCEAN_JSON), ("perdix.uddf", PERDIX_UDDF)])

        [dive_id] = await _dive_ids(async_db, diver)
        [row] = report.dives
        assert row.outcome is ImportDiveOutcome.NEW
        assert await _recordings(async_db, dive_id) == [(OCEAN, 0), (PERDIX, 1)]
        codes = {note.code for note in row.notes} | {group.code for group in report.notes}
        assert ImportNoteCode.RECORDING_SHOWN_BY_DEFAULT.value not in codes


class TestTheReview:
    @pytest.mark.asyncio
    async def test_a_recording_shown_by_default_is_one_change_naming_it(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        await _ordered(async_db, diver, PERDIX, OCEAN)
        await _import(async_db, diver, [("ocean.json", OCEAN_JSON)])

        report = await _import(async_db, diver, [("perdix.uddf", PERDIX_UDDF)], apply=False)

        [row] = report.dives
        recordings = [change for change in row.changes if change["member"].startswith("recordings/")]
        assert [change["member"] for change in recordings] == ["recordings/0"]
        assert recordings[0]["before"] is None and recordings[0]["after"]["device"]["serial"] == PERDIX
        assert row.dive is not None and [one["ordinal"] for one in row.dive["recordings"]] == [0, 1]

    @pytest.mark.asyncio
    async def test_the_apply_holds_to_it(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        await _ordered(async_db, diver, PERDIX, OCEAN)
        await _import(async_db, diver, [("ocean.json", OCEAN_JSON)])

        review = await upload(async_db, diver, [("perdix.uddf", PERDIX_UDDF)])
        applied = await apply(async_db, diver, review["uuid"])

        [row] = applied["dives"]
        assert [change["member"] for change in row["changes"] if change["member"].startswith("recordings/")] == [
            "recordings/0"
        ]
        [logged] = (await _log(async_db, diver, applied["uuid"]))["dives"]
        codes = [note["code"] for note in logged["notes"]]
        assert ImportNoteCode.RECORDING_SHOWN_BY_DEFAULT.value in codes
        assert ImportNoteCode.REVIEW_NOT_HELD.value not in codes
        [dive_id] = await _dive_ids(async_db, diver)
        assert await _recordings(async_db, dive_id) == [(PERDIX, 0), (OCEAN, 1)]

    @pytest.mark.asyncio
    async def test_an_import_applied_at_once_says_so(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        await _ordered(async_db, diver, PERDIX, OCEAN)
        await _import(async_db, diver, [("ocean.json", OCEAN_JSON)])

        applied = await apply_at_once(async_db, diver, [("perdix.uddf", PERDIX_UDDF)])

        [row] = applied["dives"]
        assert row["outcome"] == "modified"
        [logged] = (await _log(async_db, diver, applied["uuid"]))["dives"]
        assert [note["message"] for note in logged["notes"] if note["code"] == "recording_shown_by_default"] == [
            SHOWN_BY_DEFAULT
        ]


class TestTheForm:
    @pytest.mark.asyncio
    async def test_the_form_s_attach_is_the_same_write(self, async_db: AsyncSession, db: Session) -> None:
        ordered, pressed = create_user(db), create_user(db)
        await _ordered(async_db, ordered, PERDIX, OCEAN)
        dives = {}
        for diver in (ordered, pressed):
            dives[diver.id] = await _on_the_form(
                async_db, db, diver, [("ocean.json", OCEAN_JSON), ("perdix.uddf", PERDIX_UDDF)]
            )
        assert await _recordings(async_db, dives[pressed.id].id) == [(OCEAN, 0), (PERDIX, 1)]
        await _make_primary(async_db, pressed, dives[pressed.id].id, PERDIX)

        assert await _recordings(async_db, dives[ordered.id].id) == [(PERDIX, 0), (OCEAN, 1)]
        assert await _logbook(async_db, ordered) == await _logbook(async_db, pressed)

    @pytest.mark.asyncio
    async def test_a_second_file_of_the_shown_recording_moves_nothing(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        """A file joining a recording that exists is no recording added."""
        diver = create_user(db)
        await _ordered(async_db, diver, OCEAN, PERDIX)
        dive = await _on_the_form(async_db, db, diver, [("perdix.uddf", PERDIX_UDDF), ("ocean.fit", OCEAN_FIT)])
        assert await _recordings(async_db, dive.id) == [(PERDIX, 0), (None, 1)]

        await _attach(async_db, diver, dive, OCEAN_JSON, "ocean.json")

        assert await _recordings(async_db, dive.id) == [(PERDIX, 0), (OCEAN, 1)]


class TestNotesFollowTheRecording:
    """A later file's attach that moves a recording to the front moves what an earlier file of the
    batch placed on that dive, so a note about a recording's member still reads that recording."""

    def _file(self, placed: dict[uuid_pkg.UUID, dict[int, tuple[int, int]]]) -> _File:
        return _File(
            loaded=Mock(),
            plan=Mock(),
            findings=[],
            notes_about={},
            found_about={},
            indexes={},
            inferred=frozenset(),
            recordings=placed,
        )

    def test_each_recording_ahead_of_the_moved_one_is_one_place_on(self) -> None:
        first, second = uuid_pkg.uuid4(), uuid_pkg.uuid4()
        dives = _Dives()
        dives._files[0] = self._file({first: {0: (7, 0), 1: (8, 0)}, second: {0: (7, 2)}})
        dives._files[1] = self._file({second: {0: (7, 1)}})

        dives._moved_to_front(7, 2)

        assert dives._files[0].recordings == {first: {0: (7, 1), 1: (8, 0)}, second: {0: (7, 2)}}
        assert dives._files[1].recordings == {second: {0: (7, 2)}}
