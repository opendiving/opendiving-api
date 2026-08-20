"""Regression tests for CORS handling (`core/setup.py`), on both axes.

The web app is served from a different origin than the API and sends requests
with an `Authorization` header and/or JSON bodies plus cookies, all of which make
the browser preflight with `OPTIONS` before the real request. Without
`CORSMiddleware` configured, FastAPI has no `OPTIONS` handler for any route, so
every preflight - and therefore every real cross-origin request - 405s.

The other axis is what comes back: a cross-origin response exposes only the
CORS-safelisted headers unless `expose_headers` names more, which is why the
download filename needs a test of its own.

Builds its own app via `create_application` (rather than importing `src.app.main`'s
`app`/using the `client` fixture from `conftest.py`) with `apply_migrations_on_start=
False`, so this doesn't require a live Postgres connection just to exercise
middleware registration.
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from src.app.api import router
from src.app.core.config import settings
from src.app.core.setup import create_application

_PREFLIGHT_HEADERS = {
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type",
}


@pytest.fixture(scope="module")
def cors_client() -> Generator[TestClient]:
    app = create_application(router=router, settings=settings, apply_migrations_on_start=False)
    with TestClient(app) as client:
        yield client


def test_preflight_request_is_allowed_for_configured_frontend_origin(cors_client: TestClient) -> None:
    response = cors_client.options(
        "/api/v1/auth/refresh", headers={"Origin": settings.FRONTEND_URL, **_PREFLIGHT_HEADERS}
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == settings.FRONTEND_URL
    assert response.headers["access-control-allow-credentials"] == "true"


def test_preflight_request_is_rejected_for_unrecognized_origin(cors_client: TestClient) -> None:
    response = cors_client.options(
        "/api/v1/auth/refresh", headers={"Origin": "http://evil.example.com", **_PREFLIGHT_HEADERS}
    )

    assert "access-control-allow-origin" not in response.headers


def test_content_disposition_is_exposed_to_the_browser(cors_client: TestClient) -> None:
    """`Content-Disposition` is not CORS-safelisted, so JS can't read the download
    filename the file and `/export/*` endpoints send unless it's named here."""
    response = cors_client.get("/api/v1/export/csv", headers={"Origin": settings.FRONTEND_URL})

    # An allowed origin gets the header on any response the app returns, so without this
    # the test would keep passing off a 404 if the route were ever renamed away.
    assert response.status_code == 401
    exposed = [header.strip().lower() for header in response.headers["access-control-expose-headers"].split(",")]
    assert "content-disposition" in exposed
