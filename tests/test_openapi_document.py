"""Regression tests for the `/openapi.json` document's `info` block (`core/setup.py`).

This is the only OpenAPI document the app serves - the built-in route is disabled
(`openapi_url=None`), and `/docs` and `/redoc` both point at the one registered here. It
used to be rebuilt by a hand-rolled `get_openapi(title=..., version=..., routes=...)`
call, which published exactly three things and silently dropped every other field
`create_application` had configured: the description, the contact block and the license
never reached a reader, and `version` was FastAPI's own hardcoded default rather than the
running build's.

Both halves of that are asserted below, because neither shows up as a failure anywhere
else: a document that is missing its metadata still parses, still renders in Swagger, and
still generates clients.

Builds its own app via `create_application` like `test_cors.py` does, with
`apply_migrations_on_start=False` and no `TestClient` context manager, so none of this
needs a live Postgres or Redis - the document is assembled from the route table alone.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.app.api import router
from src.app.core.config import EnvironmentOption, settings
from src.app.core.setup import create_application

#: What `application.version` falls back to when nothing sets it, and so what this
#: document reported at every release until `version` was configured. It is a real
#: released version string, which is what made it hard to see: `0.1.0` looked like an
#: answer rather than a default.
_FASTAPI_DEFAULT_VERSION = "0.1.0"


def _document(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, Any]:
    """Serve `/openapi.json` from an app built against `settings` plus `overrides`.

    `ENVIRONMENT` is pinned rather than inherited: the docs router carries a superuser
    dependency on every environment but `local`, so an ambient `staging` would turn every
    assertion here into a 401 about something else entirely.
    """
    monkeypatch.setattr(settings, "ENVIRONMENT", EnvironmentOption.LOCAL)
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)

    app = create_application(router=router, settings=settings, apply_migrations_on_start=False)
    response = TestClient(app).get("/openapi.json")

    assert response.status_code == 200
    document: dict[str, Any] = response.json()
    return document


class TestVersion:
    def test_reports_the_running_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        info = _document(monkeypatch, APP_VERSION="9.9.9")["info"]

        assert info["version"] == "9.9.9"

    def test_does_not_report_fastapis_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The actual bug: `version` was never passed to `FastAPI()`, so the document
        answered `0.1.0` no matter what release was running.

        Distinct from the test above because that one would keep passing if somebody
        pinned the field to a literal that happened to match - this one moves the setting
        somewhere the default cannot follow.
        """
        info = _document(monkeypatch, APP_VERSION="47.0.0")["info"]

        assert info["version"] != _FASTAPI_DEFAULT_VERSION

    @pytest.mark.parametrize("absent", [None, ""])
    def test_an_uninstalled_source_tree_reads_unknown(
        self, monkeypatch: pytest.MonkeyPatch, absent: str | None
    ) -> None:
        """`APP_VERSION` is `None` where the distribution metadata isn't there to read.
        `info.version` is required by the spec, so it cannot be dropped, and `/api/v1/health`
        already calls that case `unknown`."""
        info = _document(monkeypatch, APP_VERSION=absent)["info"]

        assert info["version"] == "unknown"


class TestTitleAndDescription:
    def test_publishes_the_configured_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        info = _document(monkeypatch, APP_NAME="OpenDiving")["info"]

        assert info["title"] == "OpenDiving"

    def test_publishes_the_configured_description(self, monkeypatch: pytest.MonkeyPatch) -> None:
        info = _document(monkeypatch, APP_DESCRIPTION="Backend API for OpenDiving.")["info"]

        assert info["description"] == "Backend API for OpenDiving."


class TestContactAndLicense:
    def test_publishes_the_maintainer_contact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        info = _document(monkeypatch, CONTACT_NAME="Maintainer", CONTACT_EMAIL="maintainer@example.com")["info"]

        assert info["contact"] == {"name": "Maintainer", "email": "maintainer@example.com"}

    def test_publishes_the_license(self, monkeypatch: pytest.MonkeyPatch) -> None:
        info = _document(monkeypatch, LICENSE_NAME="AGPL-3.0-or-later")["info"]

        assert info["license"] == {"name": "AGPL-3.0-or-later"}

    def test_omits_contact_and_license_entirely_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The default for everyone: `src/.env.example` ships `LICENSE` and the contact
        pair alike commented out.

        This is the case that has to be built rather than passed through, and the `200`
        `_document` asserts is half the test. A `license_info` handed over as
        `{"name": None}` does not publish a null - it fails the document's own validation,
        because `name` is required wherever a license object appears, so `/openapi.json`
        500s outright for anyone who never set `LICENSE`. An all-`None` contact fails
        softer, publishing an empty `"contact": {}`.
        """
        info = _document(monkeypatch, CONTACT_NAME=None, CONTACT_EMAIL=None, LICENSE_NAME=None)["info"]

        assert "contact" not in info
        assert "license" not in info

    def test_publishes_only_the_half_of_the_contact_that_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both settings are independent and independently optional."""
        info = _document(monkeypatch, CONTACT_NAME=None, CONTACT_EMAIL="maintainer@example.com")["info"]

        assert info["contact"] == {"email": "maintainer@example.com"}


class TestRoutes:
    def test_still_publishes_the_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The other half of the document, and the half a metadata fix could quietly cost:
        `app.routes` holds lazy `_IncludedRouter` wrappers rather than a flat route list,
        so whatever assembles this has to be something that flattens them."""
        document = _document(monkeypatch)

        assert "/api/v1/health" in document["paths"]
