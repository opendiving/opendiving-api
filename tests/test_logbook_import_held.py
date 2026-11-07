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
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from src.app.api.v1 import logbook_import as import_routes
from src.app.core.config import Settings, settings
from src.app.core.exceptions.http_exceptions import RateLimitException
from src.app.core.worker import functions as worker_functions
from src.app.core.worker.functions import purge_expired_logbook_imports
from src.app.crud.crud_dive_mixtures import replace_mixtures_for_dive
from src.app.crud.crud_dive_sites import replace_site_with_record
from src.app.crud.crud_dives import crud_dives
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_file import DiveFile
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_recording import DiveRecording
from src.app.models.dive_site import DiveSite
from src.app.models.logbook_import import LogbookImport, LogbookImportFile
from src.app.models.user import User
from src.app.schemas.dive import DiveReadInternal
from src.app.schemas.dive_mixture import DiveMixtureCreate
from src.app.schemas.logbook_import import (
    REVIEW_SHAPE,
    ImportDiveOutcome,
    ImportNoteCode,
    LogbookImportNotes,
    LogbookImportRead,
    LogbookImportStatus,
)
from src.app.services import blob_store
from src.app.services.dive_merge import merge_dives
from src.app.services.dive_recordings import DEVICE_COLUMNS
from src.app.services.export.archive import DIVEJSON_NAME
from src.app.services.logbook_import import import_batch, load_import
from src.app.services.logbook_import.planner import DELETED_HERE
from src.app.services.logbook_import.reconcile import hold_reviewed_values
from tests.conftest import db_available
from tests.helpers.generators import (
    create_commons_site,
    create_dive,
    create_dive_recording,
    create_dive_site,
    create_user,
)
from tests.helpers.held_imports import apply, body_of, caller, discard, upload, upload_and_apply
from tests.helpers.images import phone_jpeg
from tests.helpers.import_parts import parts_of

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

# The digest `_shape_digest` answers at `REVIEW_SHAPE`. It moves with the number, on purpose.
EMBEDDED_SHAPE = "bcf091dbec136ea5201ece5155bbf2505c21bc744f9187d2499ffb141fe79bf9"

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


def _recording(serial: str, start: str) -> dict[str, Any]:
    """One computer's record of half an hour, in a logbook's terms."""
    return {
        "device": {"model": "Perdix 3", "serial": serial},
        "started_at": start,
        "profile": {"depth": {"times": [0, 60_000, 1_740_000], "values": [0, 1800, 0]}},
    }


def _settings_with(name: str, value: Any) -> Settings:
    overrides: dict[str, Any] = {name: value}
    return Settings(**overrides)


def _zip_of(*files: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files:
            archive.writestr(name, data)
    return buffer.getvalue()


def _archive(**document: Any) -> bytes:
    """A full-export archive of one dive of its own."""
    logbook = json.loads(_logbook({"started_at": "2026-05-05T10:00:00+02:00"}))
    return _zip_of((DIVEJSON_NAME, json.dumps({**logbook, **document}).encode()))


def _archive_with_a_portrait() -> bytes:
    picture = phone_jpeg(size=(320, 240), orientation=None)
    stored = {
        "uuid": str(uuid_pkg.uuid4()),
        "original_filename": "portrait.jpg",
        "content_type": "image/jpeg",
        "byte_size": len(picture),
        "sha256": hashlib.sha256(picture).hexdigest(),
        "archive_path": "files/portrait.jpg",
    }
    document = json.dumps({"format": "divejson", "version": "1.0", "diver": {"portrait_file": stored}}).encode()
    return _zip_of((DIVEJSON_NAME, document), ("files/portrait.jpg", picture))


async def _internal(db: AsyncSession, dive_id: int) -> DiveReadInternal:
    row = await crud_dives.get(db=db, id=dive_id, schema_to_select=DiveReadInternal)
    return DiveReadInternal.model_validate(row)


def _without_docs(node: Any) -> Any:
    """A schema with the words the published docs carry taken out, whatever a property is called."""
    if isinstance(node, dict):
        return {
            key: value if key == "properties" and not isinstance(value, dict) else _without_doc_members(key, value)
            for key, value in node.items()
            if key not in {"title", "description", "examples"}
        }
    if isinstance(node, list):
        return [_without_docs(value) for value in node]
    return node


def _without_doc_members(key: str, value: Any) -> Any:
    if key == "properties":
        return {name: _without_docs(member) for name, member in value.items()}
    return _without_docs(value)


def _shape_digest() -> str:
    """Every model `review` and `notes` embed, by what they hold and not how they are described."""
    shapes = [_without_docs(model.model_json_schema()) for model in (LogbookImportRead, LogbookImportNotes)]
    return hashlib.sha256(json.dumps(shapes, sort_keys=True).encode()).hexdigest()


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
        assert len(await _rows(async_db, diver)) == 1
        [held] = await _files(async_db, diver)
        assert list(blob_store.iter_keys()) == [held.storage_key], "the loser takes back what it wrote"

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


class TestTheLog:
    @staticmethod
    def _with_a_dive_it_cannot_write(document: bytes) -> bytes:
        logbook = json.loads(document)
        logbook["dives"].append({"uuid": str(uuid_pkg.uuid4()), "max_depth": 12.5})
        return json.dumps(logbook).encode()

    @pytest.mark.asyncio
    async def test_a_pending_import_s_rows_say_what_the_review_says(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        document = self._with_a_dive_it_cannot_write(_logbook({}))

        review = await upload(async_db, diver, [("log.divejson", document)])
        log = await _notes(async_db, diver, review["uuid"])

        LogbookImportNotes.model_validate(log)
        rows = [(row["uuid"], row["outcome"], row["reason"]) for row in log["dives"]]
        assert rows == [(row["uuid"], row["outcome"], row["reason"]) for row in review["dives"]]
        assert sorted(row["outcome"] for row in log["dives"]) == ["new", "skipped"]
        [skipped] = [row for row in log["dives"] if row["outcome"] == "skipped"]
        assert "start time" in skipped["reason"]

    @pytest.mark.asyncio
    async def test_an_applied_import_s_rows_say_what_the_apply_settled(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])
        review = await upload(async_db, diver, [("dive.json", OCEAN_JSON)])
        [pending] = (await _notes(async_db, diver, review["uuid"]))["dives"]
        assert (pending["outcome"], pending["reason"]) == ("modified", None)
        await async_db.execute(update(Dive).where(Dive.user_id == diver.id).values(is_deleted=True))
        await async_db.commit()

        await apply(async_db, diver, review["uuid"])
        log = await _notes(async_db, diver, review["uuid"])

        LogbookImportNotes.model_validate(log)
        [row] = log["dives"]
        assert (row["uuid"], row["outcome"], row["reason"]) == (pending["uuid"], "skipped", DELETED_HERE)


class TestTheLifetimes:
    @pytest.mark.asyncio
    async def test_a_pending_import_past_its_lifetime_is_gone_and_blocks_nothing(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        old = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        [held] = [file.storage_key for file in await _files(async_db, diver)]
        await _age(async_db, diver)

        assert (await _refusal(_pending(async_db, diver))).status_code == 404
        assert (await _refusal(apply(async_db, diver, old["uuid"]))).status_code == 404

        fresh = await upload(async_db, diver, [("dive.json", OCEAN_JSON)])

        assert fresh["uuid"] != old["uuid"]
        assert not await blob_store.has(held)
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
            _settings_with(name, 0)

    @pytest.mark.parametrize("name", ["IMPORT_PENDING_LIFETIME_HOURS", "IMPORT_HISTORY_RETENTION_DAYS"])
    def test_a_blank_lifetime_reads_as_no_limit(self, name: str) -> None:
        assert getattr(_settings_with(name, ""), name) is None


class TestTheApply:
    @pytest.mark.asyncio
    async def test_it_writes_what_the_review_showed_and_lets_the_upload_go(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        review = await upload(async_db, diver, [("dive.fit", OCEAN_FIT), ("dive.json", OCEAN_JSON)])
        held = [file.storage_key for file in await _files(async_db, diver)]

        applied = await apply(async_db, diver, review["uuid"])

        assert applied["status"] == "applied" and applied["applied_at"]
        [row] = applied["dives"]
        assert (row["outcome"], row["uuid"]) == ("new", review["dives"][0]["uuid"])
        assert row["dive"] == review["dives"][0]["dive"]
        stored = await async_db.scalar(select(Dive).where(Dive.user_id == diver.id))
        assert stored is not None and str(stored.uuid) == row["uuid"]
        assert await _files(async_db, diver) == []
        assert not any([await blob_store.has(key) for key in held])
        kept = await async_db.scalars(select(DiveFile.storage_key).where(DiveFile.user_id == diver.id))
        assert all([await blob_store.has(key) for key in kept.all()])
        assert (
            await async_db.scalar(select(func.count()).select_from(DiveFile).where(DiveFile.user_id == diver.id))
        ) == 2
        LogbookImportRead.model_validate(applied)

    @pytest.mark.asyncio
    async def test_no_transaction_is_open_across_the_held_files(
        self, async_db: AsyncSession, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """They may be the whole request bound, and a connection idle in a transaction for that
        long is the pool exhaustion `release_read_transaction` exists to prevent."""
        diver = create_user(db)
        review = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        await async_db.rollback()
        read = import_routes.held_parts
        open_while_reading: list[bool] = []

        async def reading(files: Any) -> Any:
            open_while_reading.append(async_db.in_transaction())
            return await read(files)

        monkeypatch.setattr(import_routes, "held_parts", reading)

        await apply(async_db, diver, review["uuid"])

        assert open_while_reading == [False]

    @pytest.mark.asyncio
    async def test_an_apply_that_loses_its_files_to_another_device_says_what_became_of_them(
        self, async_db: AsyncSession, async_sessions: async_sessionmaker[AsyncSession], db: Session, monkeypatch: Any
    ) -> None:
        diver = create_user(db)
        review = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        read = import_routes.held_parts

        async def another_device_first(files: Any) -> Any:
            async with async_sessions() as another:
                monkeypatch.setattr(import_routes, "held_parts", read)
                await apply(another, diver, review["uuid"])
            return await read(files)

        monkeypatch.setattr(import_routes, "held_parts", another_device_first)

        assert (await _refusal(apply(async_db, diver, review["uuid"]))).status_code == 409

    @pytest.mark.asyncio
    async def test_an_apply_over_its_budget_reads_nothing(
        self, async_db: AsyncSession, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused apply stays pending, so a retry would otherwise repeat the whole pass freely."""
        diver = create_user(db)
        review = await upload(async_db, diver, [("dive.fit", OCEAN_FIT)])
        monkeypatch.setattr(import_routes, "enforce_rate_limit", AsyncMock(side_effect=RateLimitException("Too many")))
        read = AsyncMock()
        monkeypatch.setattr(import_routes, "held_parts", read)

        assert (await _refusal(apply(async_db, diver, review["uuid"]))).status_code == 429
        read.assert_not_awaited()

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
        second = await upload_and_apply(
            async_db, diver, [("two.json", _logbook({}, {"started_at": "2026-05-02T10:00:00+02:00"}))]
        )
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
            if change["member"].startswith("mixtures/") and change["member"].endswith("_pressure")
        ]
        assert values, "the fixture's JSON should fill the pressures the FIT left blank"
        dive_id = await async_db.scalar(select(Dive.id).where(Dive.user_id == diver.id))
        assert dive_id is not None
        # An edit through the form replaces the list row by row, and here gives each listed member
        # a value of its own: a fill writes blanks, so only the reconciliation can bring `after` back.
        mixtures = (
            await async_db.scalars(select(DiveMixture).where(DiveMixture.dive_id == dive_id).order_by(DiveMixture.id))
        ).all()
        edited: list[dict[str, Any]] = [
            {name: getattr(mixture, name) for name in ("oxygen", "helium", "volume", "gas_number")}
            for mixture in mixtures
        ]
        for change in values:
            _, index, member = change["member"].split("/")
            edited[int(index)][member] = change["after"] - 10
        await replace_mixtures_for_dive(
            db=async_db, dive_id=dive_id, mixtures=[DiveMixtureCreate(**cylinder) for cylinder in edited]
        )
        await async_db.execute(update(Dive).where(Dive.id == dive_id).values(max_depth=11.5))
        await async_db.commit()

        applied = await apply(async_db, diver, review["uuid"])

        assert [one["outcome"] for one in applied["dives"]] == ["modified"]
        stored = (
            await async_db.scalars(
                select(DiveMixture)
                .where(DiveMixture.dive_id == dive_id)
                .order_by(DiveMixture.id)
                .execution_options(populate_existing=True)
            )
        ).all()
        for change in values:
            _, index, member = change["member"].split("/")
            assert getattr(stored[int(index)], member) == change["after"]
        depth = await async_db.scalar(select(Dive.max_depth).where(Dive.id == dive_id))
        assert depth == 11.5, "the review did not list it, so the edit stands"

    @pytest.mark.asyncio
    async def test_a_cylinder_list_reshaped_since_is_a_note_rather_than_a_write_onto_another_cylinder(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])
        review = await upload(async_db, diver, [("dive.json", OCEAN_JSON)])
        dive_id = await async_db.scalar(select(Dive.id).where(Dive.user_id == diver.id))
        assert dive_id is not None
        reshaped = len(review["dives"][0]["dive"]["mixtures"]) + 1
        await replace_mixtures_for_dive(
            db=async_db, dive_id=dive_id, mixtures=[DiveMixtureCreate(oxygen=0.5) for _ in range(reshaped)]
        )
        await async_db.commit()

        applied = await apply(async_db, diver, review["uuid"])

        stored = (await async_db.scalars(select(DiveMixture.oxygen).where(DiveMixture.dive_id == dive_id))).all()
        assert stored == [0.5] * reshaped
        assert applied["dives"][0]["outcome"] == "modified"
        [row] = (await _notes(async_db, diver, review["uuid"]))["dives"]
        assert ImportNoteCode.REVIEW_NOT_HELD.value in {note["code"] for note in row["notes"]}

    @pytest.mark.asyncio
    async def test_a_pair_of_members_lands_together_past_what_the_diver_edited(self, db: Session) -> None:
        """200/50 over 300/250 written one member at a time would break the order `CHECK`
        between the two statements."""
        reviewed = {"mixtures": [{"gas_number": 0}], "recordings": []}
        stored = {
            "mixtures": [
                {
                    "id": 7,
                    "gas_number": 0,
                    "start_pressure": 300.0,
                    "end_pressure": 250.0,
                    "oxygen": None,
                    "helium": None,
                    "volume": None,
                }
            ],
            "recordings": [],
        }
        session = AsyncMock()
        changes = [
            {"member": "mixtures/0/start_pressure", "before": None, "after": 200.0},
            {"member": "mixtures/0/end_pressure", "before": None, "after": 50.0},
        ]

        held = await hold_reviewed_values(session, reviewed=reviewed, changes=changes, stored=stored)

        session.execute.assert_awaited_once()
        assert session.execute.await_args is not None
        written = session.execute.await_args.args[0].compile().params
        assert (written["start_pressure"], written["end_pressure"]) == (200.0, 50.0)
        assert (held.wrote, held.moved) == (True, [])

    @pytest.mark.asyncio
    async def test_a_value_the_row_can_no_longer_hold_is_a_note_and_not_a_refused_apply(self, db: Session) -> None:
        """Oxygen 32 beside helium the diver set to 80 is a row the table refuses, and a refusal
        here would fail the apply on every retry."""
        reviewed = {"mixtures": [{"gas_number": 0}], "recordings": []}
        stored = {
            "mixtures": [
                {
                    "id": 7,
                    "gas_number": 0,
                    "start_pressure": None,
                    "end_pressure": None,
                    "oxygen": None,
                    "helium": 80.0,
                    "volume": None,
                }
            ],
            "recordings": [],
        }
        session = AsyncMock()
        changes = [{"member": "mixtures/0/oxygen", "before": None, "after": 32.0}]

        held = await hold_reviewed_values(session, reviewed=reviewed, changes=changes, stored=stored)

        session.execute.assert_not_awaited()
        assert held.wrote is False and len(held.moved) == 1

    @pytest.mark.asyncio
    async def test_gradient_factors_past_each_other_are_a_note(self, db: Session) -> None:
        recording = {"uuid": str(uuid_pkg.uuid4()), "deco_model": {"gf_low": None, "gf_high": 30}}
        session = AsyncMock()
        changes = [{"member": "recordings/0/deco_model/gf_low", "before": None, "after": 40}]

        held = await hold_reviewed_values(
            session,
            reviewed={"mixtures": [], "recordings": [recording]},
            changes=changes,
            stored={"mixtures": [], "recordings": [recording]},
        )

        session.execute.assert_not_awaited()
        assert held.wrote is False and len(held.moved) == 1

    @pytest.mark.parametrize(
        "first_start",
        [
            pytest.param("2026-08-01T12:00:00+00:00", id="the gone dive sorts ahead"),
            pytest.param("2026-08-01T08:00:00+00:00", id="the gone dive sorts behind"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_dive_partly_on_a_gone_dive_goes_with_it_for_the_reason_it_did(
        self, async_db: AsyncSession, db: Session, first_start: str
    ) -> None:
        """One document dive, one recording on a stored dive and one of its own: the whole of it
        is skipped when the stored dive goes, and the new row says why rather than that its file
        could not be read."""
        diver = create_user(db)
        stored = _recording("A1", first_start)
        await upload_and_apply(
            async_db, diver, [("stored.divejson", _logbook({"started_at": first_start, "recordings": [stored]}))]
        )
        start = "2026-08-01T10:00:00+00:00"
        partly = _logbook({"started_at": start, "recordings": [_recording("B2", start), stored]})
        review = await upload(async_db, diver, [("partly.divejson", partly)])
        assert "new" in {row["outcome"] for row in review["dives"]} and len(review["dives"]) == 2
        await async_db.execute(update(Dive).where(Dive.user_id == diver.id).values(is_deleted=True))
        await async_db.commit()

        applied = await apply(async_db, diver, review["uuid"])

        assert [(row["outcome"], row["reason"]) for row in applied["dives"]] == [("skipped", DELETED_HERE)] * 2
        live = select(func.count()).select_from(Dive).where(Dive.user_id == diver.id, Dive.is_deleted.is_(False))
        assert await async_db.scalar(live) == 0
        log = await _notes(async_db, diver, review["uuid"])
        said = {note["code"] for row in log["dives"] for note in row["notes"]}
        assert ImportNoteCode.REVIEW_OVERTAKEN.value not in said, "a skipped row reached no dive"

    @pytest.mark.asyncio
    async def test_a_gone_dive_two_files_reached_is_one_skipped_row(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])
        review = await upload(async_db, diver, [("dive.fit", OCEAN_FIT), ("dive.json", OCEAN_JSON)])
        assert [row["outcome"] for row in review["dives"]] == ["modified"]
        await async_db.execute(update(Dive).where(Dive.user_id == diver.id).values(is_deleted=True))
        await async_db.commit()

        applied = await apply(async_db, diver, review["uuid"])

        assert [row["outcome"] for row in applied["dives"]] == ["skipped"]
        assert (await _rows(async_db, diver))[-1].counts == {"skipped": 1}
        assert len((await _notes(async_db, diver, review["uuid"]))["dives"]) == 1

    @pytest.mark.asyncio
    async def test_a_recording_value_is_held_by_the_recording_s_uuid(self, async_db: AsyncSession, db: Session) -> None:
        diver = create_user(db)
        await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])
        review = await upload(async_db, diver, [("dive.json", OCEAN_JSON)])
        [row] = review["dives"]
        filled = [change for change in row["changes"] if change["member"].startswith("recordings/0/device/")]
        assert filled, "the fixture's JSON should fill a device member the FIT left blank"
        change = filled[0]
        column = DEVICE_COLUMNS[change["member"].rsplit("/", 1)[1]]
        await async_db.execute(
            update(DiveRecording).values({column: "Edited since"}).where(DiveRecording.user_id == diver.id)
        )
        await async_db.commit()

        await apply(async_db, diver, review["uuid"])

        recording = await async_db.scalar(
            select(DiveRecording).where(DiveRecording.user_id == diver.id).execution_options(populate_existing=True)
        )
        assert recording is not None and getattr(recording, column) == change["after"]

    @pytest.mark.parametrize("second", ["dive.json", "dive.fit"], ids=["modified", "unchanged"])
    @pytest.mark.asyncio
    async def test_a_reviewed_dive_deleted_since_is_skipped_and_not_brought_back(
        self, async_db: AsyncSession, db: Session, second: str
    ) -> None:
        diver = create_user(db)
        await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])
        review = await upload(async_db, diver, [(second, OCEAN_JSON if second == "dive.json" else OCEAN_FIT)])
        [reviewed] = review["dives"]
        await async_db.execute(update(Dive).where(Dive.user_id == diver.id).values(is_deleted=True))
        await async_db.commit()

        applied = await apply(async_db, diver, review["uuid"])

        [row] = applied["dives"]
        assert (row["outcome"], row["reason"]) == ("skipped", DELETED_HERE)
        assert (row["uuid"], row["dive"], row["changes"]) == (reviewed["uuid"], reviewed["dive"], [])
        assert await async_db.scalar(select(func.count()).select_from(Dive).where(Dive.user_id == diver.id)) == 1
        kept = await async_db.scalar(select(func.count()).select_from(DiveFile).where(DiveFile.user_id == diver.id))
        assert kept == 1, "the second file is not stored on the deleted dive"

    @pytest.mark.asyncio
    async def test_a_reviewed_dive_merged_away_since_is_skipped_and_the_survivor_is_untouched(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])
        reviewed_dive = await async_db.scalar(select(Dive).where(Dive.user_id == diver.id))
        assert reviewed_dive is not None
        reviewed_id = reviewed_dive.id
        survivor = create_dive(db, diver)
        create_dive_recording(db, diver, survivor)
        earlier = {"start_time": reviewed_dive.start_time - timedelta(minutes=5), "utc_offset_minutes": 120}
        db.execute(update(Dive).where(Dive.id == survivor.id).values(**earlier))
        db.execute(update(DiveRecording).where(DiveRecording.dive_id == survivor.id).values(**earlier))
        db.commit()
        review = await upload(async_db, diver, [("dive.json", OCEAN_JSON)])
        assert review["dives"][0]["outcome"] == "modified"
        first = await _internal(async_db, survivor.id)
        second = await _internal(async_db, reviewed_id)
        await merge_dives(async_db, first=first, second=second)
        await async_db.commit()
        files_before = await async_db.scalar(
            select(func.count()).select_from(DiveFile).where(DiveFile.user_id == diver.id)
        )

        applied = await apply(async_db, diver, review["uuid"])

        assert (applied["dives"][0]["outcome"], applied["dives"][0]["reason"]) == ("skipped", DELETED_HERE)
        files_after = await async_db.scalar(
            select(func.count()).select_from(DiveFile).where(DiveFile.user_id == diver.id)
        )
        assert files_after == files_before

    @pytest.mark.asyncio
    async def test_a_record_another_account_holds_is_written_at_the_uuid_the_review_showed(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        taken = uuid_pkg.uuid4()
        document = _logbook({}, uuid=taken)
        await upload_and_apply(async_db, create_user(db), [("one.divejson", document)])
        diver = create_user(db)

        review = await upload(async_db, diver, [("one.divejson", document)])
        [reviewed] = review["dives"]
        applied = await apply(async_db, diver, review["uuid"])

        assert reviewed["outcome"] == "new" and reviewed["uuid"] != str(taken)
        assert [row["uuid"] for row in applied["dives"]] == [reviewed["uuid"]]
        stored = await async_db.scalar(select(Dive.uuid).where(Dive.user_id == diver.id))
        assert str(stored) == reviewed["uuid"]

    @pytest.mark.asyncio
    async def test_a_new_dive_that_reached_the_logbook_since_takes_the_outcome_of_the_dive_that_holds_it(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)
        document = _logbook({})
        review = await upload(async_db, diver, [("one.divejson", document)])
        with await load_import(parts_of(("copy.divejson", document))) as batch:
            await import_batch(async_db, user_id=diver.id, batch=batch, apply=True)
            await async_db.commit()

        applied = await apply(async_db, diver, review["uuid"])

        [row] = applied["dives"]
        assert (row["outcome"], row["uuid"]) == ("unchanged", review["dives"][0]["uuid"])
        [log] = (await _notes(async_db, diver, review["uuid"]))["dives"]
        assert ImportNoteCode.REVIEW_OVERTAKEN.value in {note["code"] for note in log["notes"]}
        assert await async_db.scalar(select(func.count()).select_from(Dive).where(Dive.user_id == diver.id)) == 1

    @pytest.mark.parametrize(
        "refused",
        [
            pytest.param(("b.fit", OCEAN_FIT[:40]), id="a FIT that does not convert"),
            pytest.param(("b.zip", "second archive"), id="a second full-export archive"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_refused_file_sorting_among_the_readable_ones_moves_no_row(
        self, async_db: AsyncSession, db: Session, refused: tuple[str, Any]
    ) -> None:
        """A refused file holds a row in the batch the upload reads and none in the batch the
        apply reads from what is stored, so a key by position would shift every row after it."""
        diver, stranger = create_user(db), create_user(db)
        foreign = _logbook({"started_at": "2026-05-03T10:00:00+02:00"})
        await upload_and_apply(async_db, stranger, [("c.divejson", foreign)])
        doomed = _logbook({"started_at": "2026-05-04T10:00:00+02:00"})
        await upload_and_apply(async_db, diver, [("d.divejson", doomed)])
        name, data = refused
        files = [("a.divejson", _logbook({})), ("c.divejson", foreign), ("d.divejson", doomed)]
        if data == "second archive":
            files = [("a.zip", _archive()), *files[1:], (name, _archive())]
        else:
            files.append((name, data))
        review = await upload(async_db, diver, files)
        assert any(member["refusal"] for member in review["members"])
        await async_db.execute(
            update(Dive)
            .where(Dive.user_id == diver.id, Dive.uuid == uuid_pkg.UUID(json.loads(doomed)["dives"][0]["uuid"]))
            .values(is_deleted=True)
        )
        await async_db.commit()

        applied = await apply(async_db, diver, review["uuid"])

        by_uuid = {row["uuid"]: row for row in applied["dives"]}
        assert [row["uuid"] for row in applied["dives"]] == [row["uuid"] for row in review["dives"]]
        doomed_row = by_uuid[json.loads(doomed)["dives"][0]["uuid"]]
        assert doomed_row["outcome"] == "skipped"
        written = await async_db.scalars(select(Dive.uuid).where(Dive.user_id == diver.id, Dive.is_deleted.is_(False)))
        assert {str(uuid) for uuid in written.all()} == {
            row["uuid"] for row in applied["dives"] if row["outcome"] == "new"
        }

    @pytest.mark.asyncio
    async def test_a_dive_the_review_has_no_row_for_names_its_file_in_the_review_s_members(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        """The stored dive lost its recording since, so the file makes a dive of its own, and the
        refused file ahead of it holds a place in the review's `members` and none in the apply's
        batch."""
        diver = create_user(db)
        await upload_and_apply(async_db, diver, [("dive.fit", OCEAN_FIT)])
        review = await upload(async_db, diver, [("a.divejson", b"{not json"), ("dive.json", OCEAN_JSON)])
        await async_db.execute(delete(DiveFile).where(DiveFile.user_id == diver.id))
        await async_db.execute(delete(DiveRecording).where(DiveRecording.user_id == diver.id))
        await async_db.commit()

        applied = await apply(async_db, diver, review["uuid"])

        reviewed, extra = applied["dives"]
        assert reviewed["uuid"] == review["dives"][0]["uuid"]
        assert extra["outcome"] == "new" and extra["dive"]["uuid"] == extra["uuid"]
        assert [applied["members"][index]["name"] for index in extra["members"]] == ["dive.json"]
        written = await async_db.scalar(select(Dive.uuid).where(Dive.uuid == uuid_pkg.UUID(extra["uuid"])))
        assert str(written) == extra["uuid"], "the file is not lost"
        _, shown = (await _notes(async_db, diver, review["uuid"]))["dives"]
        assert (shown["uuid"], shown["outcome"]) == (extra["uuid"], "new")
        assert ImportNoteCode.REVIEW_UNSHOWN.value in {note["code"] for note in shown["notes"]}


class TestThePortrait:
    @pytest.mark.asyncio
    async def test_the_offer_is_a_file_of_the_import_and_goes_with_the_apply(
        self, async_db: AsyncSession, db: Session
    ) -> None:
        diver = create_user(db)

        review = await upload(async_db, diver, [("logbook.zip", _archive_with_a_portrait())])

        assert review["portrait"]["proposed"].startswith("data:image/webp;base64,")
        stored = (await _rows(async_db, diver))[0].review["portrait"]
        assert "proposed" not in stored, "the picture is in the store, not the column"
        roles = sorted(file.role for file in await _files(async_db, diver))
        assert roles == ["part", "portrait"]
        assert await _pending(async_db, diver) == review

        applied = await apply(async_db, diver, review["uuid"])

        assert applied["portrait"] is None and applied["check_in_details"] == []
        assert await _files(async_db, diver) == []


class TestTheShapeOfWhatIsStored:
    def test_the_stored_shape_is_pinned_beside_its_number(self) -> None:
        """A review is served as it was stored, so a change to a schema it embeds reaches rows no
        deploy rewrites. If this fails, bump `REVIEW_SHAPE`, make the call on the stored rows -
        migrate them or retire them - and only then write the new digest and number here."""
        assert (REVIEW_SHAPE, _shape_digest()) == (1, EMBEDDED_SHAPE)

    def test_a_change_of_wording_moves_nothing(self) -> None:
        first = _without_docs({"title": "A", "description": "x", "examples": [1], "properties": {"title": {}}})
        second = _without_docs({"title": "B", "description": "y", "examples": [2], "properties": {"title": {}}})

        assert first == second == {"properties": {"title": {}}}


class TestASiteReplacedWhileTheImportWaits:
    @pytest.mark.asyncio
    async def test_the_apply_links_the_record_and_brings_no_site_back_under_the_uuid(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        site = create_dive_site(db, diver)
        site_id, uuid = site.id, site.uuid
        record = create_commons_site(db)
        document = json.loads(_logbook({"site_uuids": [str(uuid)]}))
        document["sites"] = [{"uuid": str(uuid), "name": "As it was exported"}]
        review = await upload(async_db, diver, [("logbook.divejson", json.dumps(document).encode())])

        await replace_site_with_record(async_db, user_id=diver.id, site_id=site_id, record_id=record.id)
        await async_db.commit()
        await apply(async_db, diver, review["uuid"])

        assert (await async_db.scalars(select(DiveSite.id).where(DiveSite.user_id == diver.id))).all() == []
        named = await async_db.scalars(
            select(DiveDiveSite.dive_site_id)
            .join(Dive, Dive.id == DiveDiveSite.dive_id)
            .where(Dive.user_id == diver.id)
        )
        assert named.all() == [record.id]
