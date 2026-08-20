"""Tests for the geocoding proxy - the routes (`api/v1/geocoding.py`) and the service
underneath them (`services/geocoding_service.py`).

Three things here are compliance assertions rather than conveniences, and each is what
keeps this instance inside Nominatim's usage policy: every answer is cached, a second
identical lookup never reaches the provider, and the outbound call carries the configured
`User-Agent` and is counted against a per-second cap.

The fourth theme is degradation. A provider that times out, returns garbage or is switched
off must produce "no result", never a 5xx - a diver can always type the location in.
"""

import json
import logging
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.exceptions.http_exceptions import RateLimitException
from src.app.core.setup import create_application
from src.app.services import geocoding_service

_REAL_ASYNC_CLIENT = httpx.AsyncClient

CURRENT_USER = {"id": 7, "username": "ada", "is_superuser": False}

REVERSE_PAYLOAD = {
    "lat": "28.5717",
    "lon": "34.5372",
    "display_name": "Blue Hole, Dahab, South Sinai, Egypt",
    "name": "Blue Hole",
    # Nominatim's own string, byte for byte, including the bare `http://` it still sends.
    # The fold is only worth testing against what a provider actually says.
    "licence": "Data © OpenStreetMap contributors, ODbL 1.0. http://osm.org/copyright",
    "address": {"suburb": "Blue Hole", "city": "Dahab", "state": "South Sinai", "country": "Egypt"},
}


@pytest.fixture(scope="module")
def geocode_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`, like `test_export_endpoints.py` -
    nothing below these routes touches a database."""
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def client(geocode_app: Any) -> Generator[TestClient]:
    geocode_app.dependency_overrides[get_current_user] = lambda: CURRENT_USER
    with TestClient(geocode_app) as test_client:
        yield test_client
    geocode_app.dependency_overrides = {}


@pytest.fixture
def anonymous_client(geocode_app: Any) -> Generator[TestClient]:
    with TestClient(geocode_app) as test_client:
        yield test_client
    geocode_app.dependency_overrides = {}


class FakeRedis:
    """Enough of the Redis client for this module: `get`/`set` with an expiry we record but
    don't act on, since nothing here needs a clock.

    `failing=True` makes both raise, which is how the "a cache outage is a miss, not an
    error" branches get exercised - the one thing a purely in-memory double would otherwise
    let go untested.
    """

    def __init__(self, *, failing: bool = False) -> None:
        self.store: dict[str, bytes] = {}
        self.expiries: dict[str, int] = {}
        self.failing = failing

    async def get(self, key: str) -> bytes | None:
        if self.failing:
            raise ConnectionError("redis is down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.failing:
            raise ConnectionError("redis is down")
        self.store[key] = value.encode()
        if ex is not None:
            self.expiries[key] = ex


@pytest.fixture
def fake_redis() -> Generator[FakeRedis]:
    redis = FakeRedis()
    with patch.object(geocoding_service.cache, "client", redis):
        yield redis


@pytest.fixture
def broken_redis() -> Generator[FakeRedis]:
    redis = FakeRedis(failing=True)
    with patch.object(geocoding_service.cache, "client", redis):
        yield redis


@pytest.fixture
def no_redis() -> Generator[None]:
    with patch.object(geocoding_service.cache, "client", None):
        yield


@pytest.fixture(autouse=True)
def unthrottled() -> Generator[AsyncMock]:
    """Both limits stubbed by default; the tests that are *about* throttling opt back in."""
    with (
        patch("src.app.services.geocoding_service.enforce_rate_limit", new_callable=AsyncMock) as provider_limit,
        patch("src.app.api.v1.geocoding.enforce_rate_limit", new_callable=AsyncMock),
    ):
        yield provider_limit


class _Provider:
    """Stands in for the geocoding provider, recording every request that reaches it.

    Patches the client *construction* rather than the service function, so the headers,
    query parameters and timeout the service actually builds are under test - while
    nothing leaves the machine.
    """

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler
        self._patcher: Any = None

    def _record(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def __enter__(self) -> _Provider:
        # `_REAL_ASYNC_CLIENT`, not `httpx.AsyncClient`: the service reaches the class
        # through the same module object this file imported, so the patch below replaces it
        # here too and building one inside the factory would recurse into the mock.
        def build(**kwargs: Any) -> httpx.AsyncClient:
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(self._record), **kwargs)

        self._patcher = patch("src.app.services.geocoding_service.httpx.AsyncClient", side_effect=build)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._patcher.stop()


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> _Provider:
    return _Provider(handler)


def _responds(payload: Any, status_code: int = 200) -> _Provider:
    """A provider that answers every request with `payload`."""
    return _Provider(lambda request: httpx.Response(status_code, json=payload))


class TestReverseRoute:
    def test_returns_a_normalized_place(self, client: TestClient, no_redis: None):
        with _responds(REVERSE_PAYLOAD):
            response = client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})

        assert response.status_code == 200
        body = response.json()
        assert body["location"] == "Dahab, Egypt"
        assert body["display_name"] == "Blue Hole, Dahab, South Sinai, Egypt"
        assert body["name"] == "Blue Hole"
        assert body["attribution"] == "[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)"

    def test_answers_204_when_the_point_resolves_to_nothing(self, client: TestClient, no_redis: None):
        """Nominatim's "unable to geocode" shape is an answer, not a failure - and having no
        suggestion is a normal outcome, not a 404.

        A 204 rather than a `null` body because the client has genuinely learned something:
        this position has no name, so a location field carrying the previous pin's name can
        be cleared. See `TestCouldNotAsk` for the outcomes that look identical from here and
        must not be acted on the same way.

        Deep in the Sahara rather than mid-ocean, deliberately: open water now gets the sea's
        name instead (see `TestOffshoreFallback`), so a position out at sea would no longer
        be testing this."""
        with _responds({"error": "Unable to geocode"}):
            response = client.get("/api/v1/geocode/reverse", params={"lat": 23.4, "lon": 25.0})

        assert response.status_code == 204
        assert response.content == b""

    def test_a_cached_empty_answer_is_still_a_204(self, client: TestClient, fake_redis: FakeRedis):
        """The branch easiest to get wrong. A cached `[]` is the provider having answered, so
        the second lookup must not degrade to "could not ask" - otherwise whether a field is
        safe to clear depends on whether Redis happens to be warm."""
        with _responds({"error": "Unable to geocode"}) as provider:
            first = client.get("/api/v1/geocode/reverse", params={"lat": 23.4, "lon": 25.0})
            second = client.get("/api/v1/geocode/reverse", params={"lat": 23.4, "lon": 25.0})

        assert len(provider.requests) == 1
        assert (first.status_code, second.status_code) == (204, 204)
        assert second.content == b""

    def test_the_schema_records_both_outcomes(self, client: TestClient):
        """FastAPI will not infer the 204 from an explicit `Response`, so it is declared - and
        asserted here, since a client generated from the schema is how the web app learns the
        two answers apart."""
        outcomes = client.get("/openapi.json").json()["paths"]["/api/v1/geocode/reverse"]["get"]["responses"]

        assert "204" in outcomes
        assert "content" not in outcomes["204"]
        assert "application/json" in outcomes["200"]["content"]

    @pytest.mark.parametrize("params", [{"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": "north", "lon": 0}])
    def test_rejects_impossible_coordinates(self, client: TestClient, no_redis: None, params: dict):
        with _responds(REVERSE_PAYLOAD) as patched:
            response = client.get("/api/v1/geocode/reverse", params=params)

        assert response.status_code == 422
        assert patched.requests == []

    def test_requires_authentication(self, anonymous_client: TestClient):
        assert anonymous_client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2}).status_code == 401


class TestCouldNotAsk:
    """The three ways this instance fails to put the question to the provider. All three
    answer `200` with `null` - a success, because the diver types the location in either way
    and a 5xx would make the site form look broken over an optional convenience.

    What none of them may do is answer `204`. That would tell the client this position has no
    name, and the client acts on that by clearing a field the diver may have typed into. Each
    case uses coordinates in the Red Sea, where a genuine lookup produces a name, so a test
    that goes wrong here fails rather than passing on an accidentally nameless position."""

    def test_a_disabled_geocoder(self, client: TestClient, no_redis: None, monkeypatch: Any):
        monkeypatch.setattr(settings, "GEOCODER_URL", "")

        with _responds(REVERSE_PAYLOAD):
            response = client.get("/api/v1/geocode/reverse", params={"lat": 27.0, "lon": 35.0})

        assert response.status_code == 200
        assert response.json() is None

    def test_the_instance_being_over_its_provider_cap(self, client: TestClient, fake_redis: FakeRedis):
        """The case that forced this change. That counter is global, so a diver who nudges a
        pin twice inside a second hits it through no fault of their own - and a `204` there
        would erase a location typed by hand a round trip later."""
        with (
            _responds(REVERSE_PAYLOAD),
            patch("src.app.services.geocoding_service.anyio.sleep", new_callable=AsyncMock),
            patch("src.app.services.geocoding_service.enforce_rate_limit", new_callable=AsyncMock) as provider_limit,
        ):
            provider_limit.side_effect = RateLimitException("Too many requests. Please try again later.")

            response = client.get("/api/v1/geocode/reverse", params={"lat": 27.0, "lon": 35.0})

        assert response.status_code == 200
        assert response.json() is None
        assert fake_redis.store == {}

    def test_an_unreachable_provider(self, client: TestClient, no_redis: None):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with _transport(handler):
            response = client.get("/api/v1/geocode/reverse", params={"lat": 27.0, "lon": 35.0})

        assert response.status_code == 200
        assert response.json() is None


class TestOffshoreFallback:
    """A pin in genuinely open water gets the sea's name from the vendored polygons
    (`services.marine_areas`), because the provider has no row for such a position at all.

    The wiring is the part worth testing here rather than the geometry, which
    `test_marine_areas.py` covers: *when* it runs, and - more to the point - when it does
    not."""

    def test_open_water_is_named(self, client: TestClient, no_redis: None):
        with _responds({"error": "Unable to geocode"}):
            body = client.get("/api/v1/geocode/reverse", params={"lat": 27.0, "lon": 35.0}).json()

        assert body["location"] == "Red Sea"
        assert body["display_name"] == "Red Sea"
        assert body["name"] == "Red Sea"
        assert "Natural Earth" in body["attribution"]

    def test_echoes_the_position_that_was_asked_about(self, client: TestClient, no_redis: None):
        """Not the polygon's centroid, which would move the caller's pin several hundred
        kilometres out to sea. Rounded, like every other reverse answer."""
        with _responds({"error": "Unable to geocode"}):
            body = client.get("/api/v1/geocode/reverse", params={"lat": 27.00049, "lon": 35.0}).json()

        assert (body["latitude"], body["longitude"]) == (27.0, 35.0)

    def test_a_provider_answer_is_never_replaced(self, client: TestClient, no_redis: None):
        """The regression that matters most. Territorial waters fall inside an admin
        boundary, so a pin off Bali already reverse-geocodes to "Bali, Indonesia" - which is
        a better answer than "Bali Sea", and this must stay a fallback rather than become a
        replacement."""
        bali = {**REVERSE_PAYLOAD, "address": {"state": "Bali", "country": "Indonesia"}}

        with _responds(bali):
            body = client.get("/api/v1/geocode/reverse", params={"lat": -8.9, "lon": 115.5}).json()

        assert body["location"] == "Bali, Indonesia"

    def test_land_the_provider_could_not_name_stays_unnamed(self, client: TestClient, no_redis: None):
        with _responds({"error": "Unable to geocode"}):
            response = client.get("/api/v1/geocode/reverse", params={"lat": 23.4, "lon": 25.0})

        assert response.status_code == 204

    def test_a_disabled_geocoder_means_disabled(self, client: TestClient, no_redis: None, monkeypatch: Any):
        """Half a feature is worse than none: an operator who set `GEOCODER_URL=""` to stop
        this instance naming places should not find it still naming some of them."""
        monkeypatch.setattr(settings, "GEOCODER_URL", "")

        with _responds(REVERSE_PAYLOAD):
            response = client.get("/api/v1/geocode/reverse", params={"lat": 27.0, "lon": 35.0})

        assert response.json() is None

    def test_the_disabled_check_is_asserted_where_it_lives(self, monkeypatch: Any):
        """Through the endpoint, the test above passes with the guard deleted: `_request`
        refuses to call an unset `GEOCODER_URL` and `reverse_geocode` returns before the
        fallback is reached. The guard is belt and braces on a path production cannot take -
        cache entries are keyed by provider, so nothing written under a real one is read back
        under `""` - which is exactly why it needs asserting directly or not at all."""
        monkeypatch.setattr(settings, "GEOCODER_URL", "")

        assert geocoding_service._offshore(27.0, 35.0) is None

    def test_an_unreachable_provider_does_not_produce_a_sea_name(self, client: TestClient, no_redis: None):
        """A provider we could not reach has not told us this is open water. During an outage
        a coastal pin would otherwise be answered "Bali Sea" instead of "Bali, Indonesia",
        and that string is about to be written onto a dive site for good."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with _transport(handler):
            response = client.get("/api/v1/geocode/reverse", params={"lat": 27.0, "lon": 35.0})

        assert response.json() is None

    def test_runs_on_a_cache_hit_without_being_cached(self, client: TestClient, fake_redis: FakeRedis):
        """The provider's `[]` stays in Redis as an honest record of what it said, and the
        sea name is composed after the read. A local lookup costs a fraction of a
        millisecond, so caching it would buy nothing and would mean refreshed polygons
        waiting out an hour of stale misses."""
        with _responds({"error": "Unable to geocode"}) as provider:
            first = client.get("/api/v1/geocode/reverse", params={"lat": 27.0, "lon": 35.0})
            second = client.get("/api/v1/geocode/reverse", params={"lat": 27.0, "lon": 35.0})

        assert len(provider.requests) == 1
        assert first.json() == second.json() == {**first.json(), "location": "Red Sea"}
        (stored,) = fake_redis.store.values()
        assert json.loads(stored) == []

    @pytest.mark.parametrize("latitude,longitude", [(27.0, 35.0), (23.4, 25.0), (38.8, -76.4)])
    def test_a_named_sea_does_not_extend_how_long_the_miss_is_kept(
        self, client: TestClient, fake_redis: FakeRedis, latitude: float, longitude: float
    ):
        """Promoting a corroborated `[]` to the month-long hit TTL was tried and rejected: the
        polygons cannot tell open ocean from Chesapeake Bay, so one bad provider hour over a
        coastal cell would pin "Chesapeake Bay" for a month in place of "Annapolis, Maryland".
        Open water, land and coastal water are all asserted, because the whole objection was
        that the third behaves like the first while mattering like the second."""
        with _responds({"error": "Unable to geocode"}):
            client.get("/api/v1/geocode/reverse", params={"lat": latitude, "lon": longitude})

        (key,) = fake_redis.expiries
        assert fake_redis.expiries[key] == geocoding_service._MISS_TTL_SECONDS


class TestSearchRoute:
    def test_returns_the_matches(self, client: TestClient, no_redis: None):
        with _responds([REVERSE_PAYLOAD, {**REVERSE_PAYLOAD, "name": "Blue Hole Canyon"}]):
            response = client.get("/api/v1/geocode/search", params={"q": "blue hole dahab"})

        assert response.status_code == 200
        assert [row["name"] for row in response.json()] == ["Blue Hole", "Blue Hole Canyon"]

    def test_returns_an_empty_list_when_nothing_matches(self, client: TestClient, no_redis: None):
        with _responds([]):
            response = client.get("/api/v1/geocode/search", params={"q": "nowhere at all"})

        assert response.status_code == 200
        assert response.json() == []

    def test_bounds_the_number_of_results_itself(self, client: TestClient, no_redis: None):
        """`limit` is asked for, not relied on: a mirror that caps differently would
        otherwise have every row it sent normalized, cached for a month and returned."""
        with _responds([REVERSE_PAYLOAD] * 20):
            response = client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert len(response.json()) == geocoding_service._SEARCH_RESULT_LIMIT

    def test_counts_usable_rows_towards_the_bound_not_raw_ones(self, client: TestClient, no_redis: None):
        """Unusable leading rows must not eat the budget - a search with five junk rows in
        front of fifteen good ones is not an empty search."""
        junk = [{"display_name": "no coordinates here"}] * 5
        with _responds([*junk, *([REVERSE_PAYLOAD] * 15)]):
            response = client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert len(response.json()) == geocoding_service._SEARCH_RESULT_LIMIT

    def test_rejects_a_one_character_query(self, client: TestClient, no_redis: None):
        with _responds([]) as patched:
            response = client.get("/api/v1/geocode/search", params={"q": "d"})

        assert response.status_code == 422
        assert patched.requests == []

    def test_requires_authentication(self, anonymous_client: TestClient):
        assert anonymous_client.get("/api/v1/geocode/search", params={"q": "dahab"}).status_code == 401


class TestBoundingBox:
    """Nominatim sends a search result's extent as `boundingbox`, four strings ordered
    south, north, west, east. It rides through to the client as four named floats so a
    trip location can store it 1:1 and a map can frame the place it describes.

    The theme is that a box is a nicety: nothing here may cost a result. A row whose box
    is missing, short, unparseable or impossible keeps its name and coordinates and
    simply arrives without one.
    """

    BOX = {"boundingbox": ["9.89", "9.98", "123.35", "123.44"]}

    def _corners(self, row: dict, client: TestClient) -> tuple:
        with _responds([{**REVERSE_PAYLOAD, **row}]):
            body = client.get("/api/v1/geocode/search", params={"q": "moalboal"}).json()

        assert len(body) == 1, "the row must survive whatever its box looked like"
        return tuple(body[0][f"bbox_{corner}"] for corner in ("south", "north", "west", "east"))

    def test_a_search_result_carries_the_extent(self, client: TestClient, no_redis: None):
        assert self._corners(self.BOX, client) == (9.89, 9.98, 123.35, 123.44)

    def test_a_box_that_crosses_the_antimeridian_is_kept_as_sent(self, client: TestClient, no_redis: None):
        """West > east is what an antimeridian-crossing box looks like, and swapping the
        pair to "fix" it would frame the map on the whole planet instead of on Fiji."""
        box = {"boundingbox": ["-18.3", "-16.1", "177.0", "-179.8"]}

        assert self._corners(box, client) == (-18.3, -16.1, 177.0, -179.8)

    @pytest.mark.parametrize(
        "box",
        [
            {},
            {"boundingbox": ["9.89", "9.98", "123.35"]},
            {"boundingbox": ["9.89", "9.98", "123.35", "east"]},
            {"boundingbox": "9.89,9.98,123.35,123.44"},
            {"boundingbox": ["9.98", "9.89", "123.35", "123.44"]},
            {"boundingbox": ["9.89", "9.98", "123.35", "1234.4"]},
            {"boundingbox": ["nan", "nan", "nan", "nan"]},
        ],
    )
    def test_a_box_it_cannot_use_costs_the_row_nothing(self, client: TestClient, no_redis: None, box: dict):
        """South > north is the one ordering that carries no meaning, `nan` compares false
        against every bound it would have to satisfy, and a mirror is free to send the
        field in a shape of its own - none of which is a reason to drop the place."""
        assert self._corners(box, client) == (None, None, None, None)

    def test_a_reverse_answer_has_no_extent(self, client: TestClient, no_redis: None):
        """A pin's answer is a name for a position the caller is already holding, so
        there is nothing to frame - and a country-sized box would be actively wrong for
        one."""
        with _responds({**REVERSE_PAYLOAD, **self.BOX}):
            body = client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372}).json()

        assert body["bbox_south"] is None

    def test_the_answers_cached_before_it_existed_are_not_served(self, client: TestClient, fake_redis: FakeRedis):
        """A hit replays a stored `GeocodeResult` rather than re-normalizing the provider
        row, and search answers are kept for a month - so entries written before this
        field existed would hand back boxless results well into next month. The version
        segment in the key is what retires them, and it only works if it is *in* the key.
        """
        with _responds([{**REVERSE_PAYLOAD, **self.BOX}]):
            client.get("/api/v1/geocode/search", params={"q": "moalboal"})

        (key,) = fake_redis.store
        assert f":{geocoding_service._CACHE_VERSION}:" in key


class TestProviderContract:
    def test_sends_the_configured_user_agent(self, client: TestClient, no_redis: None):
        """Nominatim's policy blocks generic User-Agents outright."""
        with _responds(REVERSE_PAYLOAD) as patched:
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})

        assert patched.requests[0].headers["user-agent"] == settings.GEOCODER_USER_AGENT

    def test_asks_the_rounded_position_so_the_cache_key_and_the_query_agree(self, client: TestClient, no_redis: None):
        with _responds(REVERSE_PAYLOAD) as patched:
            client.get("/api/v1/geocode/reverse", params={"lat": 28.57169999, "lon": 34.5372444})

        query = dict(patched.requests[0].url.params)
        assert query["lat"] == "28.572"
        assert query["lon"] == "34.537"

    def test_asks_for_the_configured_language(self, client: TestClient, no_redis: None):
        """Unasked, Nominatim answers in the local script - and "دهب, مصر" is not what a
        diver wants written into their logbook."""
        with _responds(REVERSE_PAYLOAD) as provider:
            client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2})

        assert dict(provider.requests[0].url.params)["accept-language"] == settings.GEOCODER_LANGUAGE

    def test_a_provider_change_does_not_serve_the_old_answer(
        self, client: TestClient, fake_redis: FakeRedis, monkeypatch: Any
    ):
        """`attribution` is read from whoever answered and is a licence condition of their
        data, so cached rows must not outlive the provider that produced them - and swapping
        `GEOCODER_URL` is a `.env` edit, which `_CACHE_VERSION` cannot catch."""
        with _responds(REVERSE_PAYLOAD) as provider:
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})
            monkeypatch.setattr(settings, "GEOCODER_URL", "https://geocoder.example")
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})

        assert len(provider.requests) == 2

    def test_a_language_change_does_not_serve_the_old_answer(
        self, client: TestClient, fake_redis: FakeRedis, monkeypatch: Any
    ):
        """The language changes the answer, so it has to change the key too."""
        with _responds(REVERSE_PAYLOAD) as provider:
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})
            monkeypatch.setattr(settings, "GEOCODER_LANGUAGE", "de")
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})

        assert len(provider.requests) == 2

    def test_sends_no_key_when_none_is_configured(self, client: TestClient, no_redis: None):
        with _responds(REVERSE_PAYLOAD) as patched:
            client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2})

        assert "key" not in dict(patched.requests[0].url.params)

    def test_sends_the_key_when_one_is_configured(self, client: TestClient, no_redis: None, monkeypatch: Any):
        monkeypatch.setattr(settings, "GEOCODER_API_KEY", "secret-key")

        with _responds(REVERSE_PAYLOAD) as patched:
            client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2})

        assert dict(patched.requests[0].url.params)["key"] == "secret-key"

    def test_the_api_key_never_reaches_the_log(self, client: TestClient, no_redis: None, monkeypatch: Any, caplog):
        """The key rides in the query string because that is where Nominatim-compatible
        mirrors want it - and httpx logs the *full URL* of every request it makes at INFO.
        `core.setup` pins that logger to WARNING for exactly this reason; this asserts it,
        at INFO, so nobody removes the line and finds out from a log aggregator.
        """
        monkeypatch.setattr(settings, "GEOCODER_API_KEY", "super-secret-key")

        with caplog.at_level(logging.INFO), _responds({"error": "Bandwidth limit exceeded"}):
            client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2})

        assert caplog.records, "expected the refusal to be logged at all"
        assert "super-secret-key" not in caplog.text

    def test_lowercases_and_collapses_the_search_query(self, client: TestClient, no_redis: None):
        with _responds([]) as patched:
            client.get("/api/v1/geocode/search", params={"q": "  Blue   HOLE  "})

        assert dict(patched.requests[0].url.params)["q"] == "blue hole"


class TestDegradation:
    """Every one of these would be a 5xx if the service let the failure through."""

    def test_survives_a_timeout(self, client: TestClient, no_redis: None):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with _transport(handler):
            response = client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2})

        assert response.status_code == 200
        assert response.json() is None

    def test_a_refusal_is_bounded_and_kept_to_one_line_in_the_log(self, client: TestClient, no_redis: None, caplog):
        """The only provider string that reaches a log rather than `_normalize`. It arrives
        from a body bounded at half a megabyte, and a value carrying newlines can forge
        entries around itself in anything that parses the file afterwards."""
        refusal = "Bandwidth limit exceeded\nWARNING fake log line\n" + "x" * 5000

        with caplog.at_level(logging.WARNING), _responds({"error": refusal}):
            client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2})

        (record,) = [r for r in caplog.records if "Geocoder refused" in r.message]
        logged = record.getMessage()
        assert "\n" not in logged
        assert len(logged) < 300

    def test_a_refusal_is_not_mistaken_for_an_empty_answer(self, client: TestClient, fake_redis: FakeRedis):
        """Nominatim wears the same `{"error": ...}` shape for a bandwidth or abuse
        complaint as for "unable to geocode". Cached as a miss, one bad minute would pin a
        genuine place as "no result" for an hour."""
        with _responds({"error": "Bandwidth limit exceeded"}) as provider:
            first = client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})

        assert first.status_code == 200
        assert first.json() is None
        assert fake_redis.store == {}
        assert len(provider.requests) == 2

    def test_survives_a_provider_error_status(self, client: TestClient, no_redis: None):
        with _responds({"message": "over capacity"}, status_code=503):
            response = client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert response.status_code == 200
        assert response.json() == []

    def test_survives_a_non_json_body(self, client: TestClient, no_redis: None):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>maintenance</html>")

        with _transport(handler):
            response = client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert response.status_code == 200
        assert response.json() == []

    def test_a_body_that_is_neither_object_nor_array_is_a_failure(self, client: TestClient, fake_redis: FakeRedis):
        """Valid JSON that isn't a Nominatim shape means a proxy or the wrong host answered,
        not "no such place" - so it must not be cached as one."""
        with _responds("service unavailable"):
            response = client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert response.json() == []
        assert fake_redis.store == {}

    def test_truncates_provider_strings_rather_than_dropping_the_row(self, client: TestClient, no_redis: None):
        """An over-long `display_name` would otherwise raise inside the normalizer and turn
        one verbose row into a failed lookup. The licence is the exception - it is replaced
        rather than clipped, see `TestShortLocation`."""
        with _responds({**REVERSE_PAYLOAD, "display_name": "x" * 2000, "name": "y" * 2000}):
            body = client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2}).json()

        assert len(body["display_name"]) == 512
        assert len(body["name"]) == 255

    def test_survives_an_oversized_body(self, client: TestClient, fake_redis: FakeRedis):
        """The per-read timeout bounds each read, not the response - so the size cap is what
        stops a host making this process buffer without limit. Never cached: a truncated
        read is a failure, not "no such place"."""
        huge = [{**REVERSE_PAYLOAD, "display_name": "x" * 1000} for _ in range(2000)]

        with _responds(huge):
            response = client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert response.status_code == 200
        assert response.json() == []
        assert fake_redis.store == {}

    def test_survives_a_malformed_geocoder_url(self, client: TestClient, no_redis: None, monkeypatch: Any):
        """`httpx.InvalidURL` descends from `Exception`, not `HTTPError`, so a typo'd port in
        an operator's `.env` used to escape as a 500."""
        monkeypatch.setattr(settings, "GEOCODER_URL", "http://nominatim.example:8O80")

        with _responds(REVERSE_PAYLOAD):
            response = client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2})

        assert response.status_code == 200
        assert response.json() is None

    def test_makes_no_call_at_all_when_the_geocoder_is_switched_off(
        self, client: TestClient, no_redis: None, monkeypatch: Any
    ):
        monkeypatch.setattr(settings, "GEOCODER_URL", "")

        with _responds(REVERSE_PAYLOAD) as patched:
            response = client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2})

        assert response.status_code == 200
        assert response.json() is None
        assert patched.requests == []

    def test_drops_rows_the_provider_sent_without_coordinates(self, client: TestClient, no_redis: None):
        with _responds([{"display_name": "Somewhere"}, REVERSE_PAYLOAD]):
            response = client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert [row["name"] for row in response.json()] == ["Blue Hole"]


class TestCaching:
    def test_a_second_identical_lookup_never_reaches_the_provider(self, client: TestClient, fake_redis: FakeRedis):
        """The single assertion Nominatim's terms turn on."""
        with _responds(REVERSE_PAYLOAD) as patched:
            first = client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})
            second = client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})

        assert len(patched.requests) == 1
        assert first.json() == second.json()

    def test_positions_within_the_same_cell_share_one_answer(self, client: TestClient, fake_redis: FakeRedis):
        with _responds(REVERSE_PAYLOAD) as patched:
            client.get("/api/v1/geocode/reverse", params={"lat": 28.57171, "lon": 34.53719})
            client.get("/api/v1/geocode/reverse", params={"lat": 28.57174, "lon": 34.53722})

        assert len(patched.requests) == 1

    def test_caches_an_empty_answer_but_expires_it_sooner(self, client: TestClient, fake_redis: FakeRedis):
        """A miss is far more likely to be provider weirdness than a fact about the world,
        so it must not be pinned for a month - but it must still be cached, or a retrying
        client re-asks on every keystroke."""
        with _responds([]) as patched:
            client.get("/api/v1/geocode/search", params={"q": "nowhere"})
            client.get("/api/v1/geocode/search", params={"q": "nowhere"})

        assert len(patched.requests) == 1
        (key,) = fake_redis.expiries
        assert fake_redis.expiries[key] == geocoding_service._MISS_TTL_SECONDS

    def test_does_not_cache_a_failure(self, client: TestClient, fake_redis: FakeRedis):
        """A thirty-second outage must not become a month of empty answers."""
        with _responds({"message": "over capacity"}, status_code=503):
            client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert fake_redis.store == {}

    def test_one_lookup_serves_every_user(self, geocode_app: Any, client: TestClient, fake_redis: FakeRedis):
        """Deliberately unlike every other cache key in this app: the answer is a fact about
        the world, so one lookup has to serve every diver who pins that spot - keying it per
        user would multiply outbound calls by the number of accounts.

        The `geocode:` prefix also keeps it clear of the `user_{id}_*` namespace
        `services.cache_invalidation` sweeps by pattern.
        """
        with _responds(REVERSE_PAYLOAD) as provider:
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})
            geocode_app.dependency_overrides[get_current_user] = lambda: {**CURRENT_USER, "id": 99}
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})

        assert len(provider.requests) == 1
        (key,) = fake_redis.store
        assert key.startswith("geocode:")

    def test_a_redis_outage_is_a_miss_not_an_error(self, client: TestClient, broken_redis: FakeRedis):
        """Both the read and the write raise here. Geocoding is a suggestion; losing the
        cache should cost the provider an extra call, not cost the diver their answer."""
        with _responds(REVERSE_PAYLOAD) as provider:
            response = client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})

        assert response.status_code == 200
        assert response.json()["location"] == "Dahab, Egypt"
        assert len(provider.requests) == 1

    def test_an_unreadable_cache_entry_is_treated_as_a_miss(self, client: TestClient, fake_redis: FakeRedis):
        with _responds(REVERSE_PAYLOAD) as patched:
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})
            (key,) = list(fake_redis.store)
            fake_redis.store[key] = json.dumps([{"latitude": "not a number"}]).encode()

            response = client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})

        assert len(patched.requests) == 2
        assert response.json()["location"] == "Dahab, Egypt"


class TestThrottling:
    def test_the_provider_cap_is_only_spent_on_calls_that_leave(self, client: TestClient, fake_redis: FakeRedis):
        """A cache hit costs the provider nothing, so it must not consume the one-per-second
        allowance either - otherwise a page full of cached sites 429s for no reason."""
        with (
            _responds(REVERSE_PAYLOAD),
            patch("src.app.services.geocoding_service.enforce_rate_limit", new_callable=AsyncMock) as provider_limit,
        ):
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})
            client.get("/api/v1/geocode/reverse", params={"lat": 28.5717, "lon": 34.5372})

        assert provider_limit.await_count == 1
        assert [call.args[0] for call in provider_limit.await_args_list] == ["geocode:provider"]

    def test_the_per_user_limit_is_keyed_by_user(self, client: TestClient, no_redis: None):
        with (
            _responds(REVERSE_PAYLOAD),
            patch("src.app.api.v1.geocoding.enforce_rate_limit", new_callable=AsyncMock) as user_limit,
        ):
            client.get("/api/v1/geocode/reverse", params={"lat": 1, "lon": 2})

        assert [call.args[0] for call in user_limit.await_args_list] == ["geocode:user:7"]

    def test_a_throttled_caller_gets_429_and_no_provider_call(self, client: TestClient, no_redis: None):
        with (
            _responds(REVERSE_PAYLOAD) as patched,
            patch("src.app.api.v1.geocoding.enforce_rate_limit", new_callable=AsyncMock) as user_limit,
        ):
            user_limit.side_effect = RateLimitException("Too many requests. Please try again later.")

            response = client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert response.status_code == 429
        assert patched.requests == []

    def test_a_sustained_provider_cap_degrades_instead_of_rejecting(self, client: TestClient, fake_redis: FakeRedis):
        """The provider counter is global, so raising would mean one diver's search
        rejecting another's. The call is skipped, nothing is cached, and the caller gets the
        same "no suggestion" a provider outage produces."""
        with (
            _responds(REVERSE_PAYLOAD) as provider,
            patch("src.app.services.geocoding_service.anyio.sleep", new_callable=AsyncMock),
            patch("src.app.services.geocoding_service.enforce_rate_limit", new_callable=AsyncMock) as provider_limit,
        ):
            provider_limit.side_effect = RateLimitException("Too many requests. Please try again later.")

            response = client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert response.status_code == 200
        assert response.json() == []
        assert provider.requests == []
        assert fake_redis.store == {}

    def test_waits_out_the_provider_cap_once_before_giving_up(self, client: TestClient, no_redis: None):
        """`[]` is byte-identical to "nothing matched", so a diver told a place doesn't exist
        has no way to know they were merely unlucky with the window. One wait turns the
        common collision - two type-ahead queries in the same second - into a slow right
        answer rather than a confidently wrong one."""
        with (
            _responds([REVERSE_PAYLOAD]) as provider,
            patch("src.app.services.geocoding_service.anyio.sleep", new_callable=AsyncMock) as slept,
            patch("src.app.services.geocoding_service.enforce_rate_limit", new_callable=AsyncMock) as provider_limit,
        ):
            provider_limit.side_effect = [RateLimitException("Too many requests."), None]

            response = client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert [row["name"] for row in response.json()] == ["Blue Hole"]
        assert len(provider.requests) == 1
        slept.assert_awaited_once()

    def test_never_waits_longer_than_a_second(self, client: TestClient, no_redis: None, monkeypatch: Any):
        """Capped independently of the window, so an operator who throttles their own
        Nominatim gently can't turn that into a request held open for a minute."""
        monkeypatch.setattr(settings, "GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS", 60)

        with (
            _responds([]),
            patch("src.app.services.geocoding_service.anyio.sleep", new_callable=AsyncMock) as slept,
            patch("src.app.services.geocoding_service.enforce_rate_limit", new_callable=AsyncMock) as provider_limit,
        ):
            provider_limit.side_effect = RateLimitException("Too many requests.")

            client.get("/api/v1/geocode/search", params={"q": "dahab"})

        assert [call.args[0] for call in slept.await_args_list] == [1.0]


class TestAttribution:
    """The credit is a wire format with a parser on the other end, not display copy.

    The clients read one markdown shape - `[text](url)` - and render anything else as plain
    text, so what matters here is which provider strings fold and, far more, which ones are
    left alone. The strings below are the real values those providers send; see
    `DECISIONS.md`."""

    def test_folds_a_trailing_url_into_a_link(self):
        """Nominatim's shape, and the only one in reach that folds."""
        credit = "Data © OpenStreetMap contributors, ODbL 1.0. http://osm.org/copyright"

        assert (
            geocoding_service._linked_attribution(credit)
            == "[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)"
        )

    def test_upgrades_the_scheme_of_the_href_but_not_the_visible_text(self):
        """The one part of a provider's string that is rewritten rather than moved: we are
        minting an href a browser will follow, and `http://osm.org` redirects to TLS anyway."""
        folded = geocoding_service._linked_attribution("Credit here http://example.com/licence")

        assert folded == "[Credit here](https://example.com/licence)"

    def test_leaves_a_bare_url_alone_rather_than_making_an_empty_label(self):
        """LocationIQ's entire `licence` is the URL. Folding it would yield
        `[](https://locationiq.com/attribution)` - a link crediting nobody, which is worse
        than the bare URL it replaced. This is why the text before the URL is required."""
        credit = "https://locationiq.com/attribution"

        assert geocoding_service._linked_attribution(credit) == credit

    @pytest.mark.parametrize(
        ("shape", "credit"),
        [
            ("text with no url", "© LocationIQ.com CC BY 4.0, Data © OpenStreetMap contributors, ODbL 1.0"),
            ("a licence name alone", "ODbL"),
            (
                "a url mid-sentence",
                "NOTICE: © 2026 Mapbox and its suppliers. Terms of Service "
                "(https://www.mapbox.com/about/maps/). This response is made available.",
            ),
            (
                "html the provider already linked",
                '<a href="https://www.maptiler.com/copyright/">&copy; MapTiler</a> '
                '<a href="https://www.openstreetmap.org/copyright">&copy; OpenStreetMap contributors</a>',
            ),
        ],
    )
    def test_passes_through_every_other_provider_shape(self, shape: str, credit: str):
        """None of these has a *trailing* bare URL, so none of them matches - by construction
        rather than by a special case per provider. `GEOCODER_URL` is an operator setting and
        the string belongs to whoever answered."""
        assert geocoding_service._linked_attribution(credit) == credit, shape

    def test_keeps_a_sentences_full_stop_out_of_the_href(self):
        folded = geocoding_service._linked_attribution("Credit, see http://example.com/licence.")

        assert folded == "[Credit, see](https://example.com/licence)."

    def test_folding_is_idempotent(self):
        """Rows are cached for a month, so a folded value gets re-read and re-normalized. The
        folded form's URL is preceded by `(` rather than whitespace, so there is nothing left
        to match - but the cache TTL makes this a requirement, not a happy accident."""
        once = geocoding_service._linked_attribution(REVERSE_PAYLOAD["licence"])

        assert geocoding_service._linked_attribution(once) == once

    @pytest.mark.parametrize(
        "credit",
        [geocoding_service._DEFAULT_ATTRIBUTION, geocoding_service._MARINE_ATTRIBUTION],
    )
    def test_the_built_in_credits_are_already_folded(self, credit: str):
        """The two credits this module supplies itself are written in the shape the fold
        produces, so a reader cannot tell from the shape whether the provider answered or we
        fell back. This is what holds the literals and the transform together."""
        assert geocoding_service._linked_attribution(credit) == credit
        assert credit.startswith("[") and "](" in credit

    def test_measures_the_length_cap_against_what_actually_ships(self):
        """Folding grows the string by up to four characters, so a licence just under the cap
        would clear the guard and then fail `GeocodeResult`'s own `max_length`. Sized to land
        in that gap: long enough that folding pushes it over 255, short enough that it starts
        under."""
        prefix, url = "Licensed under ", " https://example.com/l"
        licence = prefix + "x" * (geocoding_service._ATTRIBUTION_MAX_LENGTH - len(prefix) - len(url)) + url
        assert len(licence) == geocoding_service._ATTRIBUTION_MAX_LENGTH
        assert len(geocoding_service._linked_attribution(licence)) > geocoding_service._ATTRIBUTION_MAX_LENGTH

        result = geocoding_service._normalize({"lat": "1", "lon": "2", "display_name": "Somewhere", "licence": licence})

        assert result is not None
        assert result.attribution == geocoding_service._DEFAULT_ATTRIBUTION


class TestShortLocation:
    """`location` is what gets persisted onto `dive_site.location`, so it is composed from
    the structured address rather than trimmed out of `display_name`."""

    @pytest.mark.parametrize(
        "address,expected",
        [
            ({"city": "Dahab", "state": "South Sinai", "country": "Egypt"}, "Dahab, Egypt"),
            ({"village": "Marsa Shagra", "country": "Egypt"}, "Marsa Shagra, Egypt"),
            ({"state": "South Sinai", "country": "Egypt"}, "South Sinai, Egypt"),
            ({"country": "Egypt"}, "Egypt"),
        ],
    )
    def test_composes_place_and_country(self, address: dict, expected: str):
        row = {**REVERSE_PAYLOAD, "address": address}
        result = geocoding_service._normalize(row)

        assert result is not None
        assert result.location == expected

    def test_falls_back_to_the_display_name_with_no_address(self):
        """A named bay or reef: the feature's own name is the best answer available."""
        row = {"lat": "25.34", "lon": "34.77", "display_name": "Marsa Abu Dabbab", "address": {}}
        result = geocoding_service._normalize(row)

        assert result is not None
        assert result.location == "Marsa Abu Dabbab"

    def test_truncates_to_the_width_of_the_column_it_is_headed_for(self):
        result = geocoding_service._normalize({"lat": "1", "lon": "2", "display_name": "x" * 400})

        assert result is not None
        assert len(result.location) == 255

    def test_falls_back_to_a_default_attribution(self):
        """A result never goes out uncredited, whatever the provider sent."""
        result = geocoding_service._normalize({"lat": "1", "lon": "2", "display_name": "Somewhere"})

        assert result is not None
        assert "OpenStreetMap" in result.attribution

    def test_replaces_an_over_long_licence_rather_than_cutting_it(self):
        """The one provider string not truncated: a label clipped mid-word is still a usable
        label, a licence notice clipped mid-sentence is not attribution at all."""
        row = {"lat": "1", "lon": "2", "display_name": "Somewhere", "licence": "Licensed under " + "x" * 400}
        result = geocoding_service._normalize(row)

        assert result is not None
        assert result.attribution == geocoding_service._DEFAULT_ATTRIBUTION

    @pytest.mark.parametrize(
        "row",
        [
            {"lon": "2", "display_name": "No latitude"},
            {"lat": "not a number", "lon": "2", "display_name": "Unparseable"},
            {"lat": "1", "lon": "2"},
            {"lat": "95", "lon": "2", "display_name": "Off the planet"},
        ],
    )
    def test_drops_unusable_rows(self, row: dict):
        assert geocoding_service._normalize(row) is None
