"""Unit tests for the dive-file upload endpoint (`/dive/parse`): its size guard, and the
refusals the reader answers with.

No database: the session is a stub whose every query finds nothing, which is what the
parse route's one read - the recordings a file might belong to - gets for a new account.
"""

import io
import uuid as uuid_pkg
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.api.dependencies import get_current_user
from src.app.api.v1 import dives as dives_module
from src.app.api.v1.dives import _parse_matches
from src.app.api.v1.dives import router as dives_router
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.security import verify_dive_file_token
from src.app.schemas.parsed_dive import ParsedDiveSchema
from src.app.services.dive_files import MAX_DIVE_FILE_SIZE
from tests.helpers.fit import dense_record_stream

FIXTURES = Path(__file__).parent / "fixtures"

SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"

USER_UUID = uuid_pkg.UUID("00000000-0000-0000-0000-0000000000aa")

VALID_SUUNTO_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <StartTime>2026-06-03T12:15:00</StartTime>
  <MaxDepth>25.5</MaxDepth>
  <Duration>1800</Duration>
</Dive>
""".encode()


@pytest.fixture(autouse=True)
def no_storage_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """These run with no database, and the storage check reads one; the storage limit's
    parse pre-check has its own tests, against Postgres, in `tests/test_storage_limit.py`."""
    monkeypatch.setattr(settings, "STORAGE_LIMIT_MB", None)


def _make_dive_upload_client() -> TestClient:
    """Build a minimal app exposing only the dives router, with auth stubbed out.

    This avoids exercising the full application lifespan (DB/Redis setup),
    keeping the test focused on the upload endpoint's own behavior.
    """
    app = FastAPI()
    app.include_router(dives_router)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "uuid": USER_UUID, "is_superuser": False}
    nothing = MagicMock()
    nothing.__iter__.return_value = iter(())
    session = AsyncMock()
    session.execute = AsyncMock(return_value=nothing)
    app.dependency_overrides[async_get_db] = lambda: session
    return TestClient(app)


class TestParseDiveUploadSizeLimit:
    def test_accepts_file_within_limit(self):
        client = _make_dive_upload_client()

        response = client.post(
            "/dive/parse",
            files={"file": ("export.xml", VALID_SUUNTO_XML, "application/xml")},
        )

        assert response.status_code == 200
        assert response.json()["max_depth"] == 25.5

    def test_rejects_file_over_size_limit_with_413(self):
        client = _make_dive_upload_client()
        oversized_content = b"a" * (MAX_DIVE_FILE_SIZE + 1)

        response = client.post(
            "/dive/parse",
            files={"file": ("export.xml", oversized_content, "application/xml")},
        )

        assert response.status_code == 413

    def test_does_not_buffer_more_than_the_limit_in_memory(self):
        """The endpoint must reject oversized uploads by reading bounded chunks,
        not by trusting Content-Length or reading the whole body unconditionally."""
        client = _make_dive_upload_client()
        # Comfortably larger than the limit, but not so large the test itself is slow.
        oversized_content = b"b" * (MAX_DIVE_FILE_SIZE * 2)

        response = client.post(
            "/dive/parse",
            files={"file": ("export.xml", oversized_content, "application/xml")},
        )

        assert response.status_code == 413

    def test_missing_filename_is_rejected(self):
        client = _make_dive_upload_client()

        response = client.post(
            "/dive/parse",
            files={"file": ("", VALID_SUUNTO_XML, "application/xml")},
        )

        # An empty filename never reaches parsing logic: multipart validation itself
        # rejects it (422) before our endpoint's own `400` filename check would run.
        assert response.status_code in (400, 422)

    def test_unsupported_file_returns_415(self):
        client = _make_dive_upload_client()

        response = client.post(
            "/dive/parse",
            files={"file": ("export.csv", b"time,depth\n0,0\n", "text/csv")},
        )

        assert response.status_code == 415


def _parse(name: str, content: bytes) -> tuple[int, dict]:
    response = _make_dive_upload_client().post(
        "/dive/parse", files={"file": (name, content, "application/octet-stream")}
    )
    return response.status_code, response.json()


class TestTheReadersRefusals:
    """What the form is told for each file it cannot take, as the importer tells it."""

    def test_a_file_of_one_dive_is_read_whatever_its_name_says(self) -> None:
        """The head decides the format and the name decides nothing."""
        status, body = _parse("dive.txt", (FIXTURES / "dive_files" / "suunto-d5.json").read_bytes())

        assert status == 200
        claims = verify_dive_file_token(body["file_token"])
        assert claims is not None and claims.parser_key == "suunto_json"

    def test_a_zip_is_a_415_saying_the_form_takes_one_file(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("dive.fit", (FIXTURES / "dive_files" / "suunto-ocean-2026.fit").read_bytes())

        status, body = _parse("one.zip", archive.getvalue())

        assert status == 415
        assert "takes one dive-computer file" in body["detail"]

    def test_bytes_nothing_reads_are_a_415_naming_the_formats_this_build_reads(self) -> None:
        status, body = _parse("some.pdf", b"%PDF-1.4\n%...")

        assert status == 415
        assert "UDDF (.uddf)" in body["detail"] and "FIT (.fit)" in body["detail"]

    def test_a_file_of_several_dives_is_a_422_naming_the_count_and_logbook_import(self) -> None:
        status, body = _parse("demo-account.uddf", (FIXTURES / "uddf" / "demo-account.uddf").read_bytes())

        assert status == 422
        assert "8 dives" in body["detail"] and "logbook" in body["detail"]

    def test_one_dive_recorded_by_two_computers_is_a_422_pointing_at_logbook_import(self) -> None:
        """A stored file is one recording's, so a file carrying two computers' records of one
        dive is refused here rather than half of it attached."""
        status, body = _parse("two-computers.ssrf", (FIXTURES / "dive_files" / "two-computers.ssrf").read_bytes())

        assert status == 422
        assert "one computer's record of one dive" in body["detail"] and "logbook" in body["detail"]

    def test_a_file_recording_no_dive_is_a_422(self) -> None:
        status, body = _parse("run.json", (FIXTURES / "dive_files" / "not-a-dive.json").read_bytes())

        assert status == 422
        assert "records no dive" in body["detail"]

    def test_a_claimed_file_the_reader_cannot_convert_is_a_422_carrying_its_message(self) -> None:
        truncated = (FIXTURES / "dive_files" / "suunto-ocean-2026.fit").read_bytes()[:200]

        status, body = _parse("cut.fit", truncated)

        assert status == 422
        assert "not a readable FIT file" in body["detail"]

    def test_a_fit_past_the_readers_bound_is_a_422_saying_it_is_an_activity_log(self) -> None:
        """Under the upload cap and past what one dive's record can hold. A 413 would tell the
        diver to shrink a file that is already small enough, so the answer says what it is."""
        status, body = _parse("watch.fit", dense_record_stream(100_001))

        assert status == 422
        assert "activity log rather than a dive" in body["detail"]

    @pytest.mark.parametrize(
        "document",
        [
            # An external entity reaching for a local file, on a root the DM5 reader claims.
            b"""<?xml version="1.0"?>
<!DOCTYPE Dive [
 <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<Dive xmlns="http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"><StartTime>&xxe;</StartTime></Dive>""",
            # Nested entities that expand exponentially, on a root the UDDF reader claims.
            b"""<?xml version="1.0"?>
<!DOCTYPE uddf [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<uddf version="3.2.3" xmlns="http://www.streit.cc/uddf/3.2/"><generator><name>&lol3;</name></generator></uddf>""",
        ],
        ids=["xxe", "billion-laughs"],
    )
    def test_a_doctype_is_refused_before_anything_is_expanded(self, document: bytes) -> None:
        """The package refuses any `<!DOCTYPE>` on its own (spec §9), which is what keeps an
        entity from being resolved at all - the app carries no XML parser of its own."""
        status, body = _parse("hostile.xml", document)

        assert status == 422
        assert "DOCTYPE" in body["detail"]


class TestADayIsNoStartToMatchOn:
    """A logbook file may state only the date a dive happened on. `fromisoformat` reads that
    as midnight, which would offer every dive recorded around midnight that day."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started_at", ["2026-09-01", "20260901"])
    async def test_a_date_only_start_matches_nothing(self, monkeypatch: pytest.MonkeyPatch, started_at: str) -> None:
        looked_up = AsyncMock(return_value=[])
        monkeypatch.setattr(dives_module, "load_candidates", looked_up)
        parsed = ParsedDiveSchema(
            avg_depth=None,
            bottom_temperature=None,
            dive_number=None,
            duration=1800,
            max_depth=20.0,
            start_time=started_at,
            mixtures=[],
        )

        assert await _parse_matches(AsyncMock(), user_id=1, parsed=parsed) == []
        looked_up.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_start_with_a_clock_is_still_looked_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        looked_up = AsyncMock(return_value=[])
        monkeypatch.setattr(dives_module, "load_candidates", looked_up)
        parsed = ParsedDiveSchema(
            avg_depth=None,
            bottom_temperature=None,
            dive_number=None,
            duration=1800,
            max_depth=20.0,
            start_time="2026-09-01T00:00:00",
            mixtures=[],
        )

        assert await _parse_matches(AsyncMock(), user_id=1, parsed=parsed) == []
        looked_up.assert_called_once()
