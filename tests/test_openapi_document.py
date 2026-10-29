"""Regression tests for the `/api/v1/openapi.json` document (`core/setup.py`).

This is the only OpenAPI document the app serves - the built-in route is disabled
(`openapi_url=None`), and `/docs` points at the one registered here,
which is served anonymously on every environment, `production` included. It
used to be rebuilt by a hand-rolled `get_openapi(title=..., version=..., routes=...)`
call, which published exactly three things and silently dropped every other field
`create_application` had configured: the description, the contact block and the license
never reached a reader, and `version` was FastAPI's own hardcoded default rather than the
running build's.

Both halves of that are asserted below, because neither shows up as a failure anywhere
else: a document that is missing its metadata still parses, still renders on `/docs`, and
still generates clients.

Builds its own app via `create_application` like `test_cors.py` does, with
`apply_migrations_on_start=False` and no `TestClient` context manager, so none of this
needs a live Postgres or Redis - the document is assembled from the route table alone.
"""

from typing import Any
from unittest.mock import ANY

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from src.app.api import router
from src.app.core.config import EnvironmentOption, settings
from src.app.core.openapi_access import CREDENTIALS, EXTENSION
from src.app.core.setup import OPENAPI_URL, create_application

#: What `application.version` falls back to when nothing sets it, and so what this
#: document reported at every release until `version` was configured. It is a real
#: released version string, which is what made it hard to see: `0.1.0` looked like an
#: answer rather than a default.
_FASTAPI_DEFAULT_VERSION = "0.1.0"


def _get(monkeypatch: pytest.MonkeyPatch, path: str, **overrides: Any) -> Response:
    """Read `path` with no credential from an app built against `settings` plus `overrides`."""
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)

    app = create_application(router=router, settings=settings, apply_migrations_on_start=False)
    response: Response = TestClient(app).get(path)
    return response


def _document(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, Any]:
    """Serve the document from an app built against `settings` plus `overrides`."""
    response = _get(monkeypatch, OPENAPI_URL, **overrides)

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
        info = _document(monkeypatch, APP_NAME="Example Dive Log")["info"]

        assert info["title"] == "Example Dive Log"

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
        because `name` is required wherever a license object appears, so the document
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


class TestWhereItIsServed:
    @pytest.mark.parametrize("environment", list(EnvironmentOption))
    def test_anonymously_on_every_environment(
        self, monkeypatch: pytest.MonkeyPatch, environment: EnvironmentOption
    ) -> None:
        """`production` is the case that matters: it serves no `/docs`, so this
        document is the only description of the API a caller of a production instance can
        read."""
        response = _get(monkeypatch, OPENAPI_URL, ENVIRONMENT=environment)

        assert response.status_code == 200
        assert "/api/v1/health" in response.json()["paths"]

    def test_is_publicly_cacheable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No credential and a safe method, so `ClientCacheMiddleware` labels it like any
        anonymous read: the document only changes when a deploy does."""
        response = _get(monkeypatch, OPENAPI_URL, ENVIRONMENT=EnvironmentOption.PRODUCTION)

        assert response.headers["cache-control"].startswith("public, max-age=")

    @pytest.mark.parametrize("environment", list(EnvironmentOption))
    def test_not_at_the_root(self, monkeypatch: pytest.MonkeyPatch, environment: EnvironmentOption) -> None:
        assert _get(monkeypatch, "/openapi.json", ENVIRONMENT=environment).status_code == 404

    def test_the_interactive_page_renders_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = _get(monkeypatch, "/docs", ENVIRONMENT=EnvironmentOption.LOCAL)

        assert response.status_code == 200
        assert OPENAPI_URL in response.text

    def test_production_serves_no_interactive_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _get(monkeypatch, "/docs", ENVIRONMENT=EnvironmentOption.PRODUCTION).status_code == 404


class TestSecurityScheme:
    def test_is_a_bearer_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A docs page builds its credential form from this: an `oauth2` password flow offers
        a username and password that no route accepts, where `http` bearer offers a token
        field that takes an access token or an API key alike."""
        document = _document(monkeypatch)

        assert document["components"]["securitySchemes"] == {
            "bearer": {"type": "http", "scheme": "bearer", "description": ANY}
        }
        assert document["paths"]["/api/v1/user"]["get"]["security"] == [{"bearer": []}]


_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


class TestCredentials:
    def test_every_operation_names_its_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A docs page filtering on the list drops an operation that lacks one, so a gap here is
        a route that silently disappears from the reference."""
        paths = _document(monkeypatch)["paths"]

        unnamed = [
            f"{method.upper()} {path}"
            for path, operations in paths.items()
            for method, operation in operations.items()
            if method in _METHODS and not operation.get(EXTENSION)
        ]
        assert not unnamed

    @pytest.mark.parametrize(
        ("method", "path", "expected"),
        [
            ("get", "/api/v1/health", list(CREDENTIALS)),
            ("get", "/api/v1/user", ["session", "superuser", "api-key-read", "api-key-write"]),
            ("patch", "/api/v1/user", ["session", "superuser", "api-key-write"]),
            # `refuse_api_keys`: a key cannot delete its own account or mint its successor.
            ("delete", "/api/v1/user", ["session", "superuser"]),
            ("get", "/api/v1/user/api-keys", ["session", "superuser"]),
            ("post", "/api/v1/user/api-keys", ["session", "superuser"]),
            # `bearer_scheme` alone: the access token is read, a key is never resolved.
            ("post", "/api/v1/auth/logout", ["session", "superuser"]),
            ("get", "/api/v1/admin/stats", ["superuser"]),
        ],
    )
    def test_follow_the_dependencies_that_enforce_them(
        self, monkeypatch: pytest.MonkeyPatch, method: str, path: str, expected: list[str]
    ) -> None:
        operation = _document(monkeypatch)["paths"][path][method]

        assert operation[EXTENSION] == expected

    def test_a_secured_operation_never_lists_anonymous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        paths = _document(monkeypatch)["paths"]

        contradicting = [
            f"{method.upper()} {path}"
            for path, operations in paths.items()
            for method, operation in operations.items()
            if method in _METHODS and bool(operation.get("security")) == ("anonymous" in operation[EXTENSION])
        ]
        assert not contradicting
