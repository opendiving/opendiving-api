"""What bounds the work a dive-computer file or a logbook document can ask of an instance.

No database: each bound is a property of the bytes or of the wiring - the reader's guard
against an extraction that raises, the limiter every conversion queues behind, the zip
directory count taken before `zipfile` builds anything, the nesting the JSON parser gives up
on, the import's member reads off the event loop, and the per-account budget on the two
routes that read a dive-computer file.
"""

import asyncio
import hashlib
import io
import struct
import tempfile
import threading
import time
import uuid as uuid_pkg
import zipfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import anyio.to_thread
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.api.dependencies import get_current_user
from src.app.api.v1 import dives as dives_module
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.core.utils import rate_limit
from src.app.services import dive_files, dive_reader
from src.app.services.export.archive import DIVEJSON_NAME
from src.app.services.logbook_import import ImportTooLargeError, MalformedImportError, load_import, reader
from src.app.services.logbook_import import writer as writer_module
from src.app.services.logbook_import.planner import PlannedFile
from tests.helpers.import_parts import part_of

SUUNTO_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal">
  <StartTime>2026-06-03T12:15:00</StartTime>
  <MaxDepth>25.5</MaxDepth>
  <Duration>1800</Duration>
</Dive>
"""


def _zip(count: int, *, comment: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index in range(count):
            archive.writestr(zipfile.ZipInfo(f"{index}"), b"")
        archive.comment = comment
    return buffer.getvalue()


def _declaring(data: bytes, entries: int) -> bytes:
    """`data`, a zip with no comment, with its end record's two entry counts rewritten."""
    end = len(data) - 22
    assert data[end : end + 4] == b"PK\x05\x06"
    return data[: end + 8] + struct.pack("<2H", entries, entries) + data[end + 12 :]


@pytest.fixture(autouse=True)
def no_storage_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The storage check reads the database these tests stub out."""
    monkeypatch.setattr(settings, "STORAGE_LIMIT_MB", None)


@pytest.fixture(autouse=True)
def own_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every route here spends the read budget, and a Redis another test's lifespan opened is
    bound to that test's event loop."""
    monkeypatch.setattr(rate_limit.cache, "client", _Counter())


@pytest.fixture(scope="module")
def zip64() -> bytes:
    """More entries than the classic end record can count, so Python writes the zip64 one."""
    return _zip(0xFFFF + 2)


class TestExtractingAStoredFileNeverRaises:
    def test_an_extraction_that_raises_reads_as_unread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The promise the hourly fix read leans on: an exception out of it stops the whole
        batch, and the batch is selected again on the next pass, for ever."""

        def overflow(_read: object) -> object:
            raise OverflowError("date value out of range")

        monkeypatch.setattr(dive_files, "extraction_of", overflow)

        extraction = dive_files.extract_file(SUUNTO_XML, "suunto_xml")

        assert extraction is dive_files._UNREAD


class TestConversionsQueueBehindTheirOwnLimiter:
    @pytest.mark.asyncio
    async def test_the_limiter_is_its_own_and_admits_two(self) -> None:
        assert dive_reader.CONVERT_LIMITER.total_tokens == 2
        assert dive_reader.CONVERT_LIMITER is not anyio.to_thread.current_default_thread_limiter()

    @pytest.mark.asyncio
    async def test_no_more_than_two_run_at_once(self) -> None:
        lock = threading.Lock()
        running = peak = 0

        def convert() -> None:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            with lock:
                running -= 1

        await asyncio.gather(*(dive_reader.run_in_conversion_thread(convert) for _ in range(6)))

        assert peak == 2

    @pytest.mark.asyncio
    async def test_keyword_arguments_reach_the_callable(self) -> None:
        def join(a: str, *, b: str) -> str:
            return a + b

        assert await dive_reader.run_in_conversion_thread(join, "x", b="y") == "xy"

    def test_the_form_reads_its_file_behind_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hops: list[tuple[object, object]] = []
        real = dive_reader.anyio.to_thread.run_sync

        async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
            hops.append((getattr(func, "func", func), kwargs.get("limiter")))
            return await real(func, *args, **kwargs)

        monkeypatch.setattr(dive_reader.anyio.to_thread, "run_sync", spy)

        response = _client().post("/dive/parse", files={"file": ("dive.xml", SUUNTO_XML, "application/xml")})

        assert response.status_code == 200
        assert (dive_reader.read_prefill, dive_reader.CONVERT_LIMITER) in hops

    @pytest.mark.asyncio
    async def test_an_import_surveys_and_parses_behind_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hopped: list[object] = []
        real = reader.run_in_conversion_thread

        async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
            hopped.append(func)
            return await real(func, *args, **kwargs)

        monkeypatch.setattr(reader, "run_in_conversion_thread", spy)

        with pytest.raises(reader.UnsupportedImportError):
            await load_import([part_of(b'{"format": "something else"}')])

        assert hopped == [reader._survey, reader._load_document]


class TestAZipIsCountedBeforeItIsOpened:
    def test_a_directory_past_the_cap_is_refused_without_building_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        data = _zip(11)
        monkeypatch.setattr(reader, "MAX_ARCHIVE_ENTRIES", 10)

        def never(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("zipfile was handed the archive")

        monkeypatch.setattr(reader.zipfile, "ZipFile", never)

        with pytest.raises(ImportTooLargeError, match="more than 10 entries"):
            reader._open_archive(io.BytesIO(data))

    def test_the_headers_are_counted_rather_than_the_declared_total(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`zipfile` never reads the end record's total, so a file declaring one entry is
        still built entry by entry - the count has to come from the directory itself."""
        data = _declaring(_zip(11), 1)
        assert len(zipfile.ZipFile(io.BytesIO(data)).infolist()) == 11
        monkeypatch.setattr(reader, "MAX_ARCHIVE_ENTRIES", 10)

        with pytest.raises(ImportTooLargeError):
            reader._open_archive(io.BytesIO(data))

    def test_an_archive_under_the_cap_opens(self) -> None:
        buffer = io.BytesIO(_zip(3))

        archive = reader._open_archive(buffer)

        assert len(archive.infolist()) == 3

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(_zip(0), id="empty"),
            pytest.param(_zip(5, comment=b"a comment PK\x05 that is not a record"), id="commented"),
            pytest.param(_zip(5, comment=b"c" * 0xFFFF), id="the longest comment"),
            pytest.param(b"#!/bin/sh\nexit 0\n" * 40 + _zip(4), id="prepended"),
        ],
    )
    def test_it_counts_what_zipfile_builds(self, data: bytes) -> None:
        buffer = io.BytesIO(data)
        buffer.seek(5)

        _, counted = reader._measure_directory(buffer, limit=1000)

        assert counted == len(zipfile.ZipFile(io.BytesIO(data)).infolist())
        assert buffer.tell() == 5

    def test_a_zip64_directory_is_found_through_its_locator(self, zip64: bytes) -> None:
        assert zip64[-22:-18] == b"PK\x05\x06" and zip64[-14:-12] == b"\xff\xff"
        assert zip64[-42:-38] == b"PK\x06\x07"

        assert reader._measure_directory(io.BytesIO(zip64), limit=10**6)[1] == 0xFFFF + 2

    def test_a_zip64_directory_past_the_cap_is_refused(self, zip64: bytes) -> None:
        assert reader.MAX_ARCHIVE_ENTRIES < 0xFFFF

        with pytest.raises(ImportTooLargeError):
            reader._open_archive(io.BytesIO(zip64))

    def test_a_directory_past_its_byte_cap_is_refused_without_reading_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A few hundred entries, each carrying the longest comment a header allows, are far
        under the entry cap and still over 16 MB of directory for `zipfile` to read whole."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            for index in range(260):
                info = zipfile.ZipInfo(f"{index}")
                info.comment = b"c" * 0xFFFF
                archive.writestr(info, b"")
        data = buffer.getvalue()

        def never(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("zipfile was handed the archive")

        monkeypatch.setattr(reader.zipfile, "ZipFile", never)

        assert reader.MAX_ARCHIVE_DIRECTORY_SIZE == 16 * 1024 * 1024
        (stated,) = struct.unpack_from("<L", data, len(data) - 22 + 12)
        assert reader._measure_directory(io.BytesIO(data), limit=1000) == (stated, 260)
        assert reader.MAX_ARCHIVE_DIRECTORY_SIZE < stated
        with pytest.raises(ImportTooLargeError, match="larger than 16 MB"):
            reader._open_archive(io.BytesIO(data))

    def test_bytes_with_no_end_record_are_left_to_zipfile(self) -> None:
        with pytest.raises(MalformedImportError, match="could not be opened"):
            reader._open_archive(io.BytesIO(b"PK\x03\x04" + b"\0" * 64))

    @pytest.mark.parametrize("pointed_at", [2**63, 2**64 - 1])
    @pytest.mark.parametrize("rolled", [False, True], ids=["in memory", "on disk"])
    def test_a_locator_pointing_past_the_file_is_left_to_zipfile(self, pointed_at: int, rolled: bool) -> None:
        """Too far to seek to at all; `zipfile` refuses the locator, as the import does."""
        data = (
            b"PK\x03\x04"
            + struct.pack("<4sLQL", b"PK\x06\x07", 0, pointed_at, 1)
            + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0, 0, 0, 0, 0)
        )
        buffer = tempfile.SpooledTemporaryFile(max_size=1)
        buffer.write(data)
        if rolled:
            buffer.rollover()

        with pytest.raises(MalformedImportError, match="could not be opened"):
            reader._open_archive(buffer if rolled else io.BytesIO(data))

    @pytest.mark.asyncio
    async def test_an_import_of_one_is_a_413(self, zip64: bytes) -> None:
        with pytest.raises(ImportTooLargeError):
            await load_import([part_of(zip64, "export.zip")])


class TestAFileTheAppCannotShapeIsARowNotA500:
    @pytest.mark.asyncio
    async def test_it_is_refused_with_the_read_fault_and_the_rest_of_the_import_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def overflow(_read: object) -> object:
            raise OverflowError("date value out of range")

        monkeypatch.setattr(reader, "extraction_of", overflow)
        document = b'{"format": "divejson", "version": "1.0", "exported_at": "2026-04-17T11:49:23+02:00"}'

        with await load_import([part_of(SUUNTO_XML, "dive.xml", 0), part_of(document, "logbook.divejson", 1)]) as batch:
            refused = next(row for row in batch.rows if row.name == "dive.xml")
            assert isinstance(refused.refusal, MalformedImportError)
            assert str(refused.refusal) == reader._READ_FAULT
            assert [row.name for row in batch.read] == ["logbook.divejson"]


_TOO_DEEP = b"[" * 1_000_000 + b"]" * 1_000_000


class TestADocumentNestedTooDeeplyIsMalformed:
    @pytest.mark.asyncio
    async def test_a_bare_document(self) -> None:
        with pytest.raises(MalformedImportError, match="nests too deeply"):
            await load_import([part_of(b'{"format": "divejson", "dives": ' + _TOO_DEEP + b"}")])

    @pytest.mark.asyncio
    async def test_the_archive_s_document(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(DIVEJSON_NAME, b'{"format": "divejson", "dives": ' + _TOO_DEEP + b"}")

        with pytest.raises(MalformedImportError, match="nests too deeply"):
            await load_import([part_of(buffer.getvalue(), "export.zip")])


class TestAnImportReadsItsFilesOffTheEventLoop:
    @pytest.mark.asyncio
    async def test_a_member_is_read_and_hashed_in_a_thread(self) -> None:
        data = b"a stored dive file"
        loop_thread = threading.current_thread()
        read_on: list[threading.Thread] = []

        def read_member(_path: str) -> bytes:
            read_on.append(threading.current_thread())
            return data

        loaded = MagicMock()
        loaded.read_member = read_member
        plan = MagicMock()
        plan.records = {}
        staged = MagicMock()
        staged.stage.return_value = ("dive-files/key", len(data))
        writer = writer_module._Writer(AsyncMock(), user_id=1, loaded=loaded, plan=plan, staged=staged)
        planned = PlannedFile(
            archive_path="files/dive.xml",
            sha256=hashlib.sha256(data).hexdigest(),
            original_filename="dive.xml",
            content_type="application/xml",
        )

        stored = await writer._store_blob(
            planned, kind="dive-files", collection="dives", record_uuid=uuid_pkg.uuid4(), sniff=False
        )

        assert stored is not None and stored.digest == planned.sha256
        assert read_on and read_on[0] is not loop_thread


class _Counter:
    """Enough of Redis for the fixed-window limiter: a counter per key and its window."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.windows: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.windows[key] = seconds

    async def ttl(self, key: str) -> int:
        return self.windows.get(key, -1)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(dives_module.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "uuid": uuid_pkg.UUID("00000000-0000-0000-0000-0000000000aa"),
        "is_superuser": False,
    }
    nothing = MagicMock()
    nothing.__iter__.return_value = iter(())
    session = AsyncMock()
    session.execute = AsyncMock(return_value=nothing)
    app.dependency_overrides[async_get_db] = lambda: session
    return TestClient(app)


class TestReadingDiveFilesIsRateLimitedPerAccount:
    @pytest.fixture
    def counter(self, monkeypatch: pytest.MonkeyPatch) -> _Counter:
        counter = _Counter()
        monkeypatch.setattr(rate_limit.cache, "client", counter)
        return counter

    def test_the_sixty_first_read_in_the_hour_is_a_429(self, counter: _Counter) -> None:
        client = _client()
        csv = {"file": ("export.csv", b"time,depth\n0,0\n", "text/csv")}

        answers = [
            client.post("/dive/parse", files=csv).status_code for _ in range(dives_module.DIVE_FILE_READS_PER_USER)
        ]
        over = client.post("/dive/parse", files=csv)

        assert dives_module.DIVE_FILE_READS_PER_USER == 60
        assert set(answers) == {415}
        assert over.status_code == 429
        assert counter.windows == {"dive-file-read:user:7": 3600}

    def test_attaching_spends_the_same_budget(self, counter: _Counter, monkeypatch: pytest.MonkeyPatch) -> None:
        """Counted before the dive is looked up, so the 61st attach is refused whatever dive it names."""
        missing = AsyncMock(side_effect=NotFoundException("Dive not found"))
        monkeypatch.setattr(dives_module, "_get_owned_dive", missing)
        client = _client()
        attach = {"files": {"file": ("dive.xml", SUUNTO_XML, "application/xml")}, "data": {"file_token": "t"}}
        path = f"/dive/{uuid_pkg.uuid4()}/recordings"
        csv = {"file": ("export.csv", b"time,depth\n0,0\n", "text/csv")}

        parsed = [client.post("/dive/parse", files=csv).status_code for _ in range(30)]
        attached = [client.post(path, **attach).status_code for _ in range(30)]
        over = client.post(path, **attach)

        assert set(parsed) == {415} and set(attached) == {404}
        assert over.status_code == 429
        assert missing.await_count == 30
