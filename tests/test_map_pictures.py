"""Dive, trip and dive site map pictures: what names one, the renderer's contract from the API's side,
and the routes that find or draw one.

Every draw here goes to `StubRenderer`, which speaks the renderer's contract and refuses a
body outside it with the `400` the real one answers - so a payload that grew a field, or lost
one, fails these tests rather than the first draw on a running instance.

The digest is what the whole design rests on: a field the renderer reads and the digest
leaves out is a picture that never updates. So `TestTheDigest` moves every positional field
one at a time, and `TestTheListAndTheRouteAgree` pins that a list row names the picture the
route then serves.
"""

import asyncio
import io
import json
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from functools import cache as memoize
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_user
from src.app.core.config import EnvironmentOption, Settings, normalize_map_renderer_url, settings
from src.app.core.db.database import async_engine, local_session
from src.app.core.setup import create_application
from src.app.core.utils import cache
from src.app.core.worker.functions import purge_unserved_map_pictures
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_site import DiveSite
from src.app.models.map_picture import MapPicture
from src.app.models.trip import Trip
from src.app.models.trip_part import TripPart
from src.app.models.user import User
from src.app.schemas.map_picture import MapTheme
from src.app.services import blob_store, map_pictures, map_renderer
from src.app.services.map_pictures import (
    UNSERVED_RETENTION,
    digest,
    dive_map_picture,
    dive_payload,
    dive_site_map_picture,
    trip_map_picture,
    trip_payload,
)
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_user

SIGNATURE = "a" * 64
REDEPLOYED = "b" * 64
RENDERER_URL = "http://renderer.test"

_DIVE_KEYS = {"kind", "theme", "dive_sites", *map_pictures.FIX_FIELDS}
_LOCATION_KEYS = {"latitude", "longitude", "bbox_south", "bbox_north", "bbox_west", "bbox_east"}


def _is_coordinate(value: object) -> bool:
    return value is None or (isinstance(value, int | float) and not isinstance(value, bool))


def _breaks_the_contract(body: object) -> bool:
    """Whether a `POST /render` body is outside the renderer's contract: the record's kind, the
    theme, and the positional subset in `DiveRead`'s or `TripRead`'s names - and no other field."""
    if not isinstance(body, dict) or body.get("theme") not in ("light", "dark"):
        return True
    if body.get("kind") == "dive":
        sites = body.get("dive_sites")
        return (
            set(body) != _DIVE_KEYS
            or not isinstance(sites, list)
            or any(not isinstance(site, dict) or set(site) != {"latitude", "longitude"} for site in sites)
            or not all(_is_coordinate(value) for site in sites for value in site.values())
            or not all(_is_coordinate(body[field]) for field in map_pictures.FIX_FIELDS)
        )
    if body.get("kind") == "trip":
        parts = body.get("parts")
        return (
            set(body) != {"kind", "theme", "parts"}
            or not isinstance(parts, list)
            or any(not isinstance(part, dict) or set(part) != {"location"} for part in parts)
            or any(
                part["location"] is not None
                and (
                    not isinstance(part["location"], dict)
                    or set(part["location"]) != _LOCATION_KEYS
                    or not all(_is_coordinate(value) for value in part["location"].values())
                )
                for part in parts
            )
        )
    return True


@memoize
def _webp(shade: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2048, 1024), (shade % 256, 120, 160)).save(buffer, "WEBP")
    return buffer.getvalue()


class StubRenderer:
    """The renderer's contract, in a `MockTransport`: `GET /signature`, and `POST /render`
    answering a 2048x1024 WebP signed with `X-Map-Signature` - or a `400` for a body outside
    the contract, or whatever `status` says.

    `gate`, when set, holds every draw until it opens, which is how a test arranges for
    requests to overlap one.
    """

    def __init__(self) -> None:
        self.signature = SIGNATURE
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
            self.started.set()
            if self.gate is not None:
                await self.gate.wait()
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "busy"})
            return httpx.Response(
                200,
                content=_webp(len(self.renders)),
                headers={"Content-Type": "image/webp", "X-Map-Signature": self.draws_with or self.signature},
            )
        return httpx.Response(404)


class FakeRedis:
    """What `@cache`, the rate limiter and the draw claim ask of Redis, in a dict."""

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
    monkeypatch.setattr(map_renderer, "_signature", SIGNATURE)
    monkeypatch.setattr(map_renderer, "_unreachable", False)
    monkeypatch.setattr(map_pictures, "_WAIT_POLL_SECONDS", 0.01)
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


# -------------- naming a picture --------------


def _dive(**overrides: Any) -> dict[str, Any]:
    """A dive as `GET /dives` lists it, cut to what matters here plus a few fields that don't."""
    return {
        "notes": "Turtle at the wall",
        "max_depth": 18.4,
        "dive_sites": [
            {"name": "Blue Hole", "latitude": 28.5721, "longitude": 34.5370, "location": {"name": "Dahab"}},
            {"name": "Bells", "latitude": 28.5735, "longitude": 34.5364, "location": None},
        ],
        "entry_latitude": 28.5719,
        "entry_longitude": 34.5371,
        "exit_latitude": 28.5722,
        "exit_longitude": 34.5368,
        **overrides,
    }


def _trip(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "Fiji and Samoa",
        "parts": [
            {
                "start_date": "2026-06-01",
                "end_date": "2026-06-08",
                "accommodation_uuid": None,
                "location": {
                    "name": "Fiji",
                    "latitude": -17.71,
                    "longitude": 178.06,
                    "bbox_south": -21.0,
                    "bbox_north": -12.4,
                    "bbox_west": 174.5,
                    "bbox_east": -178.2,
                },
            },
            {"start_date": None, "end_date": None, "accommodation_uuid": None, "location": None},
            {
                "start_date": "2026-06-09",
                "end_date": None,
                "accommodation_uuid": None,
                "location": {
                    "name": "Apia",
                    "latitude": -13.83,
                    "longitude": -171.76,
                    "bbox_south": None,
                    "bbox_north": None,
                    "bbox_west": None,
                    "bbox_east": None,
                },
            },
        ],
        **overrides,
    }


def _dive_digest(dive: dict[str, Any]) -> str:
    payload = dive_payload(dive)
    assert payload is not None
    return digest(payload, SIGNATURE)


def _trip_digest(trip: dict[str, Any]) -> str:
    return digest(trip_payload(trip), SIGNATURE)


def _with_site(index: int, **changes: Any) -> dict[str, Any]:
    dive = _dive()
    dive["dive_sites"] = [dict(site) for site in dive["dive_sites"]]
    dive["dive_sites"][index].update(changes)
    return dive


def _with_location(index: int, **changes: Any) -> dict[str, Any]:
    trip = _trip()
    trip["parts"] = [dict(part) for part in trip["parts"]]
    trip["parts"][index]["location"] = {**trip["parts"][index]["location"], **changes}
    return trip


class TestTheDigest:
    @pytest.mark.parametrize(
        "moved",
        [
            pytest.param(_with_site(0, latitude=28.6), id="first site latitude"),
            pytest.param(_with_site(1, longitude=34.6), id="second site longitude"),
            pytest.param(_with_site(1, latitude=None, longitude=None), id="a site losing its pin"),
            pytest.param(_dive(entry_latitude=28.58), id="entry latitude"),
            pytest.param(_dive(entry_longitude=34.54), id="entry longitude"),
            pytest.param(_dive(exit_latitude=28.58), id="exit latitude"),
            pytest.param(_dive(exit_longitude=34.54), id="exit longitude"),
            pytest.param(_dive(exit_latitude=None, exit_longitude=None), id="no exit fix"),
            pytest.param(_dive(dive_sites=list(reversed(_dive()["dive_sites"]))), id="the sites reordered"),
            pytest.param(_dive(dive_sites=_dive()["dive_sites"][:1]), id="a site removed"),
        ],
    )
    def test_every_positional_field_of_a_dive_moves_it(self, moved: dict[str, Any]) -> None:
        assert _dive_digest(moved) != _dive_digest(_dive())

    @pytest.mark.parametrize(
        "unmoved",
        [
            pytest.param(_with_site(0, name="Canyon"), id="a site renamed"),
            pytest.param(_with_site(0, location={"name": "Elsewhere"}), id="a site's locality"),
            pytest.param(_dive(notes="", max_depth=30.0), id="the dive's own fields"),
        ],
    )
    def test_a_name_or_any_other_field_of_a_dive_does_not(self, unmoved: dict[str, Any]) -> None:
        assert _dive_digest(unmoved) == _dive_digest(_dive())

    @pytest.mark.parametrize("field", sorted(_LOCATION_KEYS))
    def test_every_positional_field_of_a_trip_moves_it(self, field: str) -> None:
        assert _trip_digest(_with_location(0, **{field: 1.5})) != _trip_digest(_trip())

    @pytest.mark.parametrize(
        "moved",
        [
            pytest.param(_trip(parts=list(reversed(_trip()["parts"]))), id="the parts reordered"),
            pytest.param(_trip(parts=_trip()["parts"][:2]), id="a part removed"),
            pytest.param(_trip(parts=[*_trip()["parts"][:1], *_trip()["parts"][2:]]), id="a placeless part removed"),
        ],
    )
    def test_the_parts_order_and_count_move_it(self, moved: dict[str, Any]) -> None:
        assert _trip_digest(moved) != _trip_digest(_trip())

    @pytest.mark.parametrize(
        "unmoved",
        [
            pytest.param(_with_location(0, name="Viti Levu"), id="a place renamed"),
            pytest.param(_trip(name="Another name"), id="the trip renamed"),
        ],
    )
    def test_a_name_or_a_date_of_a_trip_does_not(self, unmoved: dict[str, Any]) -> None:
        trip = _trip()
        trip["parts"][0] = {**trip["parts"][0], "start_date": "2027-01-01"}
        assert _trip_digest(unmoved) == _trip_digest(_trip()) == _trip_digest(trip)

    def test_the_signature_moves_it(self) -> None:
        payload = dive_payload(_dive())
        assert payload is not None
        assert digest(payload, SIGNATURE) != digest(payload, REDEPLOYED)

    def test_the_kind_is_in_it(self) -> None:
        """So a dive and a trip can never share a picture by an accident of serialisation."""
        payload = dive_payload(_dive())
        assert payload is not None
        assert digest(payload, SIGNATURE) != digest({**payload, "kind": "trip"}, SIGNATURE)

    def test_an_integer_coordinate_names_the_picture_its_float_does(self) -> None:
        """A cached list row is JSON and a route's read is a float column; both must agree."""
        assert _dive_digest(_dive(entry_latitude=28, entry_longitude=34)) == _dive_digest(
            _dive(entry_latitude=28.0, entry_longitude=34.0)
        )

    def test_the_payloads_are_the_contracts_bodies(self) -> None:
        dive = dive_payload(_dive())
        assert dive is not None
        for theme in MapTheme:
            assert not _breaks_the_contract({**dive, "theme": theme.value})
            assert not _breaks_the_contract({**trip_payload(_trip()), "theme": theme.value})
        assert not _breaks_the_contract({**trip_payload({"parts": []}), "theme": "dark"})


class TestARecordsMapPicture:
    """`map_picture` on a list row: the digest, or null in each of the cases that have none."""

    def test_a_dive_and_a_trip_name_their_pictures(self, renderer: StubRenderer) -> None:
        assert dive_map_picture(_dive()) == _dive_digest(_dive())
        assert trip_map_picture(_trip()) == _trip_digest(_trip())

    def test_none_without_a_renderer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "MAP_RENDERER_URL", "")
        monkeypatch.setattr(map_renderer, "_signature", SIGNATURE)
        assert dive_map_picture(_dive()) is None
        assert trip_map_picture(_trip()) is None

    def test_none_while_the_signature_is_unknown(self, renderer: StubRenderer, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(map_renderer, "_signature", None)
        assert dive_map_picture(_dive()) is None
        assert trip_map_picture(_trip()) is None

    @pytest.mark.parametrize(
        "unplaced",
        [
            pytest.param(_dive(dive_sites=[], **dict.fromkeys(map_pictures.FIX_FIELDS)), id="no site, no fix"),
            pytest.param(
                _dive(
                    dive_sites=[{"name": "Unpinned", "latitude": None, "longitude": None}],
                    **dict.fromkeys(map_pictures.FIX_FIELDS),
                ),
                id="an unpinned site",
            ),
            pytest.param(
                _dive(dive_sites=[], entry_latitude=28.5, **dict.fromkeys(map_pictures.FIX_FIELDS[1:])),
                id="half a fix",
            ),
        ],
    )
    def test_none_for_a_dive_with_nothing_placed(self, renderer: StubRenderer, unplaced: dict[str, Any]) -> None:
        assert dive_map_picture(unplaced) is None

    @pytest.mark.parametrize(
        "placed",
        [
            pytest.param(_dive(dive_sites=[], **dict.fromkeys(map_pictures.FIX_FIELDS[:2])), id="an exit fix alone"),
            pytest.param(_dive(**dict.fromkeys(map_pictures.FIX_FIELDS)), id="sites alone"),
            # Latitude 0 and longitude 0 are positions, not absences.
            pytest.param(
                _dive(dive_sites=[{"latitude": 0.0, "longitude": 0.0}], **dict.fromkeys(map_pictures.FIX_FIELDS)),
                id="null island",
            ),
        ],
    )
    def test_a_dive_with_anything_placed_has_one(self, renderer: StubRenderer, placed: dict[str, Any]) -> None:
        assert dive_map_picture(placed) is not None

    @pytest.mark.parametrize("parts", [[], [{"location": None}]], ids=["no parts", "a placeless part"])
    def test_a_trip_with_no_place_has_one(self, renderer: StubRenderer, parts: list[dict[str, Any]]) -> None:
        """Its card shows the whole world."""
        assert trip_map_picture({"parts": parts}) is not None

    def test_a_site_names_the_picture_a_one_site_dive_there_with_no_fix_does(self, renderer: StubRenderer) -> None:
        site = {"name": "Blue Hole", "latitude": 28.5721, "longitude": 34.5370, "notes": "The arch at 55 m"}
        at_it = _dive(dive_sites=[{**site, "name": "Another name"}], **dict.fromkeys(map_pictures.FIX_FIELDS))

        assert dive_site_map_picture(site) == dive_map_picture(at_it) is not None
        assert dive_site_map_picture(site) != dive_map_picture(_dive(dive_sites=[site]))

    def test_none_for_a_site_without_a_renderer_a_signature_or_a_position(
        self, renderer: StubRenderer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site = {"latitude": 28.5721, "longitude": 34.5370}
        assert dive_site_map_picture({"latitude": None, "longitude": None}) is None
        monkeypatch.setattr(map_renderer, "_signature", None)
        assert dive_site_map_picture(site) is None
        monkeypatch.setattr(map_renderer, "_signature", SIGNATURE)
        monkeypatch.setattr(settings, "MAP_RENDERER_URL", "")
        assert dive_site_map_picture(site) is None


# -------------- the setting --------------


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


class TestTheSetting:
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
        for name in (
            "MAP_RENDERER_URL",
            "MAP_RENDERER_TIMEOUT",
            "MAP_PICTURE_RATE_LIMIT_WINDOW_SECONDS",
            "MAP_PICTURE_RATE_LIMIT_PER_USER",
        ):
            (line,) = [line for line in template.splitlines() if line.startswith(f"# {name}=")]
            values[name] = line.partition("=")[2].strip('"')

        configured = _settings(**values)

        assert configured.MAP_RENDERER_URL == ""
        assert configured.map_pictures is False


# -------------- the renderer's contract, from the client's side --------------


class TestTheRendererClient:
    @pytest.mark.asyncio
    async def test_the_signature_is_learned_and_kept_through_an_outage(
        self, renderer: StubRenderer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(map_renderer, "_signature", None)
        await map_renderer.refresh_signature()
        assert map_renderer.current_signature() == SIGNATURE

        renderer.down = True
        await map_renderer.refresh_signature()
        assert map_renderer.current_signature() == SIGNATURE

        renderer.down, renderer.signature = False, REDEPLOYED
        await map_renderer.refresh_signature()
        assert map_renderer.current_signature() == REDEPLOYED

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
                    200, content=b"not a picture", headers={"Content-Type": "image/webp", "X-Map-Signature": SIGNATURE}
                ),
                id="bytes that are no WebP",
            ),
            pytest.param(
                httpx.Response(200, content=_webp(1), headers={"Content-Type": "image/webp"}), id="no signature"
            ),
            pytest.param(
                httpx.Response(
                    200, content=_webp(1), headers={"Content-Type": "image/png", "X-Map-Signature": SIGNATURE}
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
            await map_renderer.render({"kind": "trip", "theme": "light", "parts": []}, timeout=5)

    @pytest.mark.asyncio
    async def test_a_draw_past_its_deadline_is_a_failure(self, renderer: StubRenderer) -> None:
        renderer.gate = asyncio.Event()
        with pytest.raises(map_renderer.RendererUnavailable):
            await map_renderer.render({"kind": "trip", "theme": "light", "parts": []}, timeout=0.05)

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
            assert map_renderer.current_signature() == SIGNATURE
        finally:
            await map_renderer.stop_signature_refresh()
        assert map_renderer._refresher is None

    @pytest.mark.asyncio
    async def test_no_refresh_starts_without_a_renderer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "MAP_RENDERER_URL", "")
        map_renderer.start_signature_refresh()
        assert map_renderer._refresher is None
        await map_renderer.stop_signature_refresh()


# -------------- against Postgres: the routes, the store, the cron --------------


@pytest.fixture(scope="module")
def map_app() -> Any:
    """The real application, so the picture's headers pass through the middleware that rewrites
    headers on the way out, as `test_user_pictures.py` does it."""
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


@pytest_asyncio.fixture
async def api(
    map_app: Any, db: Session, redis: FakeRedis, volume: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, User]]:
    """A signed-in diver's client over the app, on this test's event loop.

    The routes open sessions from the module-level `local_session`, and so does a draw, so the
    app's engine is disposed on either side: a pooled asyncpg connection belongs to the loop
    that opened it.
    """
    diver = create_user(db)
    map_app.dependency_overrides[get_current_user] = lambda: _as(diver)
    await async_engine.dispose()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=map_app), base_url="http://api.test") as client:
            yield client, diver
        await map_pictures._await_pending_draws()
    finally:
        map_app.dependency_overrides = {}
        await async_engine.dispose()


def _site(db: Session, user: User, latitude: float | None, longitude: float | None) -> DiveSite:
    site = DiveSite(user_id=user.id, name=f"Site {uuid7().hex[-8:]}", latitude=latitude, longitude=longitude)
    db.add(site)
    db.commit()
    return site


def _dive_at(
    db: Session,
    user: User,
    *positions: tuple[float | None, float | None],
    entry: tuple[float, float] | None = None,
    exit: tuple[float, float] | None = None,
) -> Dive:
    dive = create_dive(db, user)
    for position, (latitude, longitude) in enumerate(positions):
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=_site(db, user, latitude, longitude).id, position=position))
    dive.entry_latitude, dive.entry_longitude = entry or (None, None)
    dive.exit_latitude, dive.exit_longitude = exit or (None, None)
    db.commit()
    return dive


def _trip_through(db: Session, user: User, *places: Mapping[str, float] | None) -> Trip:
    trip = Trip(user_id=user.id, name=f"Trip {uuid7().hex[-8:]}", notes="")
    db.add(trip)
    db.commit()
    for position, place in enumerate(places):
        part = TripPart(trip_id=trip.id, position=position)
        if place is not None:
            part.name = "Somewhere"
            for field, value in place.items():
                setattr(part, field, value)
        db.add(part)
    db.commit()
    return trip


def _rows(db: Session, user: User) -> list[MapPicture]:
    db.expire_all()
    return list(db.execute(select(MapPicture).where(MapPicture.user_id == user.id)).scalars())


def _picture_url(kind: str, uuid: Any, *, theme: str = "light", v: str | None = None) -> str:
    query = f"?theme={theme}" + (f"&v={v}" if v is not None else "")
    return f"/api/v1/{kind}/{uuid}/map-picture{query}"


async def _listed(client: httpx.AsyncClient, path: str, uuid: Any) -> str | None:
    response = await client.get(path)
    assert response.status_code == 200, response.text
    (row,) = [row for row in response.json()["data"] if row["uuid"] == str(uuid)]
    picture: str | None = row["map_picture"]
    return picture


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheRoutes:
    @pytest.mark.asyncio
    async def test_a_miss_is_drawn_stored_and_kept_and_the_next_request_is_the_stored_picture(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))
        name = await _listed(client, "/api/v1/dives", dive.uuid)

        first = await client.get(_picture_url("dive", dive.uuid, v=name))
        second = await client.get(_picture_url("dive", dive.uuid, v=name))

        assert first.status_code == second.status_code == 200
        assert first.headers["content-type"] == "image/webp"
        assert first.headers["cache-control"] == second.headers["cache-control"] == "private, max-age=300"
        assert first.headers["x-content-type-options"] == "nosniff"
        assert first.headers["content-security-policy"] == "default-src 'none'; sandbox; frame-ancestors 'none'"
        assert first.content == second.content == _webp(1)
        assert first.headers["etag"] == second.headers["etag"]
        assert len(renderer.renders) == 1
        (row,) = _rows(db, diver)
        assert (row.digest, row.theme) == (name, "light")
        assert blob_store.exists(row.storage_key)

    @pytest.mark.asyncio
    async def test_no_list_calls_the_renderer(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        """The lists name pictures from the signature held in memory, so a renderer that is
        down costs them nothing."""
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))
        trip = _trip_through(db, diver, None)
        renderer.down = True

        assert await _listed(client, "/api/v1/dives", dive.uuid) is not None
        assert await _listed(client, "/api/v1/trips", trip.uuid) is not None
        assert (await client.get(f"/api/v1/trip/{trip.uuid}")).json()["map_picture"] is not None
        assert renderer.calls == 0

    @pytest.mark.asyncio
    async def test_an_etag_still_current_is_a_304(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))
        name = await _listed(client, "/api/v1/dives", dive.uuid)
        drawn = await client.get(_picture_url("dive", dive.uuid, v=name))

        again = await client.get(
            _picture_url("dive", dive.uuid, v=name), headers={"If-None-Match": drawn.headers["etag"]}
        )

        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["cache-control"] == "private, max-age=300"
        assert len(renderer.renders) == 1

    @pytest.mark.asyncio
    async def test_a_stale_v_gets_the_current_picture_uncached(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        """`v` decides only whether the browser may keep the answer; the database decides
        which picture it is."""
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))

        stale = await client.get(_picture_url("dive", dive.uuid, v="0" * 64))
        unversioned = await client.get(_picture_url("dive", dive.uuid))

        assert stale.status_code == unversioned.status_code == 200
        assert stale.headers["cache-control"] == unversioned.headers["cache-control"] == "private, no-store"
        assert stale.content == unversioned.content
        assert len(renderer.renders) == 1

    @pytest.mark.asyncio
    async def test_the_themes_are_two_pictures_under_one_digest(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))

        for theme in ("light", "dark"):
            assert (await client.get(_picture_url("dive", dive.uuid, theme=theme))).status_code == 200

        assert [body["theme"] for body in renderer.renders] == ["light", "dark"]
        rows = _rows(db, diver)
        assert sorted(row.theme for row in rows) == ["dark", "light"]
        assert len({row.digest for row in rows}) == 1

    @pytest.mark.asyncio
    async def test_same_place_dives_share_a_picture_and_a_moved_one_does_not(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        first = _dive_at(db, diver, (28.57, 34.53))
        second = _dive_at(db, diver, (28.57, 34.53))

        await client.get(_picture_url("dive", first.uuid))
        await client.get(_picture_url("dive", second.uuid))
        assert len(renderer.renders) == 1

        site_id = db.execute(select(DiveDiveSite.dive_site_id).where(DiveDiveSite.dive_id == second.id)).scalar_one()
        db.execute(update(DiveSite).where(DiveSite.id == site_id).values(latitude=28.60))
        db.commit()
        await client.get(_picture_url("dive", second.uuid))
        assert len(renderer.renders) == 2
        assert renderer.renders[-1]["dive_sites"] == [{"latitude": 28.60, "longitude": 34.53}]

    @pytest.mark.asyncio
    async def test_a_trip_with_no_place_draws_the_world(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        trip = _trip_through(db, diver, None)

        response = await client.get(_picture_url("trip", trip.uuid))

        assert response.status_code == 200
        assert renderer.renders == [{"kind": "trip", "theme": "light", "parts": [{"location": None}]}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["dive", "trip"])
    async def test_someone_elses_record_reads_as_a_missing_one(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer, kind: str
    ) -> None:
        client, _diver = api
        stranger = create_user(db)
        theirs = _dive_at(db, stranger, (28.57, 34.53)) if kind == "dive" else _trip_through(db, stranger, None)

        someone_elses = await client.get(_picture_url(kind, theirs.uuid))
        missing = await client.get(_picture_url(kind, uuid7()))

        assert someone_elses.status_code == missing.status_code == 404
        assert someone_elses.json() == missing.json() == {"detail": f"{kind.capitalize()} not found"}
        assert renderer.renders == []

    @pytest.mark.asyncio
    async def test_a_dive_with_nothing_placed_is_a_404(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        dive = _dive_at(db, diver, (None, None))

        assert await _listed(client, "/api/v1/dives", dive.uuid) is None
        response = await client.get(_picture_url("dive", dive.uuid))

        assert response.status_code == 404
        assert renderer.renders == []

    @pytest.mark.asyncio
    async def test_without_a_renderer_nothing_is_named_or_drawn(
        self, api: tuple[httpx.AsyncClient, User], db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, diver = api
        monkeypatch.setattr(settings, "MAP_RENDERER_URL", "")
        monkeypatch.setattr(map_renderer, "_signature", SIGNATURE)
        dive = _dive_at(db, diver, (28.57, 34.53))
        trip = _trip_through(db, diver, None)

        assert await _listed(client, "/api/v1/dives", dive.uuid) is None
        assert await _listed(client, "/api/v1/trips", trip.uuid) is None
        assert (await client.get(f"/api/v1/trip/{trip.uuid}")).json()["map_picture"] is None
        assert (await client.get(_picture_url("dive", dive.uuid))).status_code == 404
        assert (await client.get("/api/v1/config")).json()["map_pictures"] is False

    @pytest.mark.asyncio
    async def test_a_failed_draw_is_a_503_and_stores_nothing(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer, redis: FakeRedis
    ) -> None:
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))
        renderer.status = 503

        failed = await client.get(_picture_url("dive", dive.uuid))

        assert failed.status_code == 503
        assert _rows(db, diver) == []
        assert not [key for key in redis.values if key.startswith("map-picture:draw:")], "the claim outlived the draw"
        assert (await client.get("/api/v1/dives")).status_code == 200

        renderer.status = 200
        assert (await client.get(_picture_url("dive", dive.uuid))).status_code == 200

    @pytest.mark.asyncio
    async def test_a_renderer_redeployed_mid_request_names_the_picture_it_drew(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))
        before = await _listed(client, "/api/v1/dives", dive.uuid)
        renderer.draws_with = REDEPLOYED

        response = await client.get(_picture_url("dive", dive.uuid, v=before))

        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert map_renderer.current_signature() == REDEPLOYED
        after = await _listed(client, "/api/v1/dives", dive.uuid)
        assert after != before
        (row,) = _rows(db, diver)
        assert row.digest == after
        kept = await client.get(_picture_url("dive", dive.uuid, v=after))
        assert kept.headers["cache-control"] == "private, max-age=300"
        assert len(renderer.renders) == 1

    @pytest.mark.asyncio
    async def test_concurrent_misses_draw_once(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        """Three same-site cards at once: one draw, three pictures."""
        client, diver = api
        dives = [_dive_at(db, diver, (28.57, 34.53)) for _ in range(3)]
        renderer.gate = asyncio.Event()

        requests = asyncio.gather(*(client.get(_picture_url("dive", dive.uuid)) for dive in dives))
        await asyncio.wait_for(renderer.started.wait(), timeout=5)
        await asyncio.sleep(0.1)
        renderer.gate.set()
        responses = await asyncio.wait_for(requests, timeout=5)

        assert [response.status_code for response in responses] == [200, 200, 200]
        assert len({response.content for response in responses}) == 1
        assert len(renderer.renders) == 1
        assert len(_rows(db, diver)) == 1

    @pytest.mark.asyncio
    async def test_a_claimant_whose_client_goes_still_stores_its_picture(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer, redis: FakeRedis
    ) -> None:
        _client, diver = api
        payload = trip_payload({"parts": []})
        renderer.gate = asyncio.Event()

        async with local_session() as session:
            request = asyncio.create_task(
                map_pictures.find_or_draw(
                    session, user_id=diver.id, payload=payload, theme=MapTheme.DARK, if_none_match=None
                )
            )
            await asyncio.wait_for(renderer.started.wait(), timeout=5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        renderer.gate.set()
        await map_pictures._await_pending_draws()

        (row,) = _rows(db, diver)
        assert (row.digest, row.theme) == (digest(payload, SIGNATURE), "dark")
        assert blob_store.exists(row.storage_key)
        assert not [key for key in redis.values if key.startswith("map-picture:draw:")]

    @pytest.mark.asyncio
    async def test_a_claimant_cancelled_before_its_draw_lets_the_claim_go(
        self,
        api: tuple[httpx.AsyncClient, User],
        db: Session,
        renderer: StubRenderer,
        redis: FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancelled between taking the claim and starting the draw - here, in the look it takes
        once more after claiming. A claim left standing would hold every other request for the
        picture until it lapsed, past their own deadlines."""
        _client, diver = api
        reached, held = asyncio.Event(), asyncio.Event()
        release = map_pictures.release_read_transaction

        async def held_once_claimed(session: Any) -> None:
            await release(session)
            if [key for key in redis.values if key.startswith("map-picture:draw:")]:
                reached.set()
                await held.wait()

        monkeypatch.setattr(map_pictures, "release_read_transaction", held_once_claimed)

        async with local_session() as session:
            request = asyncio.create_task(
                map_pictures.find_or_draw(
                    session,
                    user_id=diver.id,
                    payload=trip_payload({"parts": []}),
                    theme=MapTheme.LIGHT,
                    if_none_match=None,
                )
            )
            await asyncio.wait_for(reached.wait(), timeout=5)
            assert [key for key in redis.values if key.startswith("map-picture:draw:")]
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

        assert not [key for key in redis.values if key.startswith("map-picture:draw:")]
        assert renderer.renders == []

    @pytest.mark.asyncio
    async def test_draws_are_rate_limited_and_stored_pictures_and_waiters_are_not(
        self,
        api: tuple[httpx.AsyncClient, User],
        db: Session,
        renderer: StubRenderer,
        redis: FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, diver = api
        monkeypatch.setattr(settings, "MAP_PICTURE_RATE_LIMIT_PER_USER", 1)
        here = [_dive_at(db, diver, (28.57, 34.53)) for _ in range(2)]
        elsewhere = _dive_at(db, diver, (9.95, 123.37))

        renderer.gate = asyncio.Event()
        requests = asyncio.gather(*(client.get(_picture_url("dive", dive.uuid)) for dive in here))
        await asyncio.wait_for(renderer.started.wait(), timeout=5)
        await asyncio.sleep(0.05)
        renderer.gate.set()
        assert [response.status_code for response in await requests] == [200, 200]

        refused = await client.get(_picture_url("dive", elsewhere.uuid))
        stored = await client.get(_picture_url("dive", here[0].uuid))

        assert refused.status_code == 429
        assert stored.status_code == 200
        assert len(renderer.renders) == 1
        assert not [key for key in redis.values if key.startswith("map-picture:draw:")], "a refused claim stood"

    @pytest.mark.asyncio
    async def test_a_row_whose_file_is_gone_is_drawn_again(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))
        await client.get(_picture_url("dive", dive.uuid))
        (gone,) = _rows(db, diver)
        gone_key = gone.storage_key
        await blob_store.delete(gone_key)

        response = await client.get(_picture_url("dive", dive.uuid))

        assert response.status_code == 200
        assert response.content == _webp(2)
        (row,) = _rows(db, diver)
        assert row.storage_key != gone_key
        assert blob_store.exists(row.storage_key)

    @pytest.mark.asyncio
    async def test_a_found_picture_marks_its_use_at_most_once_a_day(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))
        await client.get(_picture_url("dive", dive.uuid))
        (row,) = _rows(db, diver)
        recently, long_ago = datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC) - timedelta(days=3)

        row.last_served_at = recently
        db.commit()
        await client.get(_picture_url("dive", dive.uuid))
        assert _rows(db, diver)[0].last_served_at == recently

        _rows(db, diver)[0].last_served_at = long_ago
        db.commit()
        await client.get(_picture_url("dive", dive.uuid), headers={"If-None-Match": f'"{row.sha256}"'})
        assert _rows(db, diver)[0].last_served_at > recently

    @pytest.mark.asyncio
    async def test_an_unknown_signature_is_asked_for_and_a_silent_renderer_is_a_503(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))
        monkeypatch.setattr(map_renderer, "_signature", None)

        renderer.down = True
        assert (await client.get(_picture_url("dive", dive.uuid))).status_code == 503
        renderer.down = False
        assert (await client.get(_picture_url("dive", dive.uuid))).status_code == 200
        assert map_renderer.current_signature() == SIGNATURE


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheListAndTheRouteAgree:
    """The digest a list row carries is the one the route computes for the same record, so the
    browser's URL names the stored picture and may keep it."""

    @pytest.mark.asyncio
    async def test_for_a_dive_with_several_sites_and_both_fixes(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        dive = _dive_at(
            db,
            diver,
            (28.5721, 34.5370),
            (None, None),
            (28.5735, 34.5364),
            entry=(28.5719, 34.5371),
            exit=(28.5722, 34.5368),
        )

        name = await _listed(client, "/api/v1/dives", dive.uuid)
        response = await client.get(_picture_url("dive", dive.uuid, v=name))

        assert name is not None
        assert response.headers["cache-control"] == "private, max-age=300"
        assert _rows(db, diver)[0].digest == name
        assert renderer.renders == [
            {
                "kind": "dive",
                "theme": "light",
                "dive_sites": [
                    {"latitude": 28.5721, "longitude": 34.5370},
                    {"latitude": None, "longitude": None},
                    {"latitude": 28.5735, "longitude": 34.5364},
                ],
                "entry_latitude": 28.5719,
                "entry_longitude": 34.5371,
                "exit_latitude": 28.5722,
                "exit_longitude": 34.5368,
            }
        ]

    @pytest.mark.asyncio
    async def test_for_a_trip_with_several_parts_on_every_read_of_it(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        fiji = {
            "latitude": -17.71,
            "longitude": 178.06,
            "bbox_south": -21.0,
            "bbox_north": -12.4,
            "bbox_west": 174.5,
            "bbox_east": -178.2,
        }
        trip = _trip_through(db, diver, fiji, None, {"latitude": -13.83, "longitude": -171.76})

        listed = await _listed(client, "/api/v1/trips", trip.uuid)
        single = (await client.get(f"/api/v1/trip/{trip.uuid}")).json()["map_picture"]
        response = await client.get(_picture_url("trip", trip.uuid, v=listed))

        assert listed is not None
        assert single == listed
        assert response.headers["cache-control"] == "private, max-age=300"
        assert _rows(db, diver)[0].digest == listed
        (body,) = renderer.renders
        assert [part["location"] and part["location"]["latitude"] for part in body["parts"]] == [-17.71, None, -13.83]

    @pytest.mark.asyncio
    async def test_a_created_trip_names_the_picture_its_later_reads_do(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, _diver = api
        created = await client.post(
            "/api/v1/trip",
            json={
                "name": f"Moalboal {uuid7().hex[-8:]}",
                "parts": [{"location": {"name": "Moalboal", "latitude": 9.95, "longitude": 123.37}}],
                "people": [],
            },
        )

        assert created.status_code == 201, created.text
        body = created.json()
        assert body["map_picture"] is not None
        assert body["map_picture"] == (await client.get(f"/api/v1/trip/{body['uuid']}")).json()["map_picture"]

    @pytest.mark.asyncio
    async def test_the_config_says_pictures_are_drawn(
        self, api: tuple[httpx.AsyncClient, User], renderer: StubRenderer
    ) -> None:
        client, _diver = api
        assert (await client.get("/api/v1/config")).json()["map_pictures"] is True


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestASitesPicture:
    """A site is drawn as a one-site dive with no fix, through the dive's own find-or-draw."""

    @pytest.mark.asyncio
    async def test_every_read_of_a_site_names_the_picture_its_route_serves(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        created = await client.post(
            "/api/v1/dive-site",
            json={"name": f"Blue Hole {uuid7().hex[-8:]}", "latitude": 28.5721, "longitude": 34.537},
        )
        assert created.status_code == 201, created.text
        body = created.json()

        listed = await _listed(client, "/api/v1/dive-sites", body["uuid"])
        single = (await client.get(f"/api/v1/dive-site/{body['uuid']}")).json()["map_picture"]
        response = await client.get(_picture_url("dive-site", body["uuid"], v=listed))

        assert listed is not None
        assert body["map_picture"] == single == listed
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/webp"
        assert response.headers["cache-control"] == "private, max-age=300"
        assert _rows(db, diver)[0].digest == listed
        assert renderer.renders == [
            {
                "kind": "dive",
                "theme": "light",
                "dive_sites": [{"latitude": 28.5721, "longitude": 34.537}],
                **dict.fromkeys(map_pictures.FIX_FIELDS),
            }
        ]

    @pytest.mark.asyncio
    async def test_a_site_and_a_one_site_dive_there_with_no_fix_share_one_picture(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        dive = _dive_at(db, diver, (28.57, 34.53))
        site_uuid = db.execute(
            select(DiveSite.uuid).join(DiveDiveSite).where(DiveDiveSite.dive_id == dive.id)
        ).scalar_one()

        drawn = await client.get(_picture_url("dive", dive.uuid))
        served = await client.get(_picture_url("dive-site", site_uuid))

        assert drawn.status_code == served.status_code == 200
        assert served.content == drawn.content
        assert served.headers["etag"] == drawn.headers["etag"]
        assert len(renderer.renders) == 1
        assert len(_rows(db, diver)) == 1
        assert (
            await _listed(client, "/api/v1/dive-sites", site_uuid)
            == await _listed(client, "/api/v1/dives", dive.uuid)
            is not None
        )

    @pytest.mark.asyncio
    async def test_someone_elses_site_reads_as_a_missing_one(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, _diver = api
        theirs = _site(db, create_user(db), 28.57, 34.53)

        someone_elses = await client.get(_picture_url("dive-site", theirs.uuid))
        missing = await client.get(_picture_url("dive-site", uuid7()))

        assert someone_elses.status_code == missing.status_code == 404
        assert someone_elses.json() == missing.json() == {"detail": "Dive site not found"}
        assert renderer.renders == []

    @pytest.mark.asyncio
    async def test_a_site_with_no_position_names_none_and_is_a_404(
        self, api: tuple[httpx.AsyncClient, User], db: Session, renderer: StubRenderer
    ) -> None:
        client, diver = api
        site = _site(db, diver, None, None)

        assert await _listed(client, "/api/v1/dive-sites", site.uuid) is None
        assert (await client.get(f"/api/v1/dive-site/{site.uuid}")).json()["map_picture"] is None
        assert (await client.get(_picture_url("dive-site", site.uuid))).status_code == 404
        assert renderer.renders == []

    @pytest.mark.asyncio
    async def test_without_a_renderer_a_site_names_none_and_is_a_404(
        self, api: tuple[httpx.AsyncClient, User], db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, diver = api
        monkeypatch.setattr(settings, "MAP_RENDERER_URL", "")
        monkeypatch.setattr(map_renderer, "_signature", SIGNATURE)
        site = _site(db, diver, 28.57, 34.53)

        assert await _listed(client, "/api/v1/dive-sites", site.uuid) is None
        assert (await client.get(f"/api/v1/dive-site/{site.uuid}")).json()["map_picture"] is None
        assert (await client.get(_picture_url("dive-site", site.uuid))).status_code == 404


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheUnservedPurge:
    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        """The cron opens its own session from the module-level `local_session`."""
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @staticmethod
    async def _stored(db: Session, user: User, *, served: datetime) -> MapPicture:
        key = blob_store.new_key(map_pictures.BLOB_KIND, sha256="d" * 64)
        await blob_store.put(key, _webp(9))
        row = MapPicture(
            user_id=user.id,
            digest=uuid7().hex * 2,
            theme="light",
            storage_key=key,
            sha256="d" * 64,
            last_served_at=served,
        )
        db.add(row)
        db.commit()
        return row

    @pytest.mark.asyncio
    async def test_a_picture_unserved_past_the_retention_goes_with_its_file_and_a_used_one_stays(
        self, db: Session, volume: Path
    ) -> None:
        diver = create_user(db)
        now = datetime.now(UTC)
        unserved = await self._stored(db, diver, served=now - UNSERVED_RETENTION - timedelta(hours=1))
        used = await self._stored(db, diver, served=now - UNSERVED_RETENTION + timedelta(days=1))
        unserved_key, used_key = unserved.storage_key, used.storage_key

        await purge_unserved_map_pictures({})
        await blob_store._await_pending_removals()

        assert [row.storage_key for row in _rows(db, diver)] == [used_key]
        assert not blob_store.exists(unserved_key)
        assert blob_store.exists(used_key)

        # Idempotent: a second run finds nothing of this diver's to take.
        await purge_unserved_map_pictures({})
        assert [row.storage_key for row in _rows(db, diver)] == [used_key]

    @pytest.mark.asyncio
    async def test_it_works_through_more_than_a_batch(
        self, db: Session, volume: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(map_pictures, "PURGE_BATCH_SIZE", 2)
        diver = create_user(db)
        long_ago = datetime.now(UTC) - UNSERVED_RETENTION - timedelta(days=1)
        for _ in range(5):
            await self._stored(db, diver, served=long_ago)

        await purge_unserved_map_pictures({})

        assert _rows(db, diver) == []
