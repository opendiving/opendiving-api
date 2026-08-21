"""Unit tests for `SecurityHeadersMiddleware`.

The gap these pin down: `deploy/Caddyfile` routes `/api/v1*`, `/admin*` and the docs
paths straight to the API, so the web app's headers never reach them, and neither Caddy
nor CRUDAdmin adds any of its own. `/admin` - a full create/update/delete interface over
every model - was therefore served framable, and a bring-your-own-proxy install got
nothing at all.
"""

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from src.app.middleware.security_headers_middleware import SecurityHeadersMiddleware


def build_app(cors: bool = False) -> FastAPI:
    """Mirrors `create_application`'s registration order, which is load-bearing: CORS
    first, security headers last, so the latter ends up outermost."""
    app = FastAPI()
    if cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://diving.example.com"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/downloads-a-file")
    async def downloads_a_file() -> Response:
        return Response(
            content=b"<xml/>",
            media_type="application/xml",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
            },
        )

    @app.post("/mutates")
    async def mutates() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/raises")
    async def raises() -> dict[str, str]:
        raise ValueError("boom")

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


class TestEveryResponseIsProtected:
    @pytest.mark.parametrize(("method", "path"), [("get", "/thing"), ("post", "/mutates")])
    def test_frame_ancestors_none(self, client: TestClient, method: str, path: str):
        response = getattr(client, method)(path)

        assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"

    def test_legacy_frame_header_is_sent_too(self, client: TestClient):
        """Redundant in every browser that supports `frame-ancestors`, kept for parity
        with the web app and for the scanners self-hosters run against their instance."""
        assert client.get("/thing").headers["X-Frame-Options"] == "DENY"

    def test_nosniff(self, client: TestClient):
        assert client.get("/thing").headers["X-Content-Type-Options"] == "nosniff"

    def test_a_404_is_covered(self, client: TestClient):
        """Not a formality: an unmatched path under `/admin*` is still HTML the panel's
        mount can render, and a 404 is a response like any other."""
        response = client.get("/no-such-path")

        assert response.status_code == 404
        assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


class TestHandlersKeepTheirOwnPolicy:
    def test_a_stricter_csp_is_not_downgraded(self, client: TestClient):
        """The binary-download endpoints send `default-src 'none'; sandbox`, which is far
        stricter than the default here. Overwriting it would be a regression."""
        response = client.get("/downloads-a-file")

        assert response.headers["Content-Security-Policy"] == ("default-src 'none'; sandbox; frame-ancestors 'none'")

    def test_those_responses_still_name_frame_ancestors(self, client: TestClient):
        """`frame-ancestors` does not fall back to `default-src`, so opting out of the
        middleware's CSP means opting out of frame protection unless the handler says so
        itself - which is why every download endpoint spells it out."""
        csp = client.get("/downloads-a-file").headers["Content-Security-Policy"]

        assert "frame-ancestors 'none'" in csp

    def test_the_legacy_header_is_still_added(self, client: TestClient):
        """No handler sets `X-Frame-Options`, so opting out of the CSP does not opt out
        of this one."""
        assert client.get("/downloads-a-file").headers["X-Frame-Options"] == "DENY"


class TestCorsPreflight:
    def test_is_covered_because_this_middleware_is_outermost(self):
        """`CORSMiddleware` answers `OPTIONS` itself and never calls the app, so a
        middleware registered *after* it - and therefore inside it - would miss the
        response entirely. `create_application` registers this one last for that reason;
        the same ordering is what covers the admin panel `main.py` mounts later."""
        client = TestClient(build_app(cors=True))

        response = client.options(
            "/thing",
            headers={
                "Origin": "https://diving.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

        assert response.status_code == 200
        assert response.headers["X-Frame-Options"] == "DENY"


class TestTheCspIsFrameAncestorsOnly:
    def test_it_constrains_no_resource(self, client: TestClient):
        """A `default-src` here would break CRUDAdmin's own templates, which style
        themselves inline and pull `htmx.min.js` from `/admin/static` and a webfont from
        `fonts.googleapis.com`. `frame-ancestors` is the whole policy on purpose."""
        csp = client.get("/thing").headers["Content-Security-Policy"]

        assert csp == "frame-ancestors 'none'"
        assert "default-src" not in csp
        assert "script-src" not in csp

    def test_no_hsts(self, client: TestClient):
        """HSTS is host-scoped, so the web app's header already pins `/admin` too.
        Sending it from here as well would put two controls on one behaviour and ignore
        `WEB_HSTS=off`, which is how a plain-HTTP LAN instance stays reachable."""
        assert "Strict-Transport-Security" not in client.get("/thing").headers


class TestTheOneResponseThisMisses:
    def test_an_unhandled_exception_500(self):
        """Starlette builds the stack as `ServerErrorMiddleware` -> user middleware ->
        the app, so the 500 it synthesises for an unhandled exception never passes
        through here. Recorded rather than fixed: that response is Starlette's own
        `Internal Server Error` string, with nothing on it worth framing, and moving the
        headers outside `ServerErrorMiddleware` means not using `add_middleware` at all.
        Everything the app itself raises - `HTTPException` and the classes in
        `core/exceptions/` - is handled *inside* this middleware and is covered."""
        client = TestClient(build_app(), raise_server_exceptions=False)

        response = client.get("/raises")

        assert response.status_code == 500
        assert "X-Frame-Options" not in response.headers
