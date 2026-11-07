"""An import the server holds: `/imports` and `/import/{uuid}`, against real Postgres and a real
volume. What the files make of each other is `test_logbook_import_batch.py`; this is what is held,
for how long, and what an apply does with it.
"""

import hashlib
import io
import json
import uuid as uuid_pkg
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from src.app.api.v1 import logbook_import as import_routes
from src.app.core.config import Settings, settings
from src.app.core.worker import functions as worker_functions
from src.app.core.worker.functions import purge_expired_logbook_imports
from src.app.crud.crud_dive_mixtures import replace_mixtures_for_dive
from src.app.models.dive import Dive
from src.app.models.dive_file import DiveFile
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_recording import DiveRecording
from src.app.models.logbook_import import LogbookImport, LogbookImportFile
from src.app.models.user import User
from src.app.schemas.dive import DiveReadInternal
from src.app.schemas.dive_mixture import DiveMixtureCreate
from src.app.schemas.logbook_import import (
    ImportDiveOutcome,
    ImportNoteCode,
    LogbookImportNotes,
    LogbookImportRead,
    LogbookImportStatus,
)
from src.app.services import blob_store
from src.app.services.dive_merge import merge_dives
from src.app.services.logbook_import.planner import DELETED_HERE
from tests.conftest import db_available
from tests.helpers.generators import create_user
from tests.helpers.held_imports import apply, body_of, caller, discard, upload, upload_and_apply

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

FIXTURES = Path(__file__).parent / "fixtures" / "dive_files"
OCEAN_FIT = (FIXTURES / "suunto-ocean-2026.fit").read_bytes()
OCEAN_JSON = (FIXTURES / "suunto-ocean-2026.json").read_bytes()
UNREADABLE = b"date,depth\n2026-04-17,28.4\n"


def _export(start: str, *, device: str = "Ocean") -> bytes:
    """A Suunto app export of the fixture's dive at another moment: a dive no other test holds."""
    document = json.loads(OCEAN_JSON)
    document["DeviceLog"]["Header"]["DateTime"] = start
    document["DeviceLog"]["Device"]["Name"] = device
    return json.dumps(document).encode()


def _logbook(*dives: dict[str, Any], uuid: Any = None) -> bytes:
    """A DiveJSON document of `dives`, each with a fresh uuid unless it says otherwise."""
    records = [
        {
            "uuid": str(uuid or uuid_pkg.uuid4()),
            "started_at": "2026-05-01T10:00:00+02:00",
            "submerged_time": 1800,
            **dive,
        }
        for dive in dives
    ]
    return json.dumps(
        {"format": "divejson", "version": "1.0", "exported_at": "2026-05-02T10:00:00+02:00", "dives": records}
    ).encode()


def _zip_of(*files: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files:
            archive.writestr(name, data)
    return buffer.getvalue()


async def _rows(db: AsyncSession, user: User) -> list[LogbookImport]:
    found = await db.execute(
        select(LogbookImport)
        .where(LogbookImport.user_id == user.id)
        .order_by(LogbookImport.id)
        .execution_options(populate_existing=True)
    )
    return list(found.scalars())


async def _files(db: AsyncSession, user: User) -> list[LogbookImportFile]:
    found = await db.execute(
        select(LogbookImportFile)
        .join(LogbookImport, LogbookImport.id == LogbookImportFile.logbook_import_id)
        .where(LogbookImport.user_id == user.id)
        .order_by(LogbookImportFile.id)
    )
    return list(found.scalars())


async def _age(db: AsyncSession, user: User, *, by: timedelta = timedelta(days=1)) -> None:
    """Move the account's imports' expiries `by` into the past."""
    await db.execute(
        update(LogbookImport)
        .where(LogbookImport.user_id == user.id)
        .values(expires_at=datetime.now(UTC) - by, created_at=datetime.now(UTC) - by)
    )
    await db.commit()


async def _pending(db: AsyncSession, user: User) -> dict[str, Any]:
    return body_of(await import_routes.read_pending_logbook_import(current_user=caller(user), db=db))


async def _history(db: AsyncSession, user: User) -> dict[str, Any]:
    return await import_routes.read_logbook_imports(current_user=caller(user), db=db)


async def _notes(db: AsyncSession, user: User, uuid: Any) -> dict[str, Any]:
    return body_of(await import_routes.read_logbook_import_notes(uuid=uuid, current_user=caller(user), db=db))


async def _refusal(call: Any) -> HTTPException:
    with pytest.raises(HTTPException) as refused:
        await call
    return refused.value


def _rows_by_outcome(body: dict[str, Any], outcome: ImportDiveOutcome) -> list[dict[str, Any]]:
    return [row for row in body["dives"] if row["outcome"] == outcome.value]


@pytest.fixture(autouse=True)
def volume(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def worker_sessions(async_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_functions, "local_session", async_sessions)


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_limit(*args: Any, **kwargs: Any) -> None:
        return None

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


class TestTheUpload:
    @pytest.mark.asyncio
    async def test_it_answers_the_review_and_holds_the_readable_files_alone(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)

        body = await upload(async_db, diver, [("dive.fit", OCEAN_FIT), ("notes.csv", UNREADABLE)])

        assert body["status"] == "pending" and body["applied_at"] is None
        assert [row["outcome"] for row in body["dives"]] == ["new"]
        refused = next(member for member in body["members"] if member["name"] == "notes.csv")
        assert refused["refusal"] and refused["byte_size"] == len(UNREADABLE)
        assert refused["sha256"] == hashlib.sha256(UNREADABLE).hexdigest()
        [file] = await _files(async_db, diver)
        assert (file.role, file.filename, file.sha256) == ("part", "dive.fit", hashlib.sha256(OCEAN_FIT).hexdigest())
        assert await blob_store.get(file.storage_key) == OCEAN_FIT
        assert await async_db.scalar(select(func.count()).select_from(Dive).where(Dive.user_id == diver.id)) == 0
        LogbookImportRead.model_validate(body)

    @pytest.mark.asyncio
    async def test_a_plain_zip_holds_its_readable_members_and_not_itself(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        fit, unreadable = "export/dive.fit", "export/readme.csv"

        body = await upload(async_db, diver, [("export.zip", _zip_of((fit, OCEAN_FIT), (unreadable, UNREADABLE)))])

        [file] = await _files(async_db, diver)
        assert (file.filename, file.sha256) == (fit, hashlib.sha256(OCEAN_FIT).hexdigest())
        assert await blob_store.get(file.storage_key) == OCEAN_FIT
        names = {member["name"]: member for member in body["members"]}
        assert names["export.zip"]["format"] == "zip" and names[unreadable]["refusal"]

        applied = await apply(async_db, diver, body["uuid"])

        assert [row["uuid"] for row in applied["dives"]] == [row["uuid"] for row in body["dives"]]
        assert [row["outcome"] for row in applied["dives"]] == ["new"]
        kept = await async_db.scalar(select(func.count()).select_from(DiveFile).where(DiveFile.user_id == diver.id))
        assert kept == 1

    @pytest.mark.asyncio
    async def test_a_second_upload_is_refused_naming_the_pending_one(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        first = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])

        refused = await _refusal(upload(async_db, diver, [("dive.json", OCEAN_JSON)]))

        assert refused.status_code == 409 and first["uuid"] in str(refused.detail)
        assert len(await _rows(async_db, diver)) == 1

    @pytest.mark.asyncio
    async def test_the_loser_of_a_race_for_the_slot_is_refused_after_the_conversion(
        self, async_db: AsyncSession, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both passed the check before the body, which is the race: the index settles it."""
        diver = create_user(db)
        first = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        monkeypatch.setattr(import_routes, "refuse_while_pending", AsyncMock())

        refused = await _refusal(upload(async_db, diver, [("dive.json", OCEAN_JSON)]))

        assert refused.status_code == 409 and first["uuid"] in str(refused.detail)
        assert len(await _rows(async_db, diver)) == 1 and len(await _files(async_db, diver)) == 1

    @pytest.mark.asyncio
    async def test_any_session_of_the_account_reads_the_same_review(
        self, async_db: AsyncSession, async_sessions: async_sessionmaker[AsyncSession], db: Session
    ) -> None:
        diver = create_user(db)
        body = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])

        async with async_sessions() as another:
            assert await _pending(another, diver) == body
            log = await _notes(another, diver, body["uuid"])

        LogbookImportNotes.model_validate(log)
        assert [row["uuid"] for row in log["dives"]] == [row["uuid"] for row in body["dives"]]

    @pytest.mark.asyncio
    async def test_someone_else_s_import_is_absent(self, async_db: AsyncSession, db: Session) -> None:
        diver, stranger = create_user(db), create_user(db)
        body = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])

        refused = await _refusal(
            import_routes.read_logbook_import(uuid=body["uuid"], current_user=caller(stranger), db=async_db)
        )

        assert refused.status_code == 404
        assert (await _refusal(_pending(async_db, stranger))).status_code == 404

    @pytest.mark.asyncio
    async def test_a_stored_review_is_served_as_it_was_stored(self, async_db: AsyncSession, db: Session) -> None:
        """An earlier build's row answers: nothing validates it against today's schemas."""
        diver = create_user(db)
        body = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        [row] = await _rows(async_db, diver)
        row.review = {**row.review, "dives": [{"from_an_earlier_build": True}]}
        await async_db.commit()

        assert (await _pending(async_db, diver))["dives"] == [{"from_an_earlier_build": True}]
        assert body["dives"] != [{"from_an_earlier_build": True}]

    @pytest.mark.asyncio
    async def test_the_numbering_is_the_logbook_s_before_the_import(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)

        body = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])

        assert body["numbering"]["total_dives"] == 0


class TestTheLifetimes:
    @pytest.mark.asyncio
    async def test_a_pending_import_past_its_lifetime_is_gone_and_blocks_nothing(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        old = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        [held] = await _files(async_db, diver)
        await _age(async_db, diver)

        assert (await _refusal(_pending(async_db, diver))).status_code == 404
        assert (await _refusal(apply(async_db, diver, old["uuid"]))).status_code == 404

        fresh = await upload(async_db, diver, [("dive.json", OCEAN_JSON)])

        assert fresh["uuid"] != old["uuid"]
        assert not await blob_store.has(held.storage_key)
        assert [row.uuid for row in await _rows(async_db, diver)] == [uuid_pkg.UUID(fresh["uuid"])]

    @pytest.mark.asyncio
    async def test_the_cron_deletes_what_has_expired_with_its_files(self, async_db: AsyncSession, db: Session) -> None:
        diver, other = create_user(db), create_user(db)
        await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        await upload(async_db, other, [("dive.fit", OCEAN_FIT)])
        [held], [kept] = await _files(async_db, diver), await _files(async_db, other)
        await _age(async_db, diver)

        await purge_expired_logbook_imports({})

        assert await _rows(async_db, diver) == [] and await _files(async_db, diver) == []
        assert not await blob_store.has(held.storage_key)
        assert await blob_store.has(kept.storage_key)

    @pytest.mark.asyncio
    async def test_a_blank_lifetime_keeps_a_pending_import(
        self, async_db: AsyncSession, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "IMPORT_PENDING_LIFETIME_HOURS", None)
        diver = create_user(db)

        body = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        await async_db.execute(
            update(LogbookImport)
            .where(LogbookImport.user_id == diver.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=2))
        )
        await async_db.commit()
        await purge_expired_logbook_imports({})

        assert body["expires_at"] is None
        assert (await _pending(async_db, diver))["uuid"] == body["uuid"]

    @pytest.mark.asyncio
    async def test_the_lifetime_sets_the_expiry(
        self, async_db: AsyncSession, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "IMPORT_PENDING_LIFETIME_HOURS", 3)
        monkeypatch.setattr(settings, "IMPORT_HISTORY_RETENTION_DAYS", 2)
        diver = create_user(db)

        pending = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        applied = await apply(async_db, diver, pending["uuid"])

        def moment(value: str) -> datetime:
            return datetime.fromisoformat(value)

        assert moment(pending["expires_at"]) - moment(pending["created_at"]) == timedelta(hours=3)
        assert moment(applied["expires_at"]) - moment(applied["applied_at"]) == timedelta(days=2)

    @pytest.mark.parametrize("name", ["IMPORT_PENDING_LIFETIME_HOURS", "IMPORT_HISTORY_RETENTION_DAYS"])
    def test_a_lifetime_of_zero_refuses_startup_naming_the_setting(self, name: str) -> None:
        with pytest.raises(ValidationError, match=name):
            Settings(**{name: 0})

    @pytest.mark.parametrize("name", ["IMPORT_PENDING_LIFETIME_HOURS", "IMPORT_HISTORY_RETENTION_DAYS"])
    def test_a_blank_lifetime_reads_as_no_limit(self, name: str) -> None:
        assert getattr(Settings(**{name: ""}), name) is None


class TestTheApply:
    @pytest.mark.asyncio
    async def test_it_writes_what_the_review_showed_and_lets_the_upload_go(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        review = await upload(async_db, diver, [("dive.fit", OCEAN_FIT), ("dive.json", OCEAN_JSON)])
        held = await _files(async_db, diver)

        applied = await apply(async_db, diver, review["uuid"])

        assert applied["status"] == "applied" and applied["applied_at"]
        [row] = applied["dives"]
        assert (row["outcome"], row["uuid"]) == ("new", review["dives"][0]["uuid"])
        assert row["dive"] == review["dives"][0]["dive"]
        stored = await async_db.scalar(select(Dive).where(Dive.user_id == diver.id))
        assert stored is not None and str(stored.uuid) == row["uuid"]
        assert await _files(async_db, diver) == []
        assert not any([await blob_store.has(file.storage_key) for file in held])
        kept = await async_db.scalars(select(DiveFile.storage_key).where(DiveFile.user_id == diver.id))
        assert all([await blob_store.has(key) for key in kept.all()])
        assert (await async_db.scalar(select(func.count()).select_from(DiveFile).where(DiveFile.user_id == diver.id))) == 2
        LogbookImportRead.model_validate(applied)

    @pytest.mark.asyncio
    async def test_applying_twice_is_a_conflict(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        review = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        await apply(async_db, diver, review["uuid"])

        assert (await _refusal(apply(async_db, diver, review["uuid"]))).status_code == 409

    @pytest.mark.asyncio
    async def test_the_caches_are_dropped_after_the_commit(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)

        await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])

        import_routes.invalidate_dive_caches.assert_awaited_once_with(diver.id)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_the_upload_leaves_the_slot_free_once_applied(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])

        again = await upload(async_db, diver, [("dive.json", OCEAN_JSON)])

        assert again["status"] == "pending"
        assert (await _pending(async_db, diver))["uuid"] == again["uuid"]


class TestDiscardingAndTheHistory:
    @pytest.mark.asyncio
    async def test_a_discard_takes_the_import_and_its_files(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        review = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        [held] = await _files(async_db, diver)

        await discard(async_db, diver, review["uuid"], LogbookImportStatus.PENDING)

        assert await _rows(async_db, diver) == []
        assert not await blob_store.has(held.storage_key)

    @pytest.mark.asyncio
    async def test_a_discard_sent_from_a_review_another_device_applied_deletes_nothing(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        applied = await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])

        refused = await _refusal(discard(async_db, diver, applied["uuid"], LogbookImportStatus.PENDING))

        assert refused.status_code == 409
        assert [row.status for row in await _rows(async_db, diver)] == ["applied"]

    @pytest.mark.asyncio
    async def test_the_history_lists_applied_imports_newest_first(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        first = await upload_and_apply(async_db, diver, [("one.json", _logbook({}))])
        second = await upload_and_apply(async_db, diver, [("two.json", _logbook({}, {"started_at": "2026-05-02T10:00:00+02:00"}))])
        await upload(async_db, diver, [("three.json", _logbook({"started_at": "2026-05-03T10:00:00+02:00"}))])

        history = await _history(async_db, diver)

        assert [str(row["uuid"]) for row in history["data"]] == [second["uuid"], first["uuid"]]
        assert history["total_count"] == 2
        assert history["data"][0]["counts"] == {"new": 2}

    @pytest.mark.asyncio
    async def test_an_expired_applied_import_is_neither_listed_nor_read(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        applied = await upload_and_apply(async_db, diver, [("one.json", _logbook({}))])
        await _age(async_db, diver)

        assert (await _history(async_db, diver))["data"] == []
        refused = await _refusal(
            import_routes.read_logbook_import(uuid=applied["uuid"], current_user=caller(diver), db=async_db)
        )
        assert refused.status_code == 404

    @pytest.mark.asyncio
    async def test_deleting_an_applied_import_leaves_its_dives(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        applied = await upload_and_apply(async_db, diver, [("one.json", _logbook({}))])

        await discard(async_db, diver, applied["uuid"], LogbookImportStatus.APPLIED)

        assert (await _history(async_db, diver))["data"] == []
        assert await async_db.scalar(select(func.count()).select_from(Dive).where(Dive.user_id == diver.id)) == 1

    @pytest.mark.asyncio
    async def test_a_blank_retention_keeps_an_applied_import(
        self, async_db: AsyncSession, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "IMPORT_HISTORY_RETENTION_DAYS", None)
        diver = create_user(db)

        applied = await upload_and_apply(async_db, diver, [("one.json", _logbook({}))])
        await purge_expired_logbook_imports({})

        assert applied["expires_at"] is None
        assert len((await _history(async_db, diver))["data"]) == 1


class TestAppliedAsReviewed:
    """Decision of the review: what the diver approved persists, even where the rows moved."""

    @pytest.mark.asyncio
    async def test_an_edit_made_since_the_review_gives_way_to_the_reviewed_value(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])
        review = await upload(async_db, diver, [("dive.json", OCEAN_JSON)])
        [row] = review["dives"]
        assert row["outcome"] == "modified"
        values = [
            change
            for change in row["changes"]
            if change["member"].startswith("mixtures/") and change["after"] is not None
        ]
        assert values, "the fixture's JSON should fill a cylinder member the FIT left blank"
        dive = await async_db.scalar(select(Dive).where(Dive.user_id == diver.id))
        assert dive is not None
        # An edit through the form replaces the list row by row, and moves one listed member.
        mixtures = (
            await async_db.scalars(select(DiveMixture).where(DiveMixture.dive_id == dive.id).order_by(DiveMixture.id))
        ).all()
        edited = values[0]["member"].split("/")
        await replace_mixtures_for_dive(
            db=async_db,
            dive_id=dive.id,
            mixtures=[
                DiveMixtureCreate(
                    **{
                        name: getattr(mixture, name)
                        for name in ("oxygen", "helium", "volume", "start_pressure", "end_pressure", "gas_number")
                    }
                )
                for mixture in mixtures
            ],
        )
        await async_db.execute(update(Dive).where(Dive.id == dive.id).values(max_depth=11.5))
        await async_db.commit()

        applied = await apply(async_db, diver, review["uuid"])

        assert [one["outcome"] for one in applied["dives"]] == ["modified"]
        stored = (
            await async_db.scalars(
                select(DiveMixture)
                .where(DiveMixture.dive_id == dive.id)
                .order_by(DiveMixture.id)
                .execution_options(populate_existing=True)
            )
        ).all()
        for change in values:
            _, index, member = change["member"].split("/")
            assert getattr(stored[int(index)], member) == change["after"]
        assert edited
        await async_db.refresh(dive)
        assert dive.max_depth == 11.5
