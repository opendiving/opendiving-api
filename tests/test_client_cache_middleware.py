"""Unit tests for `ClientCacheMiddleware`.

The bug these pin down: the middleware decided "is this response user-specific?" purely
from whether the *request* carried an `Authorization` header. That is exactly backwards
for the endpoints that mint credentials - `POST /auth/email/verify`, `/auth/google`,
`/auth/complete`, `/auth/refresh` are unauthenticated by nature, since the caller has no
token yet, and each returns one in the body. All four were being labelled
`public, max-age=60`.

And its second shape, which is what the `Cookie` cases below are for: a bearer token is
not the only credential the app takes. The CRUDAdmin panel authenticates with a
`session_id` cookie, so every signed-in request to it is a safe method with no
`Authorization` header. Its model pages were saved by CRUDAdmin setting `no-store` on
them itself, which is not a thing to depend on - it skips its own header on the login
path, on `/static/` and on every redirect, and those were going out `public, max-age=60`
to a signed-in admin.
"""

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from src.app.middleware.client_cache_middleware import ClientCacheMiddleware


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    # `add_middleware` is typed against the middleware's own __init__ signature, which
    # starlette can't infer through BaseHTTPMiddleware's `app` parameter.
    app.add_middleware(ClientCacheMiddleware, max_age=60)  # type: ignore[arg-type]

    @app.get("/public-thing")
    async def public_thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/mints-a-token")
    async def mints_a_token() -> dict[str, str]:
        return {"access_token": "super-secret", "token_type": "bearer"}

    @app.get("/opts-out")
    async def opts_out(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "private, no-store"
        return {"email": "diver@example.com"}

    @app.delete("/removes-a-thing")
    async def removes_a_thing() -> dict[str, str]:
        return {"message": "gone"}

    @app.get("/echoes-auth")
    async def echoes_auth(request: Request) -> dict[str, bool]:
        return {"authenticated": "Authorization" in request.headers}

    @app.get("/sets-its-own-vary")
    async def sets_its_own_vary(response: Response) -> dict[str, str]:
        """Stands in for the CORS middleware, which adds `Vary: Origin` on the way out."""
        response.headers["Vary"] = "Origin"
        return {"ok": "yes"}

    # Shaped like the admin panel: a sub-application mounted on the outer app, so it is
    # behind this middleware exactly as `main.app.mount(CRUD_ADMIN_MOUNT_PATH, ...)` is,
    # and authenticated by a cookie rather than a header.
    panel = FastAPI()

    @panel.get("/user")
    async def panel_user_list(request: Request) -> dict[str, str]:
        if "session_id" not in request.cookies:
            return {"page": "login"}
        return {"page": "users", "row": "diver@example.com"}

    app.mount("/admin", panel)

    return TestClient(app)


class TestUnsafeMethodsAreNeverPublic:
    @pytest.mark.parametrize(("method", "path"), [("post", "/mints-a-token"), ("delete", "/removes-a-thing")])
    def test_no_store(self, client: TestClient, method: str, path: str):
        response = getattr(client, method)(path)

        assert response.headers["Cache-Control"] == "private, no-store"

    def test_a_token_response_is_not_publicly_cacheable(self, client: TestClient):
        """The specific regression: an anonymous POST that hands back an access token."""
        response = client.post("/mints-a-token")

        assert "super-secret" in response.text
        assert "public" not in response.headers["Cache-Control"]


class TestSafeAnonymousRequests:
    def test_are_public(self, client: TestClient):
        response = client.get("/public-thing")

        assert response.headers["Cache-Control"] == "public, max-age=60"

    def test_head_is_public_too(self, client: TestClient):
        assert client.head("/public-thing").headers["Cache-Control"] == "public, max-age=60"


class TestAuthenticatedRequests:
    def test_are_private_even_on_a_get(self, client: TestClient):
        response = client.get("/echoes-auth", headers={"Authorization": "Bearer abc"})

        assert response.headers["Cache-Control"] == "private, no-store"


class TestExplicitHeadersWin:
    def test_an_endpoint_can_opt_out(self, client: TestClient):
        """How the two `/verify/check` GETs protect themselves - they are anonymous and
        side-effect-free, i.e. exactly the shape this middleware marks public, but carry
        a token in the query string and return an email address.
        """
        response = client.get("/opts-out")

        assert response.headers["Cache-Control"] == "private, no-store"


class TestCookieAuthenticatedRequests:
    """The regression this file grew for: `Cookie` is a credential too."""

    def test_a_cookie_makes_a_get_private(self, client: TestClient):
        response = client.get("/public-thing", headers={"Cookie": "session_id=abc"})

        assert response.headers["Cache-Control"] == "private, no-store"

    def test_a_signed_in_admin_page_is_not_publicly_cacheable(self, client: TestClient):
        response = client.get("/admin/user", headers={"Cookie": "session_id=abc"})

        assert "diver@example.com" in response.text
        assert response.headers["Cache-Control"] == "private, no-store"

    def test_the_whole_cookie_header_counts_not_a_list_of_names(self, client: TestClient):
        """A known-session-cookie-names allowlist would be one more thing to keep in step
        with every auth surface, and getting it wrong fails open. This errs the other way.
        """
        response = client.get("/public-thing", headers={"Cookie": "theme=dark"})

        assert response.headers["Cache-Control"] == "private, no-store"


class TestPublicResponsesDeclareWhatTheyVaryOn:
    """The decision is made from request headers, so a shared cache has to be told which
    ones - otherwise it answers a credentialed request from the anonymous entry it stored
    for the same URL.
    """

    def test_vary_names_both_credential_headers(self, client: TestClient):
        vary = client.get("/public-thing").headers["Vary"]

        assert "Cookie" in vary
        assert "Authorization" in vary

    def test_an_existing_vary_is_merged_not_overwritten(self, client: TestClient):
        """`Vary: Origin` arrives from the CORS middleware on every cross-origin read."""
        vary = client.get("/sets-its-own-vary").headers["Vary"]

        assert "Origin" in vary
        assert "Cookie" in vary

    def test_every_credential_header_is_named(self, client: TestClient):
        """Derived from `CREDENTIAL_HEADERS`, not typed out beside it. A header added to
        the set but missing from the `Vary` would still make its own request private
        while leaving shared caches free to answer it from the anonymous entry - the
        fail-open direction this header exists to close.
        """
        vary = {value.strip() for value in client.get("/public-thing").headers["Vary"].split(",")}

        assert ClientCacheMiddleware.CREDENTIAL_HEADERS <= vary


class TestTheRealAuthRoutes:
    """Asserted against the actual route table rather than the fixture above, so this
    keeps holding as endpoints are added or moved.
    """

    def test_no_token_minting_route_is_a_safe_method(self):
        from src.app.api.v1.auth import router

        minting = {
            "/auth/email/verify",
            "/auth/email/verify-code",
            "/auth/google",
            "/auth/complete",
            "/auth/refresh",
            "/auth/passkey/verify",
        }
        for route in router.routes:
            if getattr(route, "path", None) in minting:
                assert route.methods == {"POST"}, f"{route.path} is no longer POST-only - re-check its caching"

    def test_the_token_carrying_gets_set_their_own_cache_control(self):
        """These two are GETs, so the method check alone doesn't save them."""
        import inspect

        from src.app.api.v1.auth import check_email_link
        from src.app.api.v1.users import check_email_change_link

        for endpoint in (check_email_link, check_email_change_link):
            source = inspect.getsource(endpoint)
            assert 'Cache-Control"] = "private, no-store"' in source, (
                f"{endpoint.__name__} takes a token in its query string and must opt out of public caching"
            )
