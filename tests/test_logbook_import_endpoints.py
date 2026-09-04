"""The two `/import/divejson*` routes (`api/v1/logbook_import.py`).

What is route behaviour rather than importer behaviour is a short list, and it is all
here: authentication, the rate limit, the status code each refusal comes back as, and the
preview token that ties an apply to the bytes a diver was shown a report for. The importing
itself is `test_logbook_import.py`, against real Postgres.

The token pair is the one thing worth testing at this layer specifically. It is what makes
"you approved *this* file" true, and its two failure modes - a token for another account
and a token for other bytes - are indistinguishable from a successful import if either
check is dropped.
"""

import hashlib
import io
import json
import uuid as uuid_pkg
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock

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

PREVIEW_PATH = "/api/v1/import/divejson/preview"
APPLY_PATH = "/api/v1/import/divejson"

CURRENT_USER_UUID = uuid7()
CURRENT_USER = {"id": 1, "uuid": CURRENT_USER_UUID, "username": "ada", "is_superuser": False}

MINIMAL = json.dumps({"format": "divejson", "version": "1.0", "exported_at": "2026-04-17T11:49:23+02:00"}).encode()


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
            notes=[],
            notes_dropped=0,
            files_referenced=0,
            files_restored=0,
            files_not_contained=0,
            files_skipped=0,
            unresolved_aphia_ids=[],
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


class TestTheListCacheNamesAgree:
    """`cache_invalidation` sweeps two list caches it cannot import.

    `_dive_site_cache` and `_trip_cache` are module-private `OwnedResourceCache` instances
    inside their routers, and a service importing a route module would invert the layering.
    So the *shape* is shared through `OwnedResourceCache.list_cache_pattern` and the
    resource names are spelled out - and this is what stops those spellings drifting from
    the caches they are meant to sweep, which nothing else would notice.
    """

    def test_the_patterns_match_the_real_caches(self) -> None:
        from src.app.api.v1.dive_sites import _dive_site_cache
        from src.app.api.v1.trips import _trip_cache
        from src.app.core.utils.owned_resource_cache import OwnedResourceCache

        assert OwnedResourceCache.list_cache_pattern(_dive_site_cache.resource_name, 7) == "user_7_dive_sites:*"
        assert OwnedResourceCache.list_cache_pattern(_trip_cache.resource_name, 7) == "user_7_trips:*"
