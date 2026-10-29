"""The ceiling on a request body (`middleware/body_size_limit_middleware.py`).

FastAPI reads a route's body before authentication, so these send no credential: a body the
ceiling admits is read in full and then refused with a 401, and one it refuses never reaches
the 401. Builds its own app without entering it, as `test_cors.py` does, so nothing here
needs Postgres or Redis.
"""

import json
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from src.app.api import router
from src.app.core.config import settings
from src.app.core.setup import create_application
from src.app.middleware.body_size_limit_middleware import (
    MAX_REQUEST_BODY_SIZE,
    UNLIMITED_PATHS,
    BodySizeLimitMiddleware,
)
from src.app.services.certification_files import MAX_CARD_FILE_SIZE
from src.app.services.dive_files import MAX_DIVE_FILE_SIZE
from src.app.services.user_pictures import MAX_PICTURE_UPLOAD_SIZE
from tests.helpers.routes import iter_api_routes

MB = 1024 * 1024
BOUNDARY = "limit-test-boundary"


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture(scope="module")
def http(app: FastAPI) -> TestClient:
    # Not entered, so no lifespan: every request here is answered before a dependency needs
    # the database.
    return TestClient(app)


def _multipart_head(field: str = "file") -> bytes:
    return (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{field}"; filename="a.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode()


def _multipart(payload: bytes) -> bytes:
    return _multipart_head() + payload + f"\r\n--{BOUNDARY}--\r\n".encode()


def _scope(path: str, *, headers: list[tuple[bytes, bytes]]) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
    }


class _Recorder:
    """The ASGI `send` side, keeping what the app answered."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)

    @property
    def _start(self) -> Message:
        return next(message for message in self.messages if message["type"] == "http.response.start")

    @property
    def status(self) -> int:
        return int(self._start["status"])

    @property
    def headers(self) -> dict[bytes, bytes]:
        return dict(self._start["headers"])

    @property
    def body(self) -> bytes:
        return b"".join(message["body"] for message in self.messages if message["type"] == "http.response.body")


def _streamed(head: bytes, *, chunk: int, total: int) -> tuple[Receive, Callable[[], int]]:
    """A chunked body: `head`, then `chunk`-byte messages up to `total`, then the end."""
    sent = 0
    started = False

    async def receive() -> Message:
        nonlocal sent, started
        if not started:
            started = True
            sent = len(head)
            return {"type": "http.request", "body": head, "more_body": True}
        if sent >= total:
            return {"type": "http.disconnect"}
        size = min(chunk, total - sent)
        sent += size
        return {"type": "http.request", "body": b"x" * size, "more_body": sent < total}

    return receive, lambda: sent


@pytest.mark.parametrize("upload_limit", [MAX_PICTURE_UPLOAD_SIZE, MAX_CARD_FILE_SIZE, MAX_DIVE_FILE_SIZE])
def test_the_ceiling_admits_every_upload_a_route_accepts(upload_limit: int) -> None:
    """Raising an upload limit past this would have the largest uploads refused here, with
    the route's own limit never reached."""
    assert MAX_REQUEST_BODY_SIZE >= upload_limit + MB


@pytest.mark.asyncio
async def test_a_declared_length_over_the_ceiling_is_refused_unread() -> None:
    async def never_called(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("the app was called")

    async def unread() -> Message:
        raise AssertionError("the body was read")

    send = _Recorder()
    over = str(MAX_REQUEST_BODY_SIZE + 1).encode()

    await BodySizeLimitMiddleware(never_called)(
        _scope("/api/v1/user/avatar", headers=[(b"content-length", over)]), unread, send
    )

    assert send.status == 413
    assert "Request body too large" in json.loads(send.body)["detail"]


def test_a_413_carries_the_cors_headers(http: TestClient) -> None:
    response = http.put(
        "/api/v1/user/avatar",
        content=_multipart(b"x" * MAX_REQUEST_BODY_SIZE),
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "Origin": settings.frontend_origin},
    )

    assert response.status_code == 413
    assert "Request body too large" in response.json()["detail"]
    assert response.headers["access-control-allow-origin"] == settings.frontend_origin


@pytest.mark.asyncio
async def test_a_streamed_body_over_the_ceiling_is_a_413_not_a_500(app: FastAPI) -> None:
    """No `Content-Length`, so it is counted as it arrives. The refusal is raised from inside
    the framework's form reading, and has to come out of it as this 413 rather than its 400
    for an unreadable body or a 500."""
    receive, sent = _streamed(_multipart_head(), chunk=MB, total=MAX_REQUEST_BODY_SIZE + 5 * MB)
    send = _Recorder()
    headers = [
        (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode()),
        (b"transfer-encoding", b"chunked"),
        (b"origin", settings.frontend_origin.encode()),
    ]

    await app(_scope("/api/v1/user/avatar", headers=headers), receive, send)

    assert send.status == 413
    assert "Request body too large" in json.loads(send.body)["detail"]
    assert send.headers[b"access-control-allow-origin"] == settings.frontend_origin.encode()
    # Stopped at the ceiling, not drained to the end.
    assert sent() <= MAX_REQUEST_BODY_SIZE + MB


def test_the_largest_upload_a_route_accepts_reaches_the_route(http: TestClient) -> None:
    response = http.put(
        "/api/v1/user/avatar",
        content=_multipart(b"x" * MAX_PICTURE_UPLOAD_SIZE),
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_streamed_body_under_the_ceiling_arrives_whole() -> None:
    received: list[bytes] = []

    async def reader(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            received.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    receive, sent = _streamed(b"", chunk=MB, total=MAX_REQUEST_BODY_SIZE)
    send = _Recorder()

    await BodySizeLimitMiddleware(reader)(_scope("/api/v1/user/avatar", headers=[]), receive, send)

    assert send.status == 204
    assert sum(map(len, received)) == sent() == MAX_REQUEST_BODY_SIZE


def test_the_unlimited_paths_are_the_import_routes(app: FastAPI) -> None:
    """An exemption naming a path no route serves exempts nothing, and the import routes are
    then held to a ceiling a fraction of their own."""
    posts = {route.path for route in iter_api_routes(app) if route.method == "POST"}

    assert UNLIMITED_PATHS == {"/api/v1/import/logbook/preview", "/api/v1/import/logbook"}
    assert posts >= UNLIMITED_PATHS


@pytest.mark.parametrize("path", sorted(UNLIMITED_PATHS))
def test_the_import_routes_are_not_held_to_the_ceiling(http: TestClient, path: str) -> None:
    """They authenticate before they read, so an anonymous body over the ceiling is a 401."""
    response = http.post(
        path,
        content=_multipart(b"x" * (MAX_REQUEST_BODY_SIZE + MB)),
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
    )

    assert response.status_code == 401
