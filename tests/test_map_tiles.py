"""Map tiles: which squares there are, the renderer's contract from the API's side, and the route
that finds or draws one for every account.

Every draw here goes to `StubRenderer`, which speaks the renderer's contract and refuses a
body outside it with the `400` the real one answers - so a body that grew a field, or lost
one, fails these tests rather than the first draw on a running instance.

The store is shared by every account and persists in the suite's database, so each test draws
under a signature of its own (`renderer`) and reads back only the rows carrying it.
"""

import asyncio
import io
import json
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from functools import cache as memoize
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_user
from src.app.core.config import EnvironmentOption, Settings, normalize_map_renderer_url, settings
from src.app.core.db.database import async_engine, local_session
from src.app.core.setup import create_application
from src.app.core.utils import cache
from src.app.core.worker.functions import purge_unserved_map_tiles
from src.app.models.map_tile import MapTile
from src.app.models.user import User
from src.app.schemas.dive import DiveListItem
from src.app.schemas.dive_site import DiveSiteRead
from src.app.schemas.map_tile import MapTheme
from src.app.schemas.trip import TripRead
from src.app.services import blob_store, map_renderer, map_tiles
from src.app.services.map_tiles import MAX_ZOOM, UNSERVED_RETENTION, Tile, tile_at
from tests.conftest import db_available
from tests.helpers.generators import create_user

RENDERER_URL = "http://renderer.test"
_CLAIMS = "map-tile:claim:"


def _signature() -> str:
    """A signature no other test, and no earlier run, has drawn under."""
    return uuid7().hex * 2


def _breaks_the_contract(body: object) -> bool:
    """Whether a `POST /render` body is outside the renderer's contract: `kind` `tile`, the
    theme, and `z`, `x`, `y` as integers inside the grid at a zoom of at most 9 - and no other
    field."""
    if not isinstance(body, dict) or set(body) != {"kind", "theme", "z", "x", "y"}:
        return True
    if body["kind"] != "tile" or body["theme"] not in ("light", "dark"):
        return True
    z, x, y = body["z"], body["x"], body["y"]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (z, x, y)):
        return True
    return not (0 <= z <= 9 and 0 <= x < 2**z and 0 <= y < 2**z)


@memoize
def _webp(shade: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1024, 1024), (shade % 256, 120, 160)).save(buffer, "WEBP")
    return buffer.getvalue()


class StubRenderer:
    """The renderer's contract, in a `MockTransport`: `GET /signature`, and `POST /render`
    answering a 1024x1024 WebP signed with `X-Map-Signature` - or a `400` for a body outside
    the contract, or whatever `status` says.

    `gate`, when set, holds every draw until it opens, which is how a test arranges for
    requests to overlap one.
    """

    def __init__(self) -> None:
        self.signature = _signature()
        # The signature a draw says it drew with, when a test redeploys it mid-request.
        self.draws_with: str | None = None
        self.status = 200
        self.down = False
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.renders: list[dict[str, Any]] = []
        self.refused: list[object] = []
        self.calls = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.down:
            raise httpx.ConnectError("Connection refused", request=request)
        if request.method == "GET" and request.url.path == "/signature":
            return httpx.Response(200, json={"signature": self.signature})
        if request.method == "POST" and request.url.path == "/render":
            body = json.loads(request.content)
            if _breaks_the_contract(body):
                self.refused.append(body)
                return httpx.Response(400, json={"error": "body outside the contract"})
            self.renders.append(body)
            drawn = len(self.renders)
            self.started.set()
            if self.gate is not None:
                await self.gate.wait()
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "busy"})
            return httpx.Response(
                200,
                # A fresh object per draw, as a real response's body is.
                content=bytes(bytearray(_webp(drawn))),
                headers={"Content-Type": "image/webp", "X-Map-Signature": self.draws_with or self.signature},
            )
        return httpx.Response(404)


class FakeRedis:
    """What the rate limiter and the draw claim ask of Redis, in a dict."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.expiries: dict[str, int] = {}

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(
        self, key: str, value: object, ex: int | None = None, px: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value if isinstance(value, bytes) else str(value).encode()
        if px is not None:
            self.expiries[key] = px // 1000
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self.expiries.get(key, -1)

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, b"0")) + 1
        self.values[key] = str(value).encode()
        return value

    async def delete(self, *keys: str) -> int:
        return sum(self.values.pop(key, None) is not None for key in keys)

    async def scan(self, cursor: int, match: str, count: int) -> tuple[int, list[str]]:
        return 0, [key for key in self.values if fnmatch(key, match)]

    def claims(self) -> list[str]:
        return [key for key in self.values if key.startswith(_CLAIMS)]


@pytest.fixture
def renderer(monkeypatch: pytest.MonkeyPatch) -> StubRenderer:
    """This instance with a renderer, which is the stub, and its signature already learned."""
    stub = StubRenderer()
    monkeypatch.setattr(settings, "MAP_RENDERER_URL", RENDERER_URL)
    monkeypatch.setattr(
        map_renderer,
        "_client",
        lambda *, timeout: httpx.AsyncClient(
            base_url=RENDERER_URL, transport=httpx.MockTransport(stub.handle), timeout=timeout
        ),
    )
    monkeypatch.setattr(map_renderer, "_signature", stub.signature)
    monkeypatch.setattr(map_renderer, "_unreachable", False)
    monkeypatch.setattr(map_tiles, "_WAIT_POLL_SECONDS", 0.01)
    return stub


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(cache, "client", fake)
    return fake


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


def _url(theme: str = "light", z: int = 9, x: int = 304, y: int = 214) -> str:
    """A tile's URL; the default is the zoom-9 square over Dahab."""
    return f"/api/v1/map-tiles/{theme}/{z}/{x}/{y}"


# -------------- which tiles there are --------------


class TestTheAddress:
    def test_the_ceiling_is_the_webs_deepest_fit(self) -> None:
        """The renderer refuses past the same zoom, and pins it on its side: a change here is
        a change to both."""
        assert MAX_ZOOM == 9

    @pytest.mark.parametrize(
        ("theme", "z", "x", "y"),
        [("light", 0, 0, 0), ("dark", 0, 0, 0), ("light", 1, 1, 1), ("dark", 9, 511, 511), ("light", 9, 0, 511)],
    )
    def test_every_square_of_the_grid_to_the_ceiling_is_one(self, theme: str, z: int, x: int, y: int) -> None:
        assert tile_at(theme, z, x, y) == Tile(MapTheme(theme), z, x, y)

    @pytest.mark.parametrize(
        ("theme", "z", "x", "y"),
        [
            pytest.param("light", 10, 0, 0, id="past the ceiling"),
            pytest.param("light", -1, 0, 0, id="a negative zoom"),
            pytest.param("light", 9, 512, 0, id="x of 2^z"),
            pytest.param("light", 9, 0, 512, id="y of 2^z"),
            pytest.param("light", 0, 1, 0, id="x past the world at zoom 0"),
            pytest.param("light", 3, -1, 0, id="a negative x"),
            pytest.param("light", 3, 0, -1, id="a negative y"),
            pytest.param("sepia", 0, 0, 0, id="no such theme"),
            pytest.param("Light", 0, 0, 0, id="a theme in the wrong case"),
        ],
    )
    def test_anything_else_is_none(self, theme: str, z: int, x: int, y: int) -> None:
        assert tile_at(theme, z, x, y) is None

    def test_the_body_is_the_contracts(self) -> None:
        tile = Tile(MapTheme.DARK, 9, 304, 214)
        assert tile.body() == {"kind": "tile", "theme": "dark", "z": 9, "x": 304, "y": 214}
        assert not _breaks_the_contract(tile.body())


# -------------- the settings --------------


def _settings(**overrides: Any) -> Settings:
    """A `Settings` without the developer's own values for what it asserts, as
    `test_config_safety.py` builds one."""
    base = {
        "SECRET_KEY": "test-secret-key-for-testing-only",
        "ENVIRONMENT": EnvironmentOption.LOCAL,
        "CRUD_ADMIN_ENABLED": False,
        "SMTP_HOST": None,
        "EMAIL_FROM_ADDRESS": None,
    }
    return Settings(**{**base, **overrides})


_RENDERER_SETTINGS = (
    "MAP_RENDERER_URL",
    "MAP_RENDERER_TIMEOUT",
    "MAP_RENDERER_LIMIT_WINDOW_SECONDS",
    "MAP_RENDERER_DRAW_LIMIT_PER_USER",
    "MAP_RENDERER_REQUEST_LIMIT_PER_USER",
)


class TestTheSettings:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", ""),
            ("   ", ""),
            # The form a Render Blueprint's `fromService … property: hostport` supplies.
            ("opendiving-map-renderer:10000", "http://opendiving-map-renderer:10000"),
            ("http://map-renderer:3001/", "http://map-renderer:3001"),
            ("https://renderer.example", "https://renderer.example"),
        ],
    )
    def test_it_takes_a_host_port_or_a_url(self, raw: str, expected: str) -> None:
        assert normalize_map_renderer_url(raw) == expected
        assert _settings(MAP_RENDERER_URL=raw).MAP_RENDERER_URL == expected

    @pytest.mark.parametrize("raw", ["ftp://renderer:21", "http://", "://renderer"])
    def test_anything_else_fails_startup_naming_the_setting(self, raw: str) -> None:
        with pytest.raises(ValueError, match="MAP_RENDERER_URL"):
            _settings(MAP_RENDERER_URL=raw)

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_draw_needs_a_deadline(self, value: float) -> None:
        with pytest.raises(ValueError, match="MAP_RENDERER_TIMEOUT"):
            _settings(MAP_RENDERER_TIMEOUT=value)

    def test_the_template_carries_every_one_and_passes_with_them(self) -> None:
        template = (Path(__file__).resolve().parents[1] / "src" / ".env.example").read_text()
        values: dict[str, str] = {}
        for name in _RENDERER_SETTINGS:
            (line,) = [line for line in template.splitlines() if line.startswith(f"# {name}=")]
            values[name] = line.partition("=")[2].strip('"')

        configured = _settings(**values)

        assert configured.MAP_RENDERER_URL == ""
        assert configured.map_tiles is False
        for name in _RENDERER_SETTINGS[1:]:
            assert getattr(configured, name) == Settings.model_fields[name].default, name

    def test_no_map_picture_setting_is_left(self) -> None:
        template = (Path(__file__).resolve().parents[1] / "src" / ".env.example").read_text()
        assert "MAP_PICTURE" not in template
        assert not [name for name in Settings.model_fields if name.startswith("MAP_PICTURE")]


# -------------- the renderer's contract, from the client's side --------------


class TestTheRendererClient:
    @pytest.mark.asyncio
    async def test_the_signature_is_learned_and_kept_through_an_outage(
        self, renderer: StubRenderer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = renderer.signature
        monkeypatch.setattr(map_renderer, "_signature", None)
        await map_renderer.refresh_signature()
        assert map_renderer.current_signature() == first

        renderer.down = True
        await map_renderer.refresh_signature()
        assert map_renderer.current_signature() == first

        renderer.down, renderer.signature = False, _signature()
        await map_renderer.refresh_signature()
        assert map_renderer.current_signature() == renderer.signature

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", [{"signature": "A" * 64}, {"signature": "a" * 63}, {}, ["a" * 64]])
    async def test_a_signature_outside_the_contract_is_none(
        self, renderer: StubRenderer, monkeypatch: pytest.MonkeyPatch, answer: object
    ) -> None:
        monkeypatch.setattr(
            map_renderer,
            "_client",
            lambda *, timeout: httpx.AsyncClient(
                base_url=RENDERER_URL, transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=answer))
            ),
        )
        with pytest.raises(map_renderer.RendererUnavailable):
            await map_renderer.fetch_signature()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param(httpx.Response(503), id="a full queue"),
            pytest.param(httpx.Response(400), id="a refused body"),
            pytest.param(
                httpx.Response(
                    200, content=b"not a tile", headers={"Content-Type": "image/webp", "X-Map-Signature": "a" * 64}
                ),
                id="bytes that are no WebP",
            ),
            pytest.param(
                httpx.Response(200, content=_webp(1), headers={"Content-Type": "image/webp"}), id="no signature"
            ),
            pytest.param(
                httpx.Response(
                    200, content=_webp(1), headers={"Content-Type": "image/png", "X-Map-Signature": "a" * 64}
                ),
                id="another type",
            ),
        ],
    )
    async def test_anything_but_a_signed_webp_is_a_failure(
        self, renderer: StubRenderer, monkeypatch: pytest.MonkeyPatch, answer: httpx.Response
    ) -> None:
        monkeypatch.setattr(
            map_renderer,
            "_client",
            lambda *, timeout: httpx.AsyncClient(
                base_url=RENDERER_URL, transport=httpx.MockTransport(lambda _request: answer)
            ),
        )
        with pytest.raises(map_renderer.RendererUnavailable):
            await map_renderer.render(Tile(MapTheme.LIGHT, 0, 0, 0).body(), timeout=5)

    @pytest.mark.asyncio
    async def test_a_draw_past_its_deadline_is_a_failure(self, renderer: StubRenderer) -> None:
        renderer.gate = asyncio.Event()
        with pytest.raises(map_renderer.RendererUnavailable):
            await map_renderer.render(Tile(MapTheme.LIGHT, 0, 0, 0).body(), timeout=0.05)

    @pytest.mark.asyncio
    async def test_the_refresh_learns_the_signature_in_the_background(
        self, renderer: StubRenderer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(map_renderer, "_signature", None)
        map_renderer.start_signature_refresh()
        try:
            for _ in range(200):
                if map_renderer.current_signature() is not None:
                    break
                await asyncio.sleep(0.01)
            assert map_renderer.current_signature() == renderer.signature
        finally:
            await map_renderer.stop_signature_refresh()
        assert map_renderer._refresher is None

    @pytest.mark.asyncio
    async def test_no_refresh_starts_without_a_renderer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "MAP_RENDERER_URL", "")
        map_renderer.start_signature_refresh()
        assert map_renderer._refresher is None
        await map_renderer.stop_signature_refresh()


# -------------- the route, and what no longer exists --------------


@pytest.fixture(scope="module")
def map_app() -> Any:
    """The real application, so the tile's headers pass through the middleware that rewrites
    headers on the way out, as `test_user_pictures.py` does it."""
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


@pytest.fixture
def anyone(map_app: Any) -> Generator[None]:
    """A signed-in caller who has no row: enough for every answer given before the route
    reads anything."""
    map_app.dependency_overrides[get_current_user] = lambda: {
        "id": -1,
        "uuid": uuid7(),
        "username": "nobody",
        "is_superuser": False,
    }
    try:
        yield
    finally:
        map_app.dependency_overrides = {}


class TestBeforeAnythingIsRead:
    """What the route answers without touching the database, so these run on a cold checkout."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            pytest.param(_url(z=10, x=0, y=0), id="zoom 10"),
            pytest.param(_url(z=9, x=512, y=0), id="x of 2^z"),
            pytest.param(_url(z=9, x=0, y=512), id="y of 2^z"),
            pytest.param(_url(z=-1, x=0, y=0), id="a negative zoom"),
            pytest.param(_url(theme="sepia"), id="no such theme"),
        ],
    )
    async def test_a_square_that_is_no_tile_is_a_404_and_reaches_no_renderer(
        self, map_app: Any, anyone: None, renderer: StubRenderer, redis: FakeRedis, path: str
    ) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=map_app), base_url="http://api.test") as client:
            response = await client.get(path)

        assert response.status_code == 404
        assert response.json() == {"detail": "No such map tile"}
        assert renderer.calls == 0
        assert redis.values == {}, "a request that names no tile counted against a limit"

    @pytest.mark.asyncio
    async def test_without_a_renderer_every_tile_is_a_404_and_the_config_says_so(
        self, map_app: Any, anyone: None, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "MAP_RENDERER_URL", "")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=map_app), base_url="http://api.test") as client:
            tile = await client.get(_url())
            config = await client.get("/api/v1/config")

        assert tile.status_code == 404
        assert tile.json() == {"detail": "This instance draws no map tiles"}
        assert config.json()["map_tiles"] is False
        assert "map_pictures" not in config.json()

    @pytest.mark.asyncio
    async def test_with_one_the_config_says_tiles_are_drawn(
        self, map_app: Any, anyone: None, renderer: StubRenderer
    ) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=map_app), base_url="http://api.test") as client:
            assert (await client.get("/api/v1/config")).json()["map_tiles"] is True

    def test_the_route_names_a_square_and_no_record(self, map_app: Any) -> None:
        """So every record and every account whose map covers a tile asks one URL for it."""
        operation = map_app.openapi()["paths"]["/api/v1/map-tiles/{theme}/{z}/{x}/{y}"]["get"]
        assert sorted((parameter["in"], parameter["name"]) for parameter in operation["parameters"]) == [
            ("path", "theme"),
            ("path", "x"),
            ("path", "y"),
            ("path", "z"),
        ]

    def test_no_map_picture_is_served_or_named(self, map_app: Any) -> None:
        assert not [path for path in map_app.openapi()["paths"] if "map-picture" in path]
        for schema in (DiveListItem, TripRead, DiveSiteRead):
            assert "map_picture" not in schema.model_fields, schema.__name__


# -------------- against Postgres: the route, the store, the cron --------------


@pytest_asyncio.fixture
async def api(
    map_app: Any, db: Session, redis: FakeRedis, volume: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, User]]:
    """A signed-in diver's client over the app, on this test's event loop.

    The route opens sessions from the module-level `local_session`, and so does a draw, so the
    app's engine is disposed on either side: a pooled asyncpg connection belongs to the loop
    that opened it.
    """
    diver = create_user(db)
    map_app.dependency_overrides[get_current_user] = lambda: _as(diver)
    await async_engine.dispose()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=map_app), base_url="http://api.test") as client:
            yield client, diver
        await map_tiles._await_pending_draws()
    finally:
        map_app.dependency_overrides = {}
        await async_engine.dispose()


def _rows(db: Session, signature: str) -> list[MapTile]:
    db.expire_all()
    return list(db.execute(select(MapTile).where(MapTile.signature == signature).order_by(MapTile.id)).scalars())


def _sign_in(map_app: Any, user: User) -> None:
    map_app.dependency_overrides[get_current_user] = lambda: _as(user)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheRoute:
    @pytest.mark.asyncio
    async def test_a_miss_is_drawn_stored_and_kept_and_the_next_request_is_the_stored_tile(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, _diver = api

        first = await client.get(_url("dark"))
        second = await client.get(_url("dark"))

        assert first.status_code == second.status_code == 200
        assert first.headers["content-type"] == "image/webp"
        assert first.headers["cache-control"] == second.headers["cache-control"] == "private, max-age=300"
        assert first.headers["x-content-type-options"] == "nosniff"
        assert first.headers["content-security-policy"] == "default-src 'none'; sandbox; frame-ancestors 'none'"
        assert first.headers["content-disposition"].startswith("attachment")
        assert first.content == second.content == _webp(1)
        assert first.headers["etag"] == second.headers["etag"]
        assert renderer.renders == [{"kind": "tile", "theme": "dark", "z": 9, "x": 304, "y": 214}]
        (row,) = _rows(db, renderer.signature)
        assert (row.theme, row.z, row.x, row.y) == ("dark", 9, 304, 214)
        assert row.storage_key.startswith(f"{map_tiles.BLOB_KIND}/")
        assert blob_store.exists(row.storage_key)

    @pytest.mark.asyncio
    async def test_two_accounts_asking_one_tile_draw_once_and_share_one_row(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer, map_app: Any
    ) -> None:
        client, _diver = api
        mine = await client.get(_url())
        _sign_in(map_app, create_user(db))
        theirs = await client.get(_url())

        assert mine.status_code == theirs.status_code == 200
        assert mine.content == theirs.content
        assert len(renderer.renders) == 1
        assert len(_rows(db, renderer.signature)) == 1

    @pytest.mark.asyncio
    async def test_an_etag_still_current_is_a_304(
        self, api: tuple[httpx.AsyncClient, User], renderer: StubRenderer
    ) -> None:
        client, _diver = api
        drawn = await client.get(_url())

        again = await client.get(_url(), headers={"If-None-Match": drawn.headers["etag"]})

        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["cache-control"] == "private, max-age=300"
        assert again.headers["etag"] == drawn.headers["etag"]
        assert len(renderer.renders) == 1

    @pytest.mark.asyncio
    async def test_the_themes_and_the_squares_are_tiles_of_their_own(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, _diver = api

        for path in (_url("light"), _url("dark"), _url("light", x=305)):
            assert (await client.get(path)).status_code == 200

        assert [(body["theme"], body["x"]) for body in renderer.renders] == [
            ("light", 304),
            ("dark", 304),
            ("light", 305),
        ]
        assert len(_rows(db, renderer.signature)) == 3

    @pytest.mark.asyncio
    async def test_the_whole_world_is_one_tile(
        self, api: tuple[httpx.AsyncClient, User], renderer: StubRenderer
    ) -> None:
        client, _diver = api

        assert (await client.get(_url(z=0, x=0, y=0))).status_code == 200
        assert renderer.renders == [{"kind": "tile", "theme": "light", "z": 0, "x": 0, "y": 0}]

    @pytest.mark.asyncio
    async def test_a_failed_draw_is_a_503_and_stores_nothing(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer, redis: FakeRedis
    ) -> None:
        client, _diver = api
        renderer.status = 503

        failed = await client.get(_url())

        assert failed.status_code == 503
        assert _rows(db, renderer.signature) == []
        assert not redis.claims(), "the claim outlived the draw"

        renderer.status = 200
        assert (await client.get(_url())).status_code == 200

    @pytest.mark.asyncio
    async def test_a_renderer_redeployed_mid_request_stores_the_tile_under_the_signature_it_drew_with(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, _diver = api
        asked_under = renderer.signature
        renderer.draws_with = _signature()

        response = await client.get(_url())

        assert response.status_code == 200
        assert map_renderer.current_signature() == renderer.draws_with
        assert _rows(db, asked_under) == []
        (row,) = _rows(db, renderer.draws_with)
        assert response.headers["etag"] == f'"{row.sha256}"'
        assert (await client.get(_url())).status_code == 200
        assert len(renderer.renders) == 1

    @pytest.mark.asyncio
    async def test_concurrent_misses_draw_once(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer, map_app: Any
    ) -> None:
        """Three cards on one coast at once, two of them another account's: one draw, three tiles."""
        client, _diver = api
        renderer.gate = asyncio.Event()

        requests = asyncio.gather(*(client.get(_url()) for _ in range(3)))
        await asyncio.wait_for(renderer.started.wait(), timeout=5)
        _sign_in(map_app, create_user(db))
        late = asyncio.ensure_future(client.get(_url()))
        await asyncio.sleep(0.1)
        renderer.gate.set()
        responses = [*await asyncio.wait_for(requests, timeout=5), await asyncio.wait_for(late, timeout=5)]

        assert [response.status_code for response in responses] == [200, 200, 200, 200]
        assert len({response.content for response in responses}) == 1
        assert len(renderer.renders) == 1
        assert len(_rows(db, renderer.signature)) == 1

    @pytest.mark.asyncio
    async def test_without_redis_every_miss_draws_for_itself_and_one_row_survives(
        self,
        api: tuple[httpx.AsyncClient, User],
        db: Session,
        renderer: StubRenderer,
        volume: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failing open costs renders and never a tile: the loser's file goes after its commit."""
        client, _diver = api
        monkeypatch.setattr(cache, "client", None)
        renderer.gate = asyncio.Event()

        requests = asyncio.gather(client.get(_url()), client.get(_url()))
        for _ in range(500):
            if len(renderer.renders) == 2:
                break
            await asyncio.sleep(0.01)
        renderer.gate.set()
        responses = await asyncio.wait_for(requests, timeout=5)
        await blob_store._await_pending_removals()

        assert [response.status_code for response in responses] == [200, 200]
        assert len(renderer.renders) == 2
        (row,) = _rows(db, renderer.signature)
        assert [path.relative_to(volume).as_posix() for path in volume.rglob("*") if path.is_file()] == [
            row.storage_key
        ]

    @pytest.mark.asyncio
    async def test_a_wait_past_the_deadline_is_a_503(
        self,
        api: tuple[httpx.AsyncClient, User],
        renderer: StubRenderer,
        redis: FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another process holds the claim and never stores: this request gives up when its own
        deadline does, rather than drawing beside it."""
        client, _diver = api
        monkeypatch.setattr(settings, "MAP_RENDERER_TIMEOUT", 0.1)
        tile = tile_at("light", 9, 304, 214)
        assert tile is not None
        redis.values[map_tiles._claim_key(renderer.signature, tile)] = b"1"

        response = await client.get(_url())

        assert response.status_code == 503
        assert renderer.renders == []

    @pytest.mark.asyncio
    async def test_a_claimant_whose_client_goes_still_stores_its_tile(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer, redis: FakeRedis
    ) -> None:
        _client, diver = api
        renderer.gate = asyncio.Event()

        async with local_session() as session:
            request = asyncio.create_task(
                map_tiles.find_or_draw(session, user_id=diver.id, tile=Tile(MapTheme.DARK, 2, 1, 3), if_none_match=None)
            )
            await asyncio.wait_for(renderer.started.wait(), timeout=5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        renderer.gate.set()
        await map_tiles._await_pending_draws()

        (row,) = _rows(db, renderer.signature)
        assert (row.theme, row.z, row.x, row.y) == ("dark", 2, 1, 3)
        assert blob_store.exists(row.storage_key)
        assert not redis.claims()

    @pytest.mark.asyncio
    async def test_a_claimant_cancelled_before_its_draw_lets_the_claim_go(
        self,
        api: tuple[httpx.AsyncClient, User],
        renderer: StubRenderer,
        redis: FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancelled between taking the claim and starting the draw - here, in the look it takes
        once more after claiming. A claim left standing would hold every other request for the
        tile until it lapsed, past their own deadlines."""
        _client, diver = api
        reached, held = asyncio.Event(), asyncio.Event()
        release = map_tiles.release_read_transaction

        async def held_once_claimed(session: Any) -> None:
            await release(session)
            if redis.claims():
                reached.set()
                await held.wait()

        monkeypatch.setattr(map_tiles, "release_read_transaction", held_once_claimed)

        async with local_session() as session:
            request = asyncio.create_task(
                map_tiles.find_or_draw(
                    session, user_id=diver.id, tile=Tile(MapTheme.LIGHT, 0, 0, 0), if_none_match=None
                )
            )
            await asyncio.wait_for(reached.wait(), timeout=5)
            assert redis.claims()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

        assert not redis.claims()
        assert renderer.renders == []

    @pytest.mark.asyncio
    async def test_the_draw_limit_counts_draws_and_never_hits_or_waits(
        self,
        api: tuple[httpx.AsyncClient, User],
        renderer: StubRenderer,
        redis: FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _diver = api
        monkeypatch.setattr(settings, "MAP_RENDERER_DRAW_LIMIT_PER_USER", 1)

        renderer.gate = asyncio.Event()
        drawer_and_waiter = asyncio.gather(client.get(_url()), client.get(_url()))
        await asyncio.wait_for(renderer.started.wait(), timeout=5)
        await asyncio.sleep(0.05)
        renderer.gate.set()
        assert [response.status_code for response in await drawer_and_waiter] == [200, 200]

        refused = await client.get(_url(x=305))
        stored = await client.get(_url())

        assert refused.status_code == 429
        assert stored.status_code == 200
        assert len(renderer.renders) == 1
        assert not redis.claims(), "a refused claim stood"

    @pytest.mark.asyncio
    async def test_the_request_limit_counts_every_request_hits_and_waits_included(
        self,
        api: tuple[httpx.AsyncClient, User],
        db: Session,
        renderer: StubRenderer,
        redis: FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
        map_app: Any,
    ) -> None:
        """Past it, a stored tile and an unstored one answer alike - the 429 comes before
        anything is read - so a probe of what this instance has drawn runs at this rate."""
        client, _diver = api
        monkeypatch.setattr(settings, "MAP_RENDERER_REQUEST_LIMIT_PER_USER", 3)

        renderer.gate = asyncio.Event()
        drawer_and_waiter = asyncio.gather(client.get(_url()), client.get(_url()))
        await asyncio.wait_for(renderer.started.wait(), timeout=5)
        await asyncio.sleep(0.05)
        renderer.gate.set()
        assert [response.status_code for response in await drawer_and_waiter] == [200, 200]
        assert (await client.get(_url())).status_code == 200

        stored = await client.get(_url())
        unstored = await client.get(_url(x=305))

        assert stored.status_code == unstored.status_code == 429
        assert stored.json() == unstored.json()
        assert len(renderer.renders) == 1
        assert len(_rows(db, renderer.signature)) == 1

        _sign_in(map_app, create_user(db))
        assert (await client.get(_url())).status_code == 200, "one account's count reached another's"

    @pytest.mark.asyncio
    async def test_a_row_whose_file_is_gone_is_drawn_again(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, _diver = api
        await client.get(_url())
        (gone,) = _rows(db, renderer.signature)
        gone_key = gone.storage_key
        await blob_store.delete(gone_key)

        response = await client.get(_url())

        assert response.status_code == 200
        assert response.content == _webp(2)
        (row,) = _rows(db, renderer.signature)
        assert row.storage_key != gone_key
        assert blob_store.exists(row.storage_key)

    @pytest.mark.asyncio
    async def test_a_found_tile_marks_its_use_at_most_once_a_day(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, _diver = api
        await client.get(_url())
        (row,) = _rows(db, renderer.signature)
        recently, long_ago = datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC) - timedelta(days=3)

        row.last_served_at = recently
        db.commit()
        await client.get(_url())
        assert _rows(db, renderer.signature)[0].last_served_at == recently

        _rows(db, renderer.signature)[0].last_served_at = long_ago
        db.commit()
        await client.get(_url(), headers={"If-None-Match": f'"{row.sha256}"'})
        assert _rows(db, renderer.signature)[0].last_served_at > recently

    @pytest.mark.asyncio
    async def test_an_unknown_signature_is_asked_for_and_a_silent_renderer_is_a_503(
        self, api: tuple[httpx.AsyncClient, User], renderer: StubRenderer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _diver = api
        monkeypatch.setattr(map_renderer, "_signature", None)

        renderer.down = True
        assert (await client.get(_url())).status_code == 503
        renderer.down = False
        assert (await client.get(_url())).status_code == 200
        assert map_renderer.current_signature() == renderer.signature


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheUnservedPurge:
    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        """The cron opens its own session from the module-level `local_session`."""
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @staticmethod
    async def _stored(db: Session, signature: str, *, x: int, served: datetime) -> MapTile:
        key = blob_store.new_key(map_tiles.BLOB_KIND, sha256="d" * 64)
        await blob_store.put(key, _webp(9))
        row = MapTile(
            z=9, x=x, y=0, theme="light", signature=signature, storage_key=key, sha256="d" * 64, last_served_at=served
        )
        db.add(row)
        db.commit()
        return row

    @pytest.mark.asyncio
    async def test_a_tile_unserved_past_the_retention_goes_with_its_file_and_a_served_one_stays(
        self, db: Session, volume: Path
    ) -> None:
        signature = _signature()
        now = datetime.now(UTC)
        unserved = await self._stored(db, signature, x=0, served=now - UNSERVED_RETENTION - timedelta(hours=1))
        served = await self._stored(db, signature, x=1, served=now - UNSERVED_RETENTION + timedelta(days=1))
        unserved_key, served_key = unserved.storage_key, served.storage_key

        await purge_unserved_map_tiles({})
        await blob_store._await_pending_removals()

        assert [row.storage_key for row in _rows(db, signature)] == [served_key]
        assert not blob_store.exists(unserved_key)
        assert blob_store.exists(served_key)

        # Idempotent: a second run finds nothing of these to take.
        await purge_unserved_map_tiles({})
        assert [row.storage_key for row in _rows(db, signature)] == [served_key]

    @pytest.mark.asyncio
    async def test_it_works_through_more_than_a_batch(
        self, db: Session, volume: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(map_tiles, "PURGE_BATCH_SIZE", 2)
        signature = _signature()
        long_ago = datetime.now(UTC) - UNSERVED_RETENTION - timedelta(days=1)
        for x in range(5):
            await self._stored(db, signature, x=x, served=long_ago)

        await purge_unserved_map_tiles({})

        assert _rows(db, signature) == []
