"""Tests for the four `/export/*` routes (`api/v1/export.py`).

What is actually route behaviour, as opposed to writer behaviour, is a short list, and
it is all here: authentication, the headers that make the response a download rather than
something a cache keeps a copy of, and the rate limit. The documents themselves are
covered by `test_export_uddf.py`, `test_export_tabular.py`, `test_export_json.py` and
`test_export_archive.py`.

Two of these are security assertions rather than conveniences. `no-store` is the one that
keeps a zip full of certification-card scans out of a browser's disk cache, and the
absence of any user parameter is what makes "the caller's own data" true by construction
rather than by an ownership check that could be got wrong.
"""

import inspect
import io
import json
import zipfile
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import export as export_route
from src.app.core.config import settings
from src.app.core.exceptions.http_exceptions import RateLimitException
from src.app.core.setup import create_application
from tests.helpers.export import full_bundle

PATHS = (
    "/api/v1/export/divejson",
    "/api/v1/export/uddf",
    "/api/v1/export/csv",
    "/api/v1/export/archive",
)

CURRENT_USER = {"id": 1, "uuid": full_bundle().user.uuid, "username": "ada", "is_superuser": False}


@pytest.fixture(scope="module")
def export_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`, like `test_cors.py`.

    The shared `client` fixture opens `src.app.main`'s app, whose startup hook connects
    to Postgres - which would make these route tests part of the database-backed subset
    for no reason: nothing below the route is real here anyway.
    """
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def client(export_app: Any) -> Generator[TestClient]:
    with TestClient(export_app) as test_client:
        yield test_client
    export_app.dependency_overrides = {}


@pytest.fixture
def signed_in(export_app: Any, monkeypatch: Any) -> Any:
    """The four routes with their database and rate limiter stubbed out.

    Everything below the route is exercised elsewhere; what is under test here is the
    response the route builds around it.
    """
    export_app.dependency_overrides[get_current_user] = lambda: CURRENT_USER

    async def fake_load_bundle(db: Any, *, user_id: int) -> Any:
        return full_bundle()

    async def fake_load_profile(db: Any, *, recording_id: int) -> None:
        return None

    async def fake_load_dive_file(db: Any, *, file_id: int) -> None:
        return None

    async def fake_load_certification_file(db: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(export_route, "load_export_bundle", fake_load_bundle)
    monkeypatch.setattr(export_route, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("src.app.services.export.uddf.load_profile", fake_load_profile)
    monkeypatch.setattr("src.app.services.export.envelope.load_profile", fake_load_profile)
    monkeypatch.setattr("src.app.services.export.archive.load_dive_file", fake_load_dive_file)
    monkeypatch.setattr("src.app.services.export.archive.load_certification_file", fake_load_certification_file)
    yield
    export_app.dependency_overrides = {}


class TestAuthentication:
    @pytest.mark.parametrize("path", PATHS)
    def test_an_anonymous_caller_gets_401(self, client: TestClient, path: str):
        assert client.get(path).status_code == 401

    @pytest.mark.parametrize("path", PATHS)
    def test_no_route_takes_any_parameter_at_all(self, client: TestClient, path: str):
        """The bearer token names the only account there is to export.

        There being nothing to pass is the guarantee every route in the API makes; here
        it is the whole of one, with no id to probe with. Asserted against the published
        contract rather than the signature, so a path, query or header parameter added
        later fails here whatever shape it takes.
        """
        operation = client.get("/openapi.json").json()["paths"][path]["get"]
        assert operation.get("parameters", []) == []

    @pytest.mark.parametrize(
        "endpoint",
        (
            export_route.export_divejson,
            export_route.export_uddf,
            export_route.export_csv,
            export_route.export_archive,
        ),
    )
    def test_no_handler_names_a_user(self, endpoint: Any):
        """The same claim at the source, so it also holds for anything FastAPI would not
        publish as a parameter."""
        assert not {"username", "user_uuid", "user_id"} & set(inspect.signature(endpoint).parameters)


class TestResponseHeaders:
    @pytest.mark.parametrize(
        ("path", "extension", "media_type"),
        [
            # The DiveJSON row is the wire-identity assertion the format needs: the media
            # type spec §8 registers and the extension it recommends, both served by the
            # rules every other download here already goes through.
            (PATHS[0], "divejson", "application/vnd.dive+json"),
            (PATHS[1], "uddf", "application/xml"),
            (PATHS[2], "csv", "text/csv; charset=utf-8"),
            (PATHS[3], "zip", "application/zip"),
        ],
    )
    def test_each_route_offers_a_dated_download_of_its_own_type(
        self, client: TestClient, signed_in, path, extension, media_type
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"] == media_type
        disposition = response.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="opendiving-ada-')
        assert disposition.endswith(f'.{extension}"')

    @pytest.mark.parametrize("path", PATHS)
    def test_nothing_is_allowed_to_keep_a_copy(self, client: TestClient, signed_in, path):
        """`no-store`, not `private`: the archive includes certification-card scans, which
        have no business sitting in a browser's disk cache."""
        assert client.get(path).headers["cache-control"] == "no-store"

    @pytest.mark.parametrize("path", PATHS)
    def test_the_length_is_known_so_a_browser_can_show_progress(self, client: TestClient, signed_in, path):
        response = client.get(path)
        assert int(response.headers["content-length"]) == len(response.content)

    @pytest.mark.parametrize("path", PATHS)
    def test_the_content_type_may_not_be_re_sniffed(self, client: TestClient, signed_in, path):
        assert client.get(path).headers["x-content-type-options"] == "nosniff"


class TestBodies:
    def test_the_divejson_route_serves_a_divejson_document(self, client: TestClient, signed_in):
        body = client.get(PATHS[0]).content
        assert body.startswith(b'{"format": "divejson",\n"version": "1.0",\n')

    def test_the_archive_member_is_the_same_writer_as_the_standalone_route(self, client: TestClient, signed_in):
        """One writer, two surfaces - the way `dives.uddf` and `GET /export/uddf` already
        work. Not byte-identical, and deliberately so: inside the zip every stored file
        gains an `archive_path` pointing at the member holding its bytes, which is the one
        thing a bare document has nothing to say about. Everything else must match.
        """
        standalone = json.loads(client.get(PATHS[0]).content)
        with zipfile.ZipFile(io.BytesIO(client.get(PATHS[3]).content)) as archive:
            member = json.loads(archive.read("logbook.divejson"))

        for document in (standalone, member):
            document.pop("exported_at")
            for dive in document["dives"]:
                for recording in dive["recordings"]:
                    for file in recording.get("source_files", []):
                        file.pop("archive_path", None)
            for certification in document["certifications"]:
                for side in ("front_file", "back_file"):
                    certification.get(side, {}).pop("archive_path", None)
        assert standalone == member

    def test_the_uddf_route_serves_a_uddf_document(self, client: TestClient, signed_in):
        body = client.get(PATHS[1]).content
        assert body.startswith(b'<?xml version="1.0" encoding="utf-8"?>')
        assert b'<uddf xmlns="http://www.streit.cc/uddf/3.2/"' in body

    def test_the_csv_route_serves_the_flat_dive_sheet(self, client: TestClient, signed_in):
        body = client.get(PATHS[2]).content
        assert body.startswith("﻿".encode())
        assert b"dive_number,date,time,utc_offset" in body

    def test_the_archive_route_serves_a_zip(self, client: TestClient, signed_in):
        assert client.get(PATHS[3]).content.startswith(b"PK\x03\x04")


class TestRateLimit:
    @pytest.mark.parametrize("path", PATHS)
    def test_every_route_is_limited(self, client: TestClient, signed_in, monkeypatch, path):
        """One archive request reads every blob the caller owns, and nothing else in the
        API does that."""
        monkeypatch.setattr(
            export_route, "enforce_rate_limit", AsyncMock(side_effect=RateLimitException("Too many requests."))
        )
        assert client.get(path).status_code == 429

    @pytest.mark.parametrize("path", PATHS)
    def test_the_limit_is_checked_before_any_data_is_read(self, client: TestClient, signed_in, monkeypatch, path):
        """Otherwise the throttle would only start after the expensive part had run."""
        loaded = False

        async def spy_load_bundle(db: Any, *, user_id: int) -> Any:
            nonlocal loaded
            loaded = True
            return full_bundle()

        monkeypatch.setattr(export_route, "load_export_bundle", spy_load_bundle)
        monkeypatch.setattr(
            export_route, "enforce_rate_limit", AsyncMock(side_effect=RateLimitException("Too many requests."))
        )
        client.get(path)
        assert not loaded
