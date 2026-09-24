"""The two `/import/logbook*` routes (`api/v1/logbook_import.py`).

What is route behaviour rather than importer behaviour is a short list, and it is all
here: authentication, the rate limit, the status code each refusal comes back as, which
uploads the converter claims and what it reports about them, and the preview token that
ties an apply to the bytes a diver was shown a report for. The importing itself is
`test_logbook_import.py`, against real Postgres.

Every source-format payload below is built inline, the way `MINIMAL` is, except the one
UDDF fixture this repository already carries for the export tests. Real dive-computer
captures live outside this repository on purpose, so what they prove is a manual walk
rather than a test here - and an inline document exercises exactly the same seam.

The token pair is the one thing worth testing at this layer specifically. It is what makes
"you approved *this* file" true, and its two failure modes - a token for another account
and a token for other bytes - are indistinguishable from a successful import if either
check is dropped.
"""

import hashlib
import io
import json
import threading
import uuid as uuid_pkg
import zipfile
from collections.abc import Generator
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import divejson
import pytest
from fastapi.testclient import TestClient
from uuid6 import uuid7

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import logbook_import as import_route
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.exceptions.http_exceptions import RateLimitException
from src.app.core.security import create_logbook_import_token
from src.app.core.setup import create_application
from src.app.schemas.logbook_import import (
    ImportBornOnDetail,
    ImportCheckInInsurance,
    ImportInsuranceDetail,
    ImportPortraitOffer,
)
from src.app.services.logbook_import import reader

PREVIEW_PATH = "/api/v1/import/logbook/preview"
APPLY_PATH = "/api/v1/import/logbook"

CURRENT_USER_UUID = uuid7()
CURRENT_USER = {"id": 1, "uuid": CURRENT_USER_UUID, "username": "ada", "is_superuser": False}

MINIMAL = json.dumps({"format": "divejson", "version": "1.0", "exported_at": "2026-04-17T11:49:23+02:00"}).encode()

# The export corpus the UDDF writer's tests already keep - a real download of a whole demo
# account, and the largest source document this repository has.
UDDF_CORPUS = Path(__file__).parent / "fixtures" / "uddf" / "demo-account.uddf"

# A logbook nothing in this app wrote. Deliberately carrying no UTC offset, which is the
# commonest thing a converter has to report as absent, so the conversion block below has
# something real in it rather than an empty list.
SSRF = b"""<?xml version="1.0"?>
<divelog program="subsurface" version="3">
<divesites>
<site uuid="a1b2c3d4" name="Blue Hole" gps="28.5717 34.5372"/>
</divesites>
<dives>
<dive number="1" date="2026-04-17" time="09:30:00" duration="42:10 min" divesiteid="a1b2c3d4">
<depth max="28.4 m" mean="14.2 m"/>
</dive>
</dives>
</divelog>
"""

# Neither JSON nor a zip nor anything the registry claims: the file a diver picks by
# mistake, which used to be told its DiveJSON was broken.
NOT_A_LOGBOOK = b"date,depth\n2026-04-17,28.4\n"


def _uddf(dive_id: str = "dive-1", when: str = "2026-04-17T09:30:00") -> bytes:
    """One UDDF dive, built here rather than captured.

    Enough of the format to convert: a site, a repetition group, a dive that links the
    site, and a three-waypoint profile. The captured exports that exercise the readers
    properly are the library's own fixtures and a by-hand walk over real dive-computer
    files.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<uddf xmlns="http://www.streit.cc/uddf/3.2/" version="3.2.2">
  <generator><name>opendiving-tests</name></generator>
  <divesite><site id="site-1"><name>Blue Hole</name></site></divesite>
  <profiledata>
    <repetitiongroup id="rg-1">
      <dive id="{dive_id}">
        <informationbeforedive><datetime>{when}</datetime><link ref="site-1"/></informationbeforedive>
        <samples>
          <waypoint><depth>0.0</depth><divetime>0</divetime></waypoint>
          <waypoint><depth>28.4</depth><divetime>600</divetime></waypoint>
          <waypoint><depth>0.0</depth><divetime>2530</divetime></waypoint>
        </samples>
        <informationafterdive><greatestdepth>28.4</greatestdepth><diveduration>2530</diveduration></informationafterdive>
      </dive>
    </repetitiongroup>
  </profiledata>
</uddf>
""".encode()


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def import_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`, like `test_export_endpoints.py`.

    The shared `client` fixture opens `src.app.main`'s app, whose startup hook connects to
    Postgres - which would make these route tests part of the database-backed subset for no
    reason: everything below the route is stubbed here.
    """
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def client(import_app: Any) -> Generator[TestClient]:
    with TestClient(import_app) as test_client:
        yield test_client
    import_app.dependency_overrides = {}


@pytest.fixture
def signed_in(import_app: Any, monkeypatch: Any) -> Any:
    """The two routes with their planner, writer and rate limiter stubbed out."""
    import_app.dependency_overrides[get_current_user] = lambda: CURRENT_USER
    import_app.dependency_overrides[async_get_db] = lambda: AsyncMock()

    async def no_limit(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_plan(db: Any, **kwargs: Any) -> Any:
        loaded = kwargs["loaded"]
        from src.app.services.logbook_import.planner import ImportPlan

        return ImportPlan(
            is_archive=loaded.is_archive,
            records={},
            recording_matches=[],
            notes=[],
            notes_dropped=0,
            files_referenced=0,
            files_restored=0,
            files_not_contained=0,
            files_skipped=0,
        )

    async def fake_write(db: Any, **kwargs: Any) -> None:
        return None

    async def fake_resolve(db: Any, **kwargs: Any) -> frozenset[int]:
        return frozenset()

    async def fake_invalidate(user_id: int) -> None:
        return None

    monkeypatch.setattr(import_route, "enforce_rate_limit", no_limit)
    monkeypatch.setattr(import_route, "plan_import", fake_plan)
    monkeypatch.setattr(import_route, "write_import", fake_write)
    monkeypatch.setattr(import_route, "resolve_catalog_gaps", fake_resolve)
    for name in (
        "invalidate_dive_caches",
        "invalidate_certification_caches",
        "invalidate_course_caches",
        "invalidate_gear_caches",
        "invalidate_dive_site_caches",
        "invalidate_trip_caches",
    ):
        monkeypatch.setattr(import_route, name, fake_invalidate)
    return import_app


def _files(payload: bytes = MINIMAL, filename: str = "logbook.divejson") -> dict[str, Any]:
    return {"file": (filename, io.BytesIO(payload), "application/vnd.dive+json")}


class TestAuthentication:
    def test_both_routes_need_a_token(self, client: TestClient) -> None:
        assert client.post(PREVIEW_PATH, files=_files()).status_code == 401
        assert client.post(APPLY_PATH, files=_files(), data={"token": "x"}).status_code == 401

    def test_neither_route_takes_a_user_parameter(self) -> None:
        """ "The caller's own logbook" is true by construction rather than by an ownership
        check that could be got wrong - the same guarantee the export routes make."""
        import inspect

        for handler in (import_route.preview_logbook_import, import_route.apply_logbook_import):
            names = set(inspect.signature(handler).parameters)
            assert not names & {"user_uuid", "username", "user_id"}


class TestPreview:
    def test_it_reports_the_documents_own_markers(self, signed_in: Any, client: TestClient) -> None:
        response = client.post(PREVIEW_PATH, files=_files())

        assert response.status_code == 200
        body = response.json()
        assert body["format"] == "divejson"
        assert body["version"] == "1.0"
        assert body["archive"] is False
        assert body["token"]
        assert body["notes_truncated"] == 0

    def test_it_carries_the_planner_s_check_in_section(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        stub = import_route.plan_import

        async def plan_with_a_section(db: Any, **kwargs: Any) -> Any:
            plan = await stub(db, **kwargs)
            plan.check_in_details = [
                ImportBornOnDetail(proposed=date(1988, 4, 12)),
                ImportInsuranceDetail(
                    account=ImportCheckInInsurance(provider="DAN Europe", number="DE-4471902"),
                    proposed=ImportCheckInInsurance(provider="Aqua Med"),
                ),
            ]
            return plan

        monkeypatch.setattr(import_route, "plan_import", plan_with_a_section)

        body = client.post(PREVIEW_PATH, files=_files()).json()

        assert body["check_in_details"] == [
            {"detail": "born_on", "account": None, "proposed": "1988-04-12"},
            {
                "detail": "insurance",
                "account": {"provider": "DAN Europe", "number": "DE-4471902", "expires_on": None},
                "proposed": {"provider": "Aqua Med", "number": None, "expires_on": None},
            },
        ]

    def test_it_carries_the_planner_s_portrait_beside_the_section(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        from src.app.services.logbook_import.planner import ImportPlan

        offer = ImportPortraitOffer(account_sha256="a" * 64, proposed="data:image/webp;base64,UklGRg==")

        async def portrait_offer(self: Any) -> ImportPortraitOffer:
            return offer

        monkeypatch.setattr(ImportPlan, "portrait_offer", portrait_offer)

        body = client.post(PREVIEW_PATH, files=_files()).json()

        assert body["portrait"] == offer.model_dump()

    def test_a_logbook_with_no_portrait_to_offer_says_null(self, signed_in: Any, client: TestClient) -> None:
        assert client.post(PREVIEW_PATH, files=_files()).json()["portrait"] is None

    def test_a_file_that_is_not_divejson_is_415(self, signed_in: Any, client: TestClient) -> None:
        response = client.post(PREVIEW_PATH, files=_files(b'{"format": "uddf", "version": "3.2.2"}'))

        assert response.status_code == 415

    def test_a_broken_document_is_422(self, signed_in: Any, client: TestClient) -> None:
        response = client.post(PREVIEW_PATH, files=_files(b'{"format": "divejson", "version": "1.0", "dives": 3}'))

        assert response.status_code == 422

    def test_an_oversized_upload_is_413(self, signed_in: Any, client: TestClient, monkeypatch: Any) -> None:
        from src.app.services.logbook_import import reader

        monkeypatch.setattr(reader, "MAX_ARCHIVE_SIZE", 8)
        response = client.post(PREVIEW_PATH, files=_files())

        assert response.status_code == 413

    def test_the_rate_limit_is_enforced(self, signed_in: Any, client: TestClient, monkeypatch: Any) -> None:
        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise RateLimitException("Too many requests. Please try again later.")

        monkeypatch.setattr(import_route, "enforce_rate_limit", refuse)

        assert client.post(PREVIEW_PATH, files=_files()).status_code == 429


class TestTheFormatsItAccepts:
    """The claim the route family was renamed for: a logbook is a logbook, whoever wrote it.

    Each of these used to be a refusal. An `.ssrf` and a UDDF file are valid UTF-8 that is
    not JSON, so they died in the JSON parse as "This DiveJSON document is not valid JSON";
    a zip with no `logbook.divejson` member was a flat 415.
    """

    def test_an_ssrf_logbook_previews(self, signed_in: Any, client: TestClient) -> None:
        response = client.post(PREVIEW_PATH, files=_files(SSRF, "logbook.ssrf"))

        assert response.status_code == 200
        assert response.json()["conversion"]["format"] == "ssrf"

    def test_a_uddf_logbook_previews(self, signed_in: Any, client: TestClient) -> None:
        response = client.post(PREVIEW_PATH, files=_files(_uddf(), "dives.uddf"))

        assert response.status_code == 200
        assert response.json()["conversion"]["format"] == "uddf"

    def test_the_whole_uddf_export_corpus_previews(self, signed_in: Any, client: TestClient) -> None:
        """A real export of a whole demo account, out of this app's own UDDF writer - so
        this is the round trip the feature exists for, at the widest document available
        here."""
        response = client.post(PREVIEW_PATH, files=_files(UDDF_CORPUS.read_bytes(), "demo-account.uddf"))

        assert response.status_code == 200
        assert response.json()["conversion"]["format"] == "uddf"

    def test_the_bytes_decide_and_not_the_name_or_the_content_type(self, signed_in: Any, client: TestClient) -> None:
        """`_files` sends `application/vnd.dive+json` and a `.divejson` name whatever it is
        handed, which is exactly the lie a browser tells when a diver renames a file."""
        response = client.post(PREVIEW_PATH, files=_files(_uddf(), "logbook.divejson"))

        assert response.status_code == 200
        assert response.json()["conversion"]["format"] == "uddf"

    def test_a_zip_of_one_format_is_one_logbook(self, signed_in: Any, client: TestClient) -> None:
        """A watch writes one file per dive, so an account export is a zip of them. Two
        uploads per import against a rate limit of twenty an hour would otherwise cap a
        diver at ten dives an hour."""
        payload = _zip({"dive-1.uddf": _uddf("dive-1"), "dive-2.uddf": _uddf("dive-2", "2026-04-18T09:30:00")})
        response = client.post(PREVIEW_PATH, files=_files(payload, "watch-export.zip"))

        assert response.status_code == 200
        body = response.json()
        assert body["conversion"]["format"] == "uddf"
        # Not the app's own export archive: the converter emits no stored files at all, so
        # nothing in this upload could put a binary back.
        assert body["archive"] is False

    def test_a_zip_of_files_no_reader_claims_is_415_naming_the_formats(
        self, signed_in: Any, client: TestClient
    ) -> None:
        payload = _zip({"a.txt": NOT_A_LOGBOOK, "b.txt": NOT_A_LOGBOOK})
        response = client.post(PREVIEW_PATH, files=_files(payload, "notes.zip"))

        assert response.status_code == 415
        # The converter's own sentence for this case carries the registry's names, so this
        # is the one 415 whose list is spelled in format ids rather than in labels.
        detail = response.json()["detail"]
        assert all(fmt in detail for fmt in divejson.read_formats())

    def test_a_zip_holding_only_packaging_is_415(self, signed_in: Any, client: TestClient) -> None:
        """A folder zipped on a Mac with nothing in it: a directory entry and the `__MACOSX`
        shadow tree, neither of which is a file to convert. Not a format problem, so the
        message is the converter's reason rather than a list of formats."""
        payload = _zip({"logbooks/": b"", "__MACOSX/._logbooks": b"x"})
        response = client.post(PREVIEW_PATH, files=_files(payload, "logbooks.zip"))

        assert response.status_code == 415
        assert "no files to convert" in response.json()["detail"]

    def test_a_zip_with_no_members_at_all_is_not_even_a_zip(self, signed_in: Any, client: TestClient) -> None:
        """`PK\x03\x04` is the *local file header*, so an archive with no members does not
        carry it - and neither this module's sniff nor the library's claims one. The answer
        is the general 415 rather than a container refusal, and the two agree about it,
        which is the part worth pinning.
        """
        response = client.post(PREVIEW_PATH, files=_files(_zip({}), "empty.zip"))

        assert response.status_code == 415
        assert "not a logbook this app can read" in response.json()["detail"]

    def test_a_zip_mixing_two_formats_is_415(self, signed_in: Any, client: TestClient) -> None:
        """An archive is one logbook, so its files have to be one format - otherwise the
        positional identities of two readers' records would share one document."""
        response = client.post(PREVIEW_PATH, files=_files(_zip({"a.uddf": _uddf(), "b.ssrf": SSRF}), "mixed.zip"))

        assert response.status_code == 415
        assert "one format" in response.json()["detail"]

    def test_a_file_no_reader_claims_is_415_naming_the_formats(self, signed_in: Any, client: TestClient) -> None:
        """A 415 rather than the 422 it used to get. "This DiveJSON document is not valid
        JSON" is the wrong sentence for a file that never claimed to be one."""
        response = client.post(PREVIEW_PATH, files=_files(NOT_A_LOGBOOK, "dives.csv"))

        assert response.status_code == 415
        detail = response.json()["detail"]
        assert "not a logbook this app can read" in detail
        # Derived from the registry rather than written out here, which is the point of the
        # sentence: a build that reads a fifth format says so without anyone editing this.
        assert reader.formats_this_build_reads() in detail

    def test_bytes_that_are_not_text_at_all_are_415(self, signed_in: Any, client: TestClient) -> None:
        response = client.post(PREVIEW_PATH, files=_files(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03", "photo.png"))

        assert response.status_code == 415

    def test_a_truncated_document_is_still_422(self, signed_in: Any, client: TestClient) -> None:
        """The commonest real failure this endpoint meets: the app's own export, cut off by
        a failed download. It opens `{`, so it claimed to be a document, and telling its
        owner the app does not recognise its own format would be the wrong answer.

        Beside `test_a_broken_document_is_422`, which parses and is refused a stage later -
        this one never gets past the JSON parse at all.
        """
        response = client.post(PREVIEW_PATH, files=_files(MINIMAL[: len(MINIMAL) // 2]))

        assert response.status_code == 422
        assert "not valid JSON" in response.json()["detail"]

    def test_a_bare_document_over_the_document_cap_is_413(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """The upload is admitted against the archive cap because nothing says which shape it
        is until the zip sniff. Anything but a container then gets the smaller one."""
        monkeypatch.setattr(reader, "MAX_DOCUMENT_SIZE", 8)
        response = client.post(PREVIEW_PATH, files=_files())

        assert response.status_code == 413
        assert "logbook document may be up to" in response.json()["detail"]

    def test_a_document_that_claims_json_and_is_not_utf8_is_422(self, signed_in: Any, client: TestClient) -> None:
        """It opened `{`, so it claimed to be a document - the same reason a truncated one
        keeps its 422 rather than being told the app does not recognise its own format."""
        response = client.post(PREVIEW_PATH, files=_files(b'{"generator": "\xff\xfe"}'))

        assert response.status_code == 422
        assert "not UTF-8 text" in response.json()["detail"]

    def test_a_byte_order_mark_does_not_hide_the_json_claim(self, signed_in: Any, client: TestClient) -> None:
        """A BOM in front of `{` is still a document claiming to be one, and Windows editors
        write them."""
        response = client.post(PREVIEW_PATH, files=_files(b"\xef\xbb\xbf" + MINIMAL[: len(MINIMAL) // 2]))

        assert response.status_code == 422
        assert "not valid JSON" in response.json()["detail"]

    def test_a_zip_with_more_members_than_one_import_reads_is_413(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Refused off the central directory, before a member is inflated: the count is
        known from the listing alone."""
        monkeypatch.setattr(reader, "MAX_ARCHIVE_MEMBERS", 1)
        payload = _zip({"dive-1.uddf": _uddf("dive-1"), "dive-2.uddf": _uddf("dive-2")})
        response = client.post(PREVIEW_PATH, files=_files(payload, "watch-export.zip"))

        assert response.status_code == 413
        # The converter's own sentence, which names the bound that fired - the api's wrapper
        # deliberately restates none of them, so that no message can claim a cause that
        # cannot be one.
        assert "at most 1" in response.json()["detail"]

    def test_a_zip_whose_members_sum_past_the_document_cap_is_413(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """The *sum* of the declared sizes, off the central directory before anything is
        read. Every member of a zip of dive-computer files is converted and every result is
        held until they merge, so the sum is what ends up in memory - `MAX_ARCHIVE_EXTRACTED_SIZE`
        is ten times larger and guards the export archive, whose members are written out one
        at a time.
        """
        monkeypatch.setattr(reader, "MAX_DOCUMENT_SIZE", 100)
        payload = _zip({"dive-1.uddf": _uddf("dive-1"), "dive-2.uddf": _uddf("dive-2")})
        response = client.post(PREVIEW_PATH, files=_files(payload, "watch-export.zip"))

        assert response.status_code == 413
        assert "converted in one import" in response.json()["detail"]

    def test_the_size_it_reports_is_rounded_up(self, signed_in: Any, client: TestClient, monkeypatch: Any) -> None:
        """Floored, an archive a byte over the cap reports the cap back at itself - "holds
        100 MB, and at most 100 MB are converted" - which reads as a refusal for no reason.
        `_spool_upload` already rounds its own figure up."""
        monkeypatch.setattr(reader, "MAX_DOCUMENT_SIZE", 1024 * 1024)
        payload = _zip({"dive-1.uddf": _uddf(), "pad.uddf": b"x" * (1024 * 1024)})

        detail = client.post(PREVIEW_PATH, files=_files(payload, "watch-export.zip")).json()["detail"]

        assert "holds 2 MB" in detail
        assert "at most 1 MB" in detail

    def test_the_sum_is_checked_before_a_member_is_read(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Off the directory, so a member that could not be inflated at all is still refused
        by size rather than by failing to read."""
        payload = _zip({"dive-1.uddf": _uddf("dive-1"), "dive-2.uddf": _uddf("dive-2")})
        read_calls: list[str] = []
        opened = zipfile.ZipFile.open

        def record(self: Any, name: Any, *args: Any, **kwargs: Any) -> Any:
            read_calls.append(str(name))
            return opened(self, name, *args, **kwargs)

        # Patched only now: `_zip` writes through the same method, so patching any earlier
        # records the test building its own fixture.
        monkeypatch.setattr(reader, "MAX_DOCUMENT_SIZE", 100)
        monkeypatch.setattr(zipfile.ZipFile, "open", record)

        assert client.post(PREVIEW_PATH, files=_files(payload, "watch-export.zip")).status_code == 413
        assert read_calls == []


class TestWhereTheConversionRuns:
    def test_the_conversion_is_not_on_the_event_loop(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Reading a FIT file is the same pure-Python decode `POST /dive/parse` hands to
        `run_in_threadpool` at about two seconds a megabyte, and a zip of them is that many
        times over. Inline in an `async def` one upload stalls every other request on the
        worker.

        Asserted against the thread the *rest* of `load_import` runs on rather than against
        the main thread: `TestClient` drives the app from a portal thread of its own, so
        "not the main thread" would pass even with the hop removed.
        """
        digest, convert = reader._digest, reader._convert
        seen: dict[str, str] = {}

        def record_spool(buffer: Any) -> Any:
            seen["spool"] = threading.current_thread().name
            return digest(buffer)

        def record_convert(buffer: Any, *, source_format: str | None) -> Any:
            seen["convert"] = threading.current_thread().name
            return convert(buffer, source_format=source_format)

        monkeypatch.setattr(reader, "_digest", record_spool)
        monkeypatch.setattr(reader, "_convert", record_convert)

        assert client.post(PREVIEW_PATH, files=_files(SSRF, "logbook.ssrf")).status_code == 200
        assert seen["convert"] != seen["spool"]


class TestWhenTheConversionFails:
    """A reader claimed the bytes and could not read them: 422 with the converter's own
    sentence, never a 500. The endpoint's whole contract is a 415/422/413 taxonomy, and the
    registry can grow an error class this build has never heard of at any pin bump - so the
    last arm is a bare `ConverterError`.
    """

    def test_a_malformed_source_document_is_422(self, signed_in: Any, client: TestClient) -> None:
        truncated = (
            b'<?xml version="1.0"?>\n<uddf xmlns="http://www.streit.cc/uddf/3.2/" version="3.2.2">\n<generator>\n'
        )
        response = client.post(PREVIEW_PATH, files=_files(truncated, "dives.uddf"))

        assert response.status_code == 422
        assert "could not be converted" in response.json()["detail"]

    def test_a_single_file_over_an_adapter_cap_is_413_and_is_not_called_an_archive(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """`SourceTooLargeError` is not the container's alone: the FIT reader raises it for
        one file past a hundred thousand messages, and there is nothing to split there."""

        def refuse(buffer: Any, *, source_format: str | None) -> Any:
            raise divejson.SourceTooLargeError("this FIT file holds more than 100,000 messages")

        monkeypatch.setattr(reader, "_convert", refuse)
        response = client.post(PREVIEW_PATH, files=_files(SSRF, "logbook.ssrf"))

        assert response.status_code == 413
        detail = response.json()["detail"]
        assert "archive" not in detail
        assert "Split it" not in detail

    def test_a_doctype_is_refused_as_422(self, signed_in: Any, client: TestClient) -> None:
        """The converter refuses a `<!DOCTYPE>` outright rather than expanding it, which is
        the entity-expansion guard this app already keeps on its own XML parsers."""
        payload = (
            b'<?xml version="1.0"?>\n<!DOCTYPE uddf [<!ENTITY x "y">]>\n'
            b'<uddf xmlns="http://www.streit.cc/uddf/3.2/" version="3.2.2">'
            b"<generator><name>x</name></generator></uddf>\n"
        )
        response = client.post(PREVIEW_PATH, files=_files(payload, "dives.uddf"))

        assert response.status_code == 422
        assert "DOCTYPE" in response.json()["detail"]

    def test_a_converter_that_writes_a_non_conforming_document_is_422(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """A bug in the converter rather than anything wrong with the file, so the message
        says so and the traceback goes to the log where somebody can act on it."""

        def refuse(buffer: Any, *, source_format: str | None) -> Any:
            raise divejson.NonConformingOutputError(
                [divejson.Issue("$", "something the writer should not have emitted")]
            )

        monkeypatch.setattr(reader, "_convert", refuse)
        response = client.post(PREVIEW_PATH, files=_files(SSRF, "logbook.ssrf"))

        assert response.status_code == 422
        assert "bug in the converter" in response.json()["detail"]

    def test_a_converted_document_this_app_cannot_read_is_422(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """The second backstop: the library validated its output against the format and this
        app's own envelope still refused it. Also not a 500."""

        def wrong_shape(buffer: Any, *, source_format: str | None) -> Any:
            return divejson.Conversion({"format": "uddf", "version": "3.2.2"}, ())

        monkeypatch.setattr(reader, "_convert", wrong_shape)
        response = client.post(PREVIEW_PATH, files=_files(SSRF, "logbook.ssrf"))

        assert response.status_code == 422
        assert "bug in the converter" in response.json()["detail"]


class TestTheConversionReport:
    def test_a_native_document_reports_no_conversion(self, signed_in: Any, client: TestClient) -> None:
        assert client.post(PREVIEW_PATH, files=_files()).json()["conversion"] is None

    def test_a_converted_upload_reports_what_could_not_be_carried(self, signed_in: Any, client: TestClient) -> None:
        body = client.post(PREVIEW_PATH, files=_files(SSRF, "logbook.ssrf")).json()

        report = body["conversion"]
        assert report["converter"] == {"name": "divejson", "version": divejson.__version__}
        assert report["groups_truncated"] == 0
        assert report["groups"], "the .ssrf above records no UTC offset, which is a finding"
        group = report["groups"][0]
        assert set(group) == {"kind", "message", "count", "wheres"}
        assert group["count"] >= len(group["wheres"])
        assert len(group["wheres"]) <= reader.MAX_CONVERSION_WHERES

    def test_a_kind_from_a_later_release_comes_back_as_itself(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """The invariant the whole block turns on. The converter's kinds went from three to
        four while this feature was being written, and the pin moves without a change here -
        so typing `kind` as an enum would turn a readable logbook into a 500 at *response
        serialisation* on some future Renovate PR, with nothing in between.
        """
        convert = reader._convert

        def with_an_unknown_kind(buffer: Any, *, source_format: str | None) -> Any:
            conversion = convert(buffer, source_format=source_format)
            invented = divejson.Note("dive/0", "a kind this build has never seen", "time-shifted")  # type: ignore[arg-type]
            return divejson.Conversion(conversion.document, (*conversion.notes, invented))

        monkeypatch.setattr(reader, "_convert", with_an_unknown_kind)
        response = client.post(PREVIEW_PATH, files=_files(SSRF, "logbook.ssrf"))

        assert response.status_code == 200
        kinds = [group["kind"] for group in response.json()["conversion"]["groups"]]
        assert "time-shifted" in kinds

    def test_groups_past_the_cap_are_counted_rather_than_listed(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """The converter's note list is unbounded and the report's is not, so the cap has to
        live on the grouping - which is also why these are not another `ImportNoteCode`."""
        convert = reader._convert

        def with_extra_findings(buffer: Any, *, source_format: str | None) -> Any:
            conversion = convert(buffer, source_format=source_format)
            extra = tuple(divejson.Note("dive/0", f"finding {n}", "dropped") for n in range(2))
            return divejson.Conversion(conversion.document, (*conversion.notes, *extra))

        monkeypatch.setattr(reader, "_convert", with_extra_findings)
        whole = client.post(PREVIEW_PATH, files=_files(SSRF, "logbook.ssrf")).json()["conversion"]

        monkeypatch.setattr(reader, "MAX_CONVERSION_GROUPS", 1)
        capped = client.post(PREVIEW_PATH, files=_files(SSRF, "logbook.ssrf")).json()["conversion"]

        assert len(whole["groups"]) > 1 and whole["groups_truncated"] == 0
        assert capped["groups"] == whole["groups"][:1], "what is listed is a prefix of the whole"
        assert capped["groups_truncated"] == len(whole["groups"]) - 1


class TestApply:
    def _token(self, payload: bytes = MINIMAL, user_uuid: uuid_pkg.UUID | None = None) -> str:
        return create_logbook_import_token(
            user_uuid=user_uuid if user_uuid is not None else CURRENT_USER_UUID,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def test_a_previewed_file_imports(self, signed_in: Any, client: TestClient) -> None:
        response = client.post(APPLY_PATH, files=_files(), data={"token": self._token()})

        assert response.status_code == 200
        assert response.json()["files"]["restored"] == 0

    def test_a_converted_file_imports_and_the_result_carries_the_conversion(
        self, signed_in: Any, client: TestClient
    ) -> None:
        """The token is minted over the *uploaded* bytes, not the converted document, so it
        names the file a diver picked - and apply converts again rather than replaying a
        stored result. The block is on the result as well as the preview because the result
        panel is what stays on screen."""
        response = client.post(APPLY_PATH, files=_files(SSRF, "logbook.ssrf"), data={"token": self._token(SSRF)})

        assert response.status_code == 200
        assert response.json()["conversion"]["format"] == "ssrf"

    def test_a_token_for_other_bytes_is_refused(self, signed_in: Any, client: TestClient) -> None:
        """Without this the apply would import whatever was uploaded second, and the report
        a diver approved would describe a different file."""
        response = client.post(APPLY_PATH, files=_files(), data={"token": self._token(b"different bytes")})

        assert response.status_code == 422
        assert "previewed" in response.json()["detail"]

    def test_a_token_from_another_account_is_refused(self, signed_in: Any, client: TestClient) -> None:
        response = client.post(APPLY_PATH, files=_files(), data={"token": self._token(user_uuid=uuid7())})

        assert response.status_code == 422

    def test_a_garbled_token_is_refused(self, signed_in: Any, client: TestClient) -> None:
        response = client.post(APPLY_PATH, files=_files(), data={"token": "not-a-token"})

        assert response.status_code == 422

    def _planned_check_in(self, monkeypatch: Any, argument: str = "check_in") -> list[Any]:
        """Wraps the stubbed planner to record the submission the route hands it."""
        seen: list[Any] = []
        stub = import_route.plan_import

        async def recording_plan(db: Any, **kwargs: Any) -> Any:
            seen.append(kwargs.get(argument))
            return await stub(db, **kwargs)

        monkeypatch.setattr(import_route, "plan_import", recording_plan)
        return seen

    def test_the_portrait_choice_reaches_the_planner(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        seen = self._planned_check_in(monkeypatch, "portrait")
        chosen = {"choice": "take", "account_sha256": "a" * 64}

        response = client.post(
            APPLY_PATH, files=_files(), data={"token": self._token(), "portrait": json.dumps(chosen)}
        )

        assert response.status_code == 200
        (choice,) = seen
        assert choice.model_dump() == chosen

    def test_an_apply_without_a_portrait_choice_chooses_nothing(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """What the web build before the portrait's row sends, beside its facts: the account
        keeps its portrait."""
        seen = self._planned_check_in(monkeypatch, "portrait")

        client.post(
            APPLY_PATH,
            files=_files(),
            data={"token": self._token(), "check_in_details": json.dumps({"born_on": "1988-04-12"})},
        )

        assert seen == [None]

    @pytest.mark.parametrize(
        "chosen",
        [
            {"choice": "swap", "account_sha256": None},
            {"choice": "take"},
            {"choice": "take", "account_sha256": "not a digest"},
            {"choice": "take", "account_sha256": None, "crop": {"x": 0}},
        ],
        ids=["an unknown choice", "no digest", "a malformed digest", "a member it does not know"],
    )
    def test_a_malformed_portrait_choice_is_a_422(
        self, signed_in: Any, client: TestClient, monkeypatch: Any, chosen: dict[str, Any]
    ) -> None:
        seen = self._planned_check_in(monkeypatch, "portrait")

        response = client.post(
            APPLY_PATH, files=_files(), data={"token": self._token(), "portrait": json.dumps(chosen)}
        )

        assert response.status_code == 422
        assert seen == []

    def test_the_confirmed_check_in_details_reach_the_planner(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        seen = self._planned_check_in(monkeypatch)
        submitted = {"born_on": "1988-04-12", "emergency_contact": {"name": "Grace Hopper"}, "insurance": None}

        response = client.post(
            APPLY_PATH, files=_files(), data={"token": self._token(), "check_in_details": json.dumps(submitted)}
        )

        assert response.status_code == 200
        (submission,) = seen
        assert submission.model_fields_set == {"born_on", "emergency_contact", "insurance"}
        assert submission.insurance is None

    def test_an_apply_without_them_submits_nothing(self, signed_in: Any, client: TestClient, monkeypatch: Any) -> None:
        """A client that never showed the section - the web build before this field existed
        among them - writes no detail at all."""
        seen = self._planned_check_in(monkeypatch)

        client.post(APPLY_PATH, files=_files(), data={"token": self._token()})

        assert seen == [None]

    @pytest.mark.parametrize(
        ("submitted", "loc"),
        [
            ({"emergency_contact": {"phone": "+1 202 555 0143"}}, ["emergency_contact", "name"]),
            ({"insurance": {"provider": " ", "number": "DE-4471902"}}, ["insurance", "provider"]),
            ({"born_on": "2999-01-01"}, ["born_on"]),
            ({"phone": "1" * 33}, ["phone"]),
            ({"emergency_contact": {"name": "G" * 256}}, ["emergency_contact", "name"]),
            ({"address": "Dahab"}, ["address"]),
        ],
    )
    def test_a_submission_patch_user_would_refuse_is_a_422_naming_the_field(
        self, signed_in: Any, client: TestClient, monkeypatch: Any, submitted: dict[str, Any], loc: list[str]
    ) -> None:
        """The bounds, the future-date guard and the anchor rule `PATCH /user` applies to the
        same columns, refused before anything is read or planned."""
        seen = self._planned_check_in(monkeypatch)

        response = client.post(
            APPLY_PATH, files=_files(), data={"token": self._token(), "check_in_details": json.dumps(submitted)}
        )

        assert response.status_code == 422
        assert [error["loc"] for error in response.json()["detail"]] == [["body", "check_in_details", *loc]]
        assert seen == []

    def test_the_caches_are_dropped_after_the_commit(
        self, signed_in: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Six of them, and the last two are the point: an import fills the dive-site and
        trip collections, whose list caches live inside their own routers rather than behind
        a helper - so a restored diver would otherwise get empty pages for up to the
        60-second list expiry."""
        called: list[str] = []

        def record(name: str) -> Any:
            async def invalidate(user_id: int) -> None:
                called.append(name)

            return invalidate

        for name in (
            "invalidate_dive_caches",
            "invalidate_certification_caches",
            "invalidate_course_caches",
            "invalidate_gear_caches",
            "invalidate_dive_site_caches",
            "invalidate_trip_caches",
        ):
            monkeypatch.setattr(import_route, name, record(name))

        client.post(APPLY_PATH, files=_files(), data={"token": self._token()})

        assert "invalidate_dive_site_caches" in called
        assert "invalidate_trip_caches" in called
        assert len(called) == 6
