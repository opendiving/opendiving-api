"""Tests for the species catalog - the routes (`api/v1/species.py`), the service
underneath them (`services/species_service.py`), and the dive join CRUD.

Three themes, and the first two are the ones that would silently rot.

**Degradation.** Search fans out to two registers this app does not control, and its one
promise is that it never fails because of them: either register down, both down, Redis down,
a provider sending a shape nobody expected - the answer still comes back, from the local
catalog if that is all there is. `TestSearchDegrades` is that promise written down. Resolve
is the deliberate exception and 503s, because a catalog row is shared with every account and
never rewritten.

**Merging two registers.** WoRMS owns the taxonomy and Wikidata owns the common names, and
the whole design turns on merging them on the AphiaID they share. The accepted-taxon fold -
a diver types *Manta birostris* and gets *Mobula birostris* with `matched_name` set - is the
part a client renders, so it is pinned here rather than left to the browser.

Written in both established styles: the app-instance-with-mocked-httpx of
`test_geocoding.py` for the wire shape, and the monkeypatched-collaborator of
`test_dive_update.py` for route logic. The database-backed constraint and stats classes are
Postgres-marked and skip without one, which is what `CONTRIBUTING.md`'s skip-count check
exists to catch.
"""

import json
import uuid as uuid_pkg
from collections.abc import Callable, Generator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool
from uuid6 import uuid7

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import dives as dives_module
from src.app.api.v1.species import read_species
from src.app.core.config import settings
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.core.setup import create_application
from src.app.crud.crud_dive_species import replace_species_for_dive
from src.app.models.dive_species import DiveSpecies
from src.app.models.species import Species
from src.app.models.species_name import SpeciesName
from src.app.schemas.dive import DiveCreateRequest, SpeciesInfo
from src.app.schemas.species import SpeciesSearchResponse
from src.app.services import species_service
from src.app.services.dive_stats import recalculate_dive_stats
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_species, create_user

_REAL_ASYNC_CLIENT = httpx.AsyncClient

CURRENT_USER = {"id": 7, "uuid": uuid7(), "username": "ada", "is_superuser": False}

# *Amphiprion ocellaris* as WoRMS actually sends it - the accepted, species-rank record the
# whole feature is demonstrated on. Trimmed to the fields this app reads, keeping WoRMS's own
# key spellings (`scientificname`, the bare `class`/`order`, the 1/0 habitat flags), because
# those spellings are exactly what a normalizer gets wrong.
CLOWNFISH_RECORD = {
    "AphiaID": 278400,
    "scientificname": "Amphiprion ocellaris",
    "authority": "Cuvier, 1830",
    "status": "accepted",
    "rank": "Species",
    "valid_AphiaID": 278400,
    "valid_name": "Amphiprion ocellaris",
    "kingdom": "Animalia",
    "phylum": "Chordata",
    "class": "Teleostei",
    "order": "Perciformes",
    "family": "Pomacentridae",
    "genus": "Amphiprion",
    "isMarine": 1,
    "isBrackish": 0,
    "isFreshwater": 0,
}

# The synonym path: *Manta birostris* is unaccepted and points at *Mobula birostris*. WoRMS
# sends `valid_AphiaID`/`valid_name` inline on the unaccepted record, which is what lets the
# fold happen without a second request.
MANTA_SYNONYM_RECORD = {
    "AphiaID": 105857,
    "scientificname": "Manta birostris",
    "status": "unaccepted",
    "rank": "Species",
    "valid_AphiaID": 1015526,
    "valid_name": "Mobula birostris",
    "genus": "Manta",
}

WIKIDATA_SEARCH = {"query": {"search": [{"title": "Q1126155"}]}}
WIKIDATA_ENTITIES = {
    "entities": {
        "Q1126155": {
            # The English *label* is the binomial itself, which is the norm for taxa and the
            # exact reason `_choose_common_name` prefers a label that differs from it.
            "labels": {"en": {"language": "en", "value": "Amphiprion ocellaris"}},
            "aliases": {"en": [{"value": "ocellaris clownfish"}, {"value": "Common clownfish"}]},
            "claims": {
                # External identifiers are strings in Wikidata, whatever they look like.
                "P850": [{"mainsnak": {"datavalue": {"value": "278400"}}}],
                "P225": [{"mainsnak": {"datavalue": {"value": "Amphiprion ocellaris"}}}],
            },
        }
    }
}


@pytest.fixture(scope="module")
def species_app() -> Any:
    """Its own app with `create_tables_on_start=False`, like `test_geocoding.py` - the route
    tests here stub the database out entirely."""
    return create_application(router=router, settings=settings, create_tables_on_start=False)


@pytest.fixture
def client(species_app: Any) -> Generator[TestClient]:
    species_app.dependency_overrides[get_current_user] = lambda: CURRENT_USER
    with TestClient(species_app) as test_client:
        yield test_client
    species_app.dependency_overrides = {}


@pytest.fixture
def anonymous_client(species_app: Any) -> Generator[TestClient]:
    with TestClient(species_app) as test_client:
        yield test_client
    species_app.dependency_overrides = {}


class FakeRedis:
    """Enough of the Redis client for this module. `failing=True` makes both calls raise,
    which is how "a cache outage is a miss, not an error" gets exercised."""

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
    with patch.object(species_service.cache, "client", redis):
        yield redis


@pytest.fixture
def no_redis() -> Generator[None]:
    with patch.object(species_service.cache, "client", None):
        yield


@pytest.fixture(autouse=True)
def unthrottled() -> Generator[AsyncMock]:
    """Both limits stubbed by default; the tests that are *about* throttling opt back in."""
    with (
        patch("src.app.services.species_service.enforce_rate_limit", new_callable=AsyncMock) as provider_limit,
        patch("src.app.api.v1.species.enforce_rate_limit", new_callable=AsyncMock),
    ):
        yield provider_limit


class _Providers:
    """Stands in for WoRMS and Wikidata, recording every request that reaches either.

    Patches the client *construction* rather than the service's own functions, so the
    headers, query parameters and timeout the service actually builds are under test while
    nothing leaves the machine. Routing is by URL, since both registers are reached through
    one `_request`.
    """

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler
        self._patcher: Any = None

    def _record(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    def __enter__(self) -> _Providers:
        # `_REAL_ASYNC_CLIENT`, not `httpx.AsyncClient`: the service reaches the class
        # through the same module object this file imported, so the patch below replaces it
        # there too and building one inside the factory would recurse into the mock.
        def build(**kwargs: Any) -> httpx.AsyncClient:
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(self._record), **kwargs)

        self._patcher = patch("src.app.services.species_service.httpx.AsyncClient", side_effect=build)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._patcher.stop()


def _registers(
    *,
    by_name: Any = (),
    by_vernacular: Any = (),
    record: Any = None,
    synonyms: Any = (),
    vernaculars: Any = (),
    wikidata_search: Any = None,
    wikidata_entities: Any = None,
    worms_status: int = 200,
    wikidata_status: int = 200,
) -> _Providers:
    """Both registers answering from canned payloads, routed by path.

    Defaults are "answered, and had nothing", which is the shape a lot of these tests want
    for the source they are *not* exercising.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "wikidata" in url:
            if "wbgetentities" in url:
                return httpx.Response(wikidata_status, json=wikidata_entities or {"entities": {}})
            return httpx.Response(wikidata_status, json=wikidata_search or {"query": {"search": []}})
        if "AphiaRecordsByName" in url:
            return httpx.Response(worms_status, json=list(by_name))
        if "AphiaRecordsByVernacular" in url:
            return httpx.Response(worms_status, json=list(by_vernacular))
        if "AphiaRecordByAphiaID" in url:
            return httpx.Response(worms_status, json=record)
        if "AphiaSynonymsByAphiaID" in url:
            return httpx.Response(worms_status, json=list(synonyms))
        if "AphiaVernacularsByAphiaID" in url:
            return httpx.Response(worms_status, json=list(vernaculars))
        raise AssertionError(f"unexpected request to {url}")

    return _Providers(handle)


def _unreachable(host: str) -> _Providers:
    """One register that raises on every request; the other answers with nothing."""

    def handle(request: httpx.Request) -> httpx.Response:
        if host in str(request.url):
            raise httpx.ConnectError("unreachable")
        if "wbgetentities" in str(request.url):
            return httpx.Response(200, json={"entities": {}})
        if "wikidata" in str(request.url):
            return httpx.Response(200, json={"query": {"search": []}})
        return httpx.Response(200, json=[])

    return _Providers(handle)


def _empty_db() -> MagicMock:
    """A session whose every query returns nothing - "the catalog is empty"."""
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = []
    result.__iter__ = lambda self: iter(())
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    db.scalar = AsyncMock(return_value=None)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


# -------------- the merge, which is the whole design --------------


class TestMergingTwoRegisters:
    @pytest.mark.asyncio
    async def test_a_wikidata_common_name_lands_on_the_worms_record(self, no_redis: None):
        """The feature in one assertion. WoRMS supplies the taxonomy and Wikidata the name a
        diver would actually type, and they arrive as two rows that have to become one -
        which is only possible because both carry the same AphiaID.
        """
        db = _empty_db()
        with _registers(
            by_name=[CLOWNFISH_RECORD], wikidata_search=WIKIDATA_SEARCH, wikidata_entities=WIKIDATA_ENTITIES
        ):
            response = await species_service.search_species(db, "amphiprion ocellaris")

        assert len(response.results) == 1
        result = response.results[0]
        assert result.aphia_id == 278400
        assert result.scientific_name == "Amphiprion ocellaris"
        assert result.common_name == "ocellaris clownfish"
        # WoRMS wrote the row, so its rank survives rather than Wikidata's placeholder.
        assert (result.rank, result.source) == ("Species", "worms")

    @pytest.mark.asyncio
    async def test_a_species_only_wikidata_knows_still_comes_back(self, no_redis: None):
        """The case that justifies the second register at all: "clownfish" returns nothing
        useful from WoRMS, whose only vernacular for this taxon is in Japanese."""
        db = _empty_db()
        with _registers(wikidata_search=WIKIDATA_SEARCH, wikidata_entities=WIKIDATA_ENTITIES):
            response = await species_service.search_species(db, "clownfish")

        assert [(r.aphia_id, r.common_name, r.source) for r in response.results] == [
            (278400, "ocellaris clownfish", "wikidata")
        ]

    @pytest.mark.asyncio
    async def test_an_unaccepted_name_folds_onto_the_accepted_taxon(self, no_redis: None):
        """A diver who learned *Manta birostris* types it and gets *Mobula birostris*.

        `matched_name` is what makes that legible rather than baffling: without it the picker
        shows a binomial the diver did not type and nothing explains why. The web client
        renders it, so it is part of the contract, not an internal detail.
        """
        db = _empty_db()
        with _registers(by_name=[MANTA_SYNONYM_RECORD]):
            response = await species_service.search_species(db, "manta birostris")

        result = response.results[0]
        assert result.scientific_name == "Mobula birostris"
        assert result.matched_name == "Manta birostris"
        assert result.aphia_id == MANTA_SYNONYM_RECORD["valid_AphiaID"]
        # The row now describes the accepted taxon, so reporting the synonym's "unaccepted"
        # would be labelling the wrong thing.
        assert result.status == "accepted"

    @pytest.mark.asyncio
    async def test_an_entity_without_an_aphia_id_is_dropped(self, no_redis: None):
        """Belt and braces - the `haswbstatement:P850` filter should prevent it - because a
        hit with no AphiaID is one the client cannot resolve, which is worse than one fewer
        row."""
        db = _empty_db()
        entities = {"entities": {"Q999": {"labels": {"en": {"value": "Something"}}, "claims": {}}}}
        with _registers(wikidata_search={"query": {"search": [{"title": "Q999"}]}}, wikidata_entities=entities):
            response = await species_service.search_species(db, "something")

        assert response.results == []

    @pytest.mark.asyncio
    async def test_exact_matches_rank_above_the_rest(self, no_redis: None):
        db = _empty_db()
        other = {**CLOWNFISH_RECORD, "AphiaID": 999, "scientificname": "Amphiprion percula", "valid_AphiaID": 999}
        with _registers(by_name=[other, CLOWNFISH_RECORD]):
            response = await species_service.search_species(db, "amphiprion ocellaris")

        assert [r.scientific_name for r in response.results] == ["Amphiprion ocellaris", "Amphiprion percula"]


class TestChoosingTheDisplayName:
    """`_choose_common_name`, whose order is forced by what the sources contain rather than
    by preference: a taxon's English Wikidata label is usually the binomial itself."""

    def test_a_label_that_repeats_the_binomial_is_not_a_common_name(self):
        assert (
            species_service._choose_common_name(
                scientific_name="Amphiprion ocellaris",
                label="Amphiprion ocellaris",
                aliases=("ocellaris clownfish",),
            )
            == "ocellaris clownfish"
        )

    def test_a_label_that_differs_wins_outright(self):
        assert (
            species_service._choose_common_name(
                scientific_name="Mobula birostris", label="giant oceanic manta ray", aliases=("manta ray",)
            )
            == "giant oceanic manta ray"
        )

    def test_a_worms_vernacular_is_the_last_resort(self):
        """Last because WoRMS's English coverage is the thin part - but still tried, since a
        taxon Wikidata has never heard of may well have one."""
        assert (
            species_service._choose_common_name(
                scientific_name="Muraenidae", label=None, aliases=(), vernaculars=("moray eels",)
            )
            == "moray eels"
        )

    def test_nothing_offered_means_falling_back_to_the_scientific_name(self):
        assert species_service._choose_common_name(scientific_name="Muraenidae", label=None, aliases=()) is None

    def test_a_label_that_is_the_binomial_plus_its_authority_is_not_a_common_name(self):
        """Found by resolving a real taxon: Wikidata labels obscure species with the binomial
        and its authority, and an equality test calls that "different from the scientific
        name" and ships it as the common name. A prefix test is what rejects it."""
        assert (
            species_service._choose_common_name(
                scientific_name="Leptasterias (Leptasterias) muelleri muelleri",
                label="Leptasterias (Leptasterias) muelleri muelleri (M. Sars, 1846)",
                aliases=(),
            )
            is None
        )

    def test_a_real_common_name_is_not_caught_by_the_prefix_test(self):
        """The prefix rule must not be so eager that it rejects genuine names. Neither
        "ocellaris clownfish" nor "Giant oceanic manta ray" begins with its binomial."""
        assert (
            species_service._choose_common_name(
                scientific_name="Amphiprion ocellaris", label=None, aliases=("ocellaris clownfish",)
            )
            == "ocellaris clownfish"
        )

    def test_the_comparison_ignores_case(self):
        """ "Amphiprion Ocellaris" is the scientific name wearing a capital, not a common
        name, and shipping it would put the same string in both columns."""
        assert (
            species_service._choose_common_name(
                scientific_name="Amphiprion ocellaris", label="Amphiprion Ocellaris", aliases=()
            )
            is None
        )


# -------------- degradation, which is the module's one promise --------------


class TestSearchDegrades:
    @pytest.mark.asyncio
    async def test_worms_down_still_returns_wikidata(self, no_redis: None):
        db = _empty_db()
        with _unreachable("marinespecies.org"):
            response = await species_service.search_species(db, "clownfish")

        assert response.results == []  # this fixture's Wikidata has nothing, but nothing raised

    @pytest.mark.asyncio
    async def test_one_register_failing_does_not_cancel_the_other(self, no_redis: None):
        """The failure mode the per-task guard exists for: the three fetches share a task
        group, so an exception escaping one would cancel the other two and turn one
        register's bad day into a search that finds nothing."""
        db = _empty_db()

        def handle(request: httpx.Request) -> httpx.Response:
            if "wikidata" in str(request.url):
                raise httpx.ConnectError("unreachable")
            if "AphiaRecordsByName" in str(request.url):
                return httpx.Response(200, json=[CLOWNFISH_RECORD])
            return httpx.Response(200, json=[])

        with _Providers(handle):
            response = await species_service.search_species(db, "amphiprion ocellaris")

        assert [r.aphia_id for r in response.results] == [278400]

    @pytest.mark.asyncio
    async def test_both_registers_down_falls_back_to_the_catalog(self, no_redis: None):
        """The reason the local catalog rides in front: a species anybody has ever logged
        stays findable with both providers unreachable and Redis cold."""
        db = _empty_db()
        local = MagicMock()
        local.uuid = uuid7()
        local.aphia_id = 278400
        local.scientific_name = "Amphiprion ocellaris"
        local.common_name = "ocellaris clownfish"
        local.rank = "Species"
        local.status = "accepted"
        local.matched_name = None
        db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[local])))

        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

        with _Providers(handle):
            response = await species_service.search_species(db, "clownfish")

        assert [(r.aphia_id, r.source) for r in response.results] == [(278400, "catalog")]
        assert response.results[0].uuid == local.uuid

    @pytest.mark.asyncio
    async def test_a_garbage_body_is_a_failure_not_an_empty_answer(self, no_redis: None):
        """Caching "the register sent junk" as `[]` would turn a bad minute into a month of
        empty answers, so a body that does not parse must not reach the cache at all."""
        db = _empty_db()

        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>maintenance</html>")

        with _Providers(handle):
            response = await species_service.search_species(db, "clownfish")

        assert response.results == []

    @pytest.mark.asyncio
    async def test_a_204_is_an_answer_rather_than_a_failure(self, no_redis: None):
        """WoRMS says "no such name" with an empty 204, not with `[]`. A reader that only
        handles 200 turns every genuine miss into a provider failure - and then never caches
        it, so the next keystroke asks again."""
        with _Providers(lambda request: httpx.Response(204)):
            rows = await species_service._worms("AphiaRecordsByName", "nothing")

        assert rows == []

    @pytest.mark.asyncio
    async def test_a_query_under_two_characters_never_reaches_a_provider(self, no_redis: None):
        db = _empty_db()
        with _registers(by_name=[CLOWNFISH_RECORD]) as providers:
            response = await species_service.search_species(db, "a")

        assert response.results == []
        assert providers.requests == []


class TestCaching:
    @pytest.mark.asyncio
    async def test_a_second_identical_search_never_reaches_a_provider(self, fake_redis: FakeRedis):
        """Caching is not an optimization here - it is the condition on which asking a free
        public register at type-ahead rates is defensible at all."""
        db = _empty_db()
        with _registers(by_name=[CLOWNFISH_RECORD]) as providers:
            first = await species_service.search_species(db, "amphiprion ocellaris")
            outbound_after_first = len(providers.requests)
            second = await species_service.search_species(db, "amphiprion ocellaris")

            assert len(providers.requests) == outbound_after_first

        assert [r.aphia_id for r in first.results] == [r.aphia_id for r in second.results] == [278400]

    @pytest.mark.asyncio
    async def test_a_hit_is_held_far_longer_than_a_miss(self, fake_redis: FakeRedis):
        """A taxon's name does not change, so a hit keeps for a month. An empty answer is
        far more likely to be provider weirdness than a fact about the sea."""
        db = _empty_db()
        with _registers(by_name=[CLOWNFISH_RECORD]):
            await species_service.search_species(db, "amphiprion ocellaris")
        with _registers():
            await species_service.search_species(db, "nothing at all")

        ttls = sorted(fake_redis.expiries.values())
        assert ttls == [species_service._MISS_TTL_SECONDS, species_service._HIT_TTL_SECONDS]

    @pytest.mark.asyncio
    async def test_a_partial_answer_is_held_for_an_hour_not_a_month(self, fake_redis: FakeRedis):
        """The trap the search budget creates, and the reason `_SourceAnswer.ok` exists.

        A register that fails *fast* - connect refused, an error status, a body over the size
        cap - returns just like one that genuinely had nothing, so without the flag the
        merged answer counts as complete. Wikidata alone still produces results, so
        `_store_search` would pin a taxonomy-less "clownfish" under the 30-day TTL, and every
        later day when WoRMS was healthy would keep serving it.
        """
        db = _empty_db()

        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "wbgetentities" in url:
                return httpx.Response(200, json=WIKIDATA_ENTITIES)
            if "wikidata" in url:
                return httpx.Response(200, json=WIKIDATA_SEARCH)
            raise httpx.ConnectError("worms is down")

        with _Providers(handle):
            response = await species_service.search_species(db, "clownfish")

        # Wikidata answered, so this is a useful, non-empty, *partial* answer.
        assert len(response.results) == 1
        assert set(fake_redis.expiries.values()) == {species_service._MISS_TTL_SECONDS}

    @pytest.mark.asyncio
    async def test_an_oversized_wikidata_response_does_not_get_cached_as_the_answer(self, fake_redis: FakeRedis):
        """A body over `_MAX_RESPONSE_BYTES` is a failure, not an empty register."""
        db = _empty_db()

        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "wbgetentities" in url:
                return httpx.Response(200, content=b"x" * (species_service._MAX_RESPONSE_BYTES + 1))
            if "wikidata" in url:
                return httpx.Response(200, json=WIKIDATA_SEARCH)
            return httpx.Response(200, json=[])

        with _Providers(handle):
            await species_service.search_species(db, "clownfish")

        assert set(fake_redis.expiries.values()) == {species_service._MISS_TTL_SECONDS}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "body"),
        [
            # The Action API's default error format: a 200 with an error object, which is how
            # Wikidata reports read-only mode and a busy CirrusSearch backend. Nothing about
            # the transport says anything went wrong.
            ("an error body", {"error": {"code": "readonly", "info": "The wiki is read-only."}}),
            # A 200 that is neither an error nor a search result - a proxy or a CDN page
            # rendered as JSON. Not an empty register either.
            ("an unrecognizable shape", {"unexpected": True}),
        ],
    )
    async def test_a_wikidata_200_that_is_not_a_search_result_is_a_failure(
        self, fake_redis: FakeRedis, label: str, body: dict
    ):
        """The gap the first version of `ok` left open, and the reason `_wikidata_qids` has
        three outcomes rather than two.

        Checking only the status code sees a successful request, finds no `query.search`, and
        reports "Wikidata has nothing for you" - indistinguishable downstream from a genuine
        miss, and enough to pin a WoRMS-only answer for thirty days while Wikidata was simply
        refusing.
        """
        db = _empty_db()

        def handle(request: httpx.Request) -> httpx.Response:
            if "wikidata" in str(request.url):
                return httpx.Response(200, json=body)
            return httpx.Response(200, json=[CLOWNFISH_RECORD])

        with _Providers(handle):
            response = await species_service.search_species(db, "clownfish")

        # WoRMS answered, so there is a real result - which is exactly what makes the wrong
        # TTL reachable.
        assert len(response.results) == 1
        assert set(fake_redis.expiries.values()) == {species_service._MISS_TTL_SECONDS}

    @pytest.mark.asyncio
    async def test_a_genuinely_empty_wikidata_search_still_earns_the_month(self, fake_redis: FakeRedis):
        """The distinction the test above rests on: "matched nothing" is an answer, and must
        not be dragged down to the short TTL along with the failures."""
        db = _empty_db()

        def handle(request: httpx.Request) -> httpx.Response:
            if "wikidata" in str(request.url):
                return httpx.Response(200, json={"query": {"search": []}})
            return httpx.Response(200, json=[CLOWNFISH_RECORD])

        with _Providers(handle):
            await species_service.search_species(db, "clownfish")

        assert set(fake_redis.expiries.values()) == {species_service._HIT_TTL_SECONDS}

    @pytest.mark.asyncio
    async def test_a_healthy_fan_out_still_earns_the_month(self, fake_redis: FakeRedis):
        """The other half: the short TTL must be the exception, or nothing is ever cached
        usefully and the registers get asked on every keystroke."""
        db = _empty_db()
        with _registers(
            by_name=[CLOWNFISH_RECORD], wikidata_search=WIKIDATA_SEARCH, wikidata_entities=WIKIDATA_ENTITIES
        ):
            await species_service.search_species(db, "amphiprion ocellaris")

        assert set(fake_redis.expiries.values()) == {species_service._HIT_TTL_SECONDS}

    @pytest.mark.asyncio
    async def test_the_cached_entry_holds_no_local_uuid(self, fake_redis: FakeRedis):
        """Whether a species is in the catalog changes the moment somebody resolves it.
        Freezing that into a month-long entry would have the picker keep offering to resolve
        a species that already exists."""
        db = _empty_db()
        with _registers(by_name=[CLOWNFISH_RECORD]):
            await species_service.search_species(db, "amphiprion ocellaris")

        key, raw = next(iter(fake_redis.store.items()))
        assert key.startswith("species:")
        cached = SpeciesSearchResponse(**json.loads(raw.decode()))
        assert all(result.uuid is None for result in cached.results)

    @pytest.mark.asyncio
    async def test_a_redis_outage_is_a_miss_not_an_error(self):
        db = _empty_db()
        with patch.object(species_service.cache, "client", FakeRedis(failing=True)):
            with _registers(by_name=[CLOWNFISH_RECORD]):
                response = await species_service.search_species(db, "amphiprion ocellaris")

        assert [r.aphia_id for r in response.results] == [278400]

    @pytest.mark.asyncio
    async def test_the_keys_are_not_user_scoped(self, fake_redis: FakeRedis):
        """The second deliberate exception to "cache keys stay user-scoped", after
        `geocode:`. One lookup serving every diver is what makes this polite; keying per user
        would multiply outbound calls by the number of accounts. The prefix also keeps these
        clear of the `user_{id}_*` namespace `cache_invalidation` sweeps by pattern."""
        db = _empty_db()
        with _registers(by_name=[CLOWNFISH_RECORD]):
            await species_service.search_species(db, "amphiprion ocellaris")

        assert all(key.startswith("species:") and "user_" not in key for key in fake_redis.store)


class TestOutboundRequests:
    @pytest.mark.asyncio
    async def test_every_call_identifies_this_application(self, no_redis: None):
        """Wikimedia requires a descriptive `User-Agent` and blocks generic ones; WoRMS asks
        to be told who is calling. Not politeness - it is the condition of access."""
        db = _empty_db()
        with _registers(by_name=[CLOWNFISH_RECORD], wikidata_search=WIKIDATA_SEARCH) as providers:
            await species_service.search_species(db, "clownfish")

        assert providers.requests
        for request in providers.requests:
            assert request.headers["User-Agent"] == settings.SPECIES_USER_AGENT

    @pytest.mark.asyncio
    async def test_worms_is_asked_for_freshwater_taxa_too(self, no_redis: None):
        """`marine_only=false`, because this app logs freshwater dives and the default would
        hide every taxon in them."""
        db = _empty_db()
        with _registers(by_name=[CLOWNFISH_RECORD]) as providers:
            await species_service.search_species(db, "clownfish")

        by_name = [url for url in providers.urls() if "AphiaRecordsByName" in url]
        assert by_name and all("marine_only=false" in url for url in by_name)

    @pytest.mark.asyncio
    async def test_wikidata_is_filtered_to_entities_carrying_an_aphia_id(self, no_redis: None):
        """The filter is what keeps the result set to taxa WoRMS also knows, which is what
        makes the merge possible at all."""
        db = _empty_db()
        with _registers(wikidata_search=WIKIDATA_SEARCH, wikidata_entities=WIKIDATA_ENTITIES) as providers:
            await species_service.search_species(db, "clownfish")

        assert any("haswbstatement" in url and "P850" in url for url in providers.urls())

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("query", "must_not_contain"),
        [
            # Dot segments: httpx resolves these *before* sending, so an unencoded term would
            # send this server to `marinespecies.org/etc/passwd` instead of the search route -
            # an authenticated user steering our outbound request, with the answer cached for
            # a month under their string.
            ("../../../etc/passwd", "/etc/passwd"),
            # The quieter half of the same bug: unencoded, everything from the `#` is a
            # client-side fragment and never leaves, so the diver silently searches for
            # something other than what they typed.
            ("fish#comment", "#comment"),
            ("fish?x=1", "?x=1&"),
        ],
    )
    async def test_a_typed_query_cannot_reshape_the_url(
        self, no_redis: None, query: str, must_not_contain: str
    ) -> None:
        """WoRMS takes the search term in the URL *path*, unlike the geocoder next door which
        passes user text as a query parameter - so the term has to be percent-encoded, and
        `_worms` does it structurally rather than leaving it to each call site."""
        with _registers() as providers:
            await species_service._worms_by_name(query)

        assert providers.urls(), "the request never left"
        url = providers.urls()[0]
        assert must_not_contain not in url
        assert url.startswith(f"{settings.WORMS_API_URL.rstrip('/')}/AphiaRecordsByName/")

    @pytest.mark.asyncio
    async def test_an_ordinary_name_still_reaches_the_register_intact(self, no_redis: None) -> None:
        """The encoding must not break the normal case: a space is a legal, common thing in a
        binomial and has to arrive as one."""
        with _registers() as providers:
            await species_service._worms_by_name("amphiprion ocellaris")

        assert "AphiaRecordsByName/amphiprion%20ocellaris" in providers.urls()[0]

    @pytest.mark.asyncio
    async def test_entities_are_fetched_in_small_batches(self, no_redis: None):
        """`props=claims` returns every statement on an entity, and a taxon carries dozens of
        external identifiers - about 50 KB each. Ten in one response routinely exceeds
        `_MAX_RESPONSE_BYTES` (measured live: "shark" 667 KB, "turtle" 642 KB), which makes
        `_request` return `None` and silently costs the whole Wikidata contribution for
        exactly the words divers type most.
        """
        qids = [f"Q{n}" for n in range(species_service._WIKIDATA_SEARCH_LIMIT)]
        db = _empty_db()
        search = {"query": {"search": [{"title": qid} for qid in qids]}}

        with _registers(wikidata_search=search, wikidata_entities={"entities": {}}) as providers:
            await species_service.search_species(db, "shark")

        # Parsed rather than counted off the raw URL: `props=claims|labels|aliases` is
        # pipe-separated too, so a substring count would measure the wrong parameter.
        batches = [parse_qs(urlparse(url).query)["ids"][0] for url in providers.urls() if "wbgetentities" in url]

        assert len(batches) > 1, "all ten ids went out in one request"
        assert sum(len(ids.split("|")) for ids in batches) == len(qids), "an id was dropped or duplicated"
        for ids in batches:
            assert len(ids.split("|")) <= species_service._WIKIDATA_ENTITY_BATCH

    @pytest.mark.asyncio
    async def test_a_saturated_provider_drops_out_rather_than_rejecting_anyone(
        self, no_redis: None, unthrottled: AsyncMock
    ):
        """The provider caps are global, so raising would mean one diver's search rejecting
        another's. That register simply contributes nothing and the other still answers."""
        from src.app.core.exceptions.http_exceptions import RateLimitException

        unthrottled.side_effect = RateLimitException("too many")
        db = _empty_db()
        with _registers(by_name=[CLOWNFISH_RECORD]) as providers:
            response = await species_service.search_species(db, "clownfish")

        assert providers.requests == []
        assert response.results == []


# -------------- resolve --------------


class TestResolve:
    @pytest.mark.asyncio
    async def test_it_persists_the_taxon_and_every_name_it_is_findable_by(self, no_redis: None):
        db = _empty_db()
        with _registers(
            record=CLOWNFISH_RECORD,
            synonyms=[{"scientificname": "Amphiprion bicolor"}],
            vernaculars=[{"vernacular": "カクレクマノミ", "language_code": "jpn"}],
            wikidata_search=WIKIDATA_SEARCH,
            wikidata_entities=WIKIDATA_ENTITIES,
        ):
            species = await species_service.resolve_species(db, 278400)

        assert species.aphia_id == 278400
        assert species.scientific_name == "Amphiprion ocellaris"
        assert species.common_name == "ocellaris clownfish"
        assert species.wikidata_qid == "Q1126155"
        # WoRMS's flat classification, under its own key spellings.
        assert (species.class_name, species.order_name, species.family) == ("Teleostei", "Perciformes", "Pomacentridae")
        assert (species.is_marine, species.is_freshwater) == (True, False)

        added = [call.args[0] for call in db.add.call_args_list]
        names = {(row.name, row.kind, row.source) for row in added if isinstance(row, species_service.SpeciesName)}
        assert ("Amphiprion ocellaris", "scientific", "worms") in names
        assert ("Amphiprion bicolor", "synonym", "worms") in names
        # Every language goes into the index even though the UI is English-only: this is
        # what makes カクレクマノミ find the clownfish.
        assert ("カクレクマノミ", "common", "worms") in names
        assert ("ocellaris clownfish", "common", "wikidata") in names

    @pytest.mark.asyncio
    async def test_an_existing_species_is_returned_without_asking_anyone(self, no_redis: None):
        """Idempotent, and a 200 either way: the caller is naming a taxon that exists in the
        world, and whether this instance had seen it is not their concern."""
        db = _empty_db()
        existing = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing)))

        with _registers(record=CLOWNFISH_RECORD) as providers:
            species = await species_service.resolve_species(db, 278400)

        assert species is existing
        assert providers.requests == []

    @pytest.mark.asyncio
    async def test_a_synonym_resolves_to_the_accepted_taxon(self, no_redis: None):
        """A diver picked *Manta birostris*; the catalog stores *Mobula birostris*. Only
        accepted taxa go in, which is what keeps `aphia_id` a real identity."""
        db = _empty_db()
        valid_id = MANTA_SYNONYM_RECORD["valid_AphiaID"]
        accepted = {
            "AphiaID": valid_id,
            "scientificname": "Mobula birostris",
            "status": "accepted",
            "rank": "Species",
            "valid_AphiaID": valid_id,
        }

        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "wikidata" in url:
                return httpx.Response(200, json={"query": {"search": []}})
            if f"AphiaRecordByAphiaID/{valid_id}" in url:
                return httpx.Response(200, json=accepted)
            if "AphiaRecordByAphiaID" in url:
                return httpx.Response(200, json=MANTA_SYNONYM_RECORD)
            return httpx.Response(200, json=[])

        with _Providers(handle):
            species = await species_service.resolve_species(db, 105857)

        assert (species.aphia_id, species.scientific_name) == (valid_id, "Mobula birostris")

    @pytest.mark.asyncio
    async def test_worms_unreachable_is_a_503_rather_than_a_guess(self, no_redis: None):
        """The one place in this module that fails loudly. A catalog row is shared with
        every account and never rewritten, so inventing one is worse than a retry."""
        from fastapi import HTTPException

        db = _empty_db()
        with _unreachable("marinespecies.org"), pytest.raises(HTTPException) as raised:
            await species_service.resolve_species(db, 278400)

        assert raised.value.status_code == 503
        assert raised.value.detail == "Species lookup is temporarily unavailable."
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_row_survives_wikidata_being_down(self, no_redis: None):
        """The asymmetry that matters, and the counterpart to the 503 above: Wikidata is
        enrichment, so losing it costs the common name and the qid and nothing else. A row
        without either is a perfectly good row that falls back to the scientific name."""
        db = _empty_db()

        def handle(request: httpx.Request) -> httpx.Response:
            if "wikidata" in str(request.url):
                raise httpx.ConnectError("unreachable")
            if "AphiaRecordByAphiaID" in str(request.url):
                return httpx.Response(200, json=CLOWNFISH_RECORD)
            return httpx.Response(200, json=[])

        with _Providers(handle):
            species = await species_service.resolve_species(db, 278400)

        assert species.scientific_name == "Amphiprion ocellaris"
        assert species.wikidata_qid is None
        assert species.common_name is None

    @pytest.mark.asyncio
    async def test_two_divers_resolving_at_once_both_get_the_winner_s_row(self, no_redis: None):
        """The unique `aphia_id` turns a race into a collision rather than a duplicate taxon,
        and the loser adopts the winner's row - both callers asked for the same thing."""
        db = _empty_db()
        winner = MagicMock()
        calls = {"n": 0}

        def execute(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            # First lookup: nothing. After the IntegrityError: the winner's row.
            value = winner if calls["n"] > 1 else None
            return MagicMock(scalar_one_or_none=MagicMock(return_value=value), all=MagicMock(return_value=[]))

        db.execute = AsyncMock(side_effect=execute)
        db.commit = AsyncMock(side_effect=IntegrityError("insert", {}, Exception("duplicate key")))

        with _registers(record=CLOWNFISH_RECORD):
            species = await species_service.resolve_species(db, 278400)

        assert species is winner
        db.rollback.assert_awaited()


# -------------- routes --------------


class TestSearchRoute:
    def test_a_query_under_two_characters_is_a_422(self, client: TestClient):
        assert client.get("/api/v1/species/search", params={"q": "a"}).status_code == 422

    def test_the_literal_paths_are_declared_before_the_uuid_one(self):
        """FastAPI matches routes in declaration order, so `/species/search` and
        `/species/resolve` have to be declared before `/species/{uuid}` - otherwise "search"
        is parsed as a uuid and every request to it 422s.

        Asserted on the source, the way `test_picker_search.py` pins pagination clamping:
        both routes would still be *present* if somebody reordered them, and the reordering
        is the whole failure. `test_it_returns_the_declared_shape` above is the behavioural
        half - it gets a 200 rather than a uuid-parse 422."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "src" / "app" / "api" / "v1" / "species.py").read_text()

        assert source.index('"/species/search"') < source.index('"/species/{uuid}"')
        assert source.index('"/species/resolve"') < source.index('"/species/{uuid}"')

    def test_it_returns_the_declared_shape(self, client: TestClient, no_redis: None):
        with patch(
            "src.app.api.v1.species.search_species",
            new=AsyncMock(return_value=SpeciesSearchResponse(results=[], has_more=False)),
        ):
            response = client.get("/api/v1/species/search", params={"q": "clownfish"})

        assert response.status_code == 200
        assert SpeciesSearchResponse(**response.json()).results == []

    def test_the_per_user_limit_is_a_429(self, client: TestClient):
        from src.app.core.exceptions.http_exceptions import RateLimitException

        with patch(
            "src.app.api.v1.species.enforce_rate_limit",
            new=AsyncMock(side_effect=RateLimitException("Too many requests. Please try again later.")),
        ):
            response = client.get("/api/v1/species/search", params={"q": "clownfish"})

        assert response.status_code == 429

    def test_it_requires_authentication(self, anonymous_client: TestClient):
        assert anonymous_client.get("/api/v1/species/search", params={"q": "clownfish"}).status_code == 401


class TestResolveRoute:
    def test_resolving_is_a_200_not_a_201(self, client: TestClient):
        """ "Resolve", not "create": the taxon already existed in the world, and calling it
        twice has to be indistinguishable from calling it once."""
        species = MagicMock()
        species.uuid = uuid7()
        with patch("src.app.api.v1.species.resolve_species", new=AsyncMock(return_value=species)):
            with patch("src.app.schemas.species.SpeciesRead.model_validate") as validate:
                validate.return_value = _species_read(species.uuid)
                response = client.post("/api/v1/species/resolve", json={"aphia_id": 278400})

        assert response.status_code == 200

    @pytest.mark.parametrize("body", [{"aphia_id": 0}, {"aphia_id": -1}, {"aphia_id": "clownfish"}, {}])
    def test_it_rejects_an_impossible_aphia_id(self, client: TestClient, body: dict):
        assert client.post("/api/v1/species/resolve", json=body).status_code == 422

    def test_it_refuses_extra_fields(self, client: TestClient):
        """`extra="forbid"` so a caller who sends a scientific name gets told, rather than
        having it silently ignored - the name is not what this resolves on."""
        body = {"aphia_id": 278400, "scientific_name": "Amphiprion ocellaris"}
        assert client.post("/api/v1/species/resolve", json=body).status_code == 422

    def test_it_requires_authentication(self, anonymous_client: TestClient):
        assert anonymous_client.post("/api/v1/species/resolve", json={"aphia_id": 1}).status_code == 401


def _species_read(uuid: uuid_pkg.UUID) -> Any:
    from datetime import UTC, datetime

    from src.app.schemas.species import SpeciesRead

    return SpeciesRead(
        uuid=uuid,
        aphia_id=278400,
        scientific_name="Amphiprion ocellaris",
        rank="Species",
        status="accepted",
        created_at=datetime.now(UTC),
    )


# -------------- the dive join --------------


def _replace_db_mock() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


class TestReplaceJoinRows:
    """The `tests/test_gear.py::TestReplaceJoinRows` quartet, for the third join table."""

    @pytest.mark.asyncio
    async def test_species_are_written_in_the_order_given(self) -> None:
        db = _replace_db_mock()

        await replace_species_for_dive(db, dive_id=5, species_ids=[9, 4, 7])

        added = [call.args[0] for call in db.add.call_args_list]
        assert all(isinstance(row, DiveSpecies) for row in added)
        assert [(row.species_id, row.position) for row in added] == [(9, 0), (4, 1), (7, 2)]
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_ids_are_collapsed_keeping_first_position(self) -> None:
        """A diver picking the same species twice meant "I saw it", not "I saw two" - and v1
        stores no count for the difference to live in."""
        db = _replace_db_mock()

        await replace_species_for_dive(db, dive_id=5, species_ids=[9, 4, 9])

        added = [call.args[0] for call in db.add.call_args_list]
        assert [(row.species_id, row.position) for row in added] == [(9, 0), (4, 1)]

    @pytest.mark.asyncio
    async def test_an_empty_list_clears_the_sightings(self) -> None:
        db = _replace_db_mock()

        await replace_species_for_dive(db, dive_id=5, species_ids=[])

        db.add.assert_not_called()
        # The DELETE still runs, so passing [] genuinely empties the list.
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_commit_when_commit_is_false(self) -> None:
        db = _replace_db_mock()

        await replace_species_for_dive(db, dive_id=5, species_ids=[1], commit=False)

        db.commit.assert_not_awaited()


# -------------- against a real database --------------


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestConstraints:
    """The uniques, asserted against Postgres rather than against the declaration.

    All three tables are brand new, so `create_all` emitted their constraints at creation and
    no manual `ALTER TABLE` was needed anywhere in this feature - which is exactly the
    condition under which asserting them is worth the database round trip.
    """

    def test_two_species_cannot_share_an_aphia_id(self, db: Session):
        """The constraint the whole identity model rests on, and the one that turns two
        divers resolving at the same instant into a collision the service recovers from
        rather than a duplicate taxon nobody notices."""
        aphia_id = int(uuid7().hex[-7:], 16)
        create_species(db, aphia_id=aphia_id)

        db.add(Species(aphia_id=aphia_id, scientific_name="Impostor", rank="Species", status="accepted"))
        with pytest.raises(IntegrityError, match="ix_species_aphia_id"):
            db.commit()
        db.rollback()

    def test_a_dive_cannot_list_the_same_species_twice(self, db: Session):
        """`replace_species_for_dive` dedups before it inserts, so this is the backstop -
        and the reason the dedup has to exist rather than being tidiness."""
        user = create_user(db)
        dive = create_dive(db, user)
        species = create_species(db)
        db.add(DiveSpecies(dive_id=dive.id, species_id=species.id, position=0))
        db.commit()

        db.add(DiveSpecies(dive_id=dive.id, species_id=species.id, position=1))
        with pytest.raises(IntegrityError, match="ux_dive_species_dive_id_species_id"):
            db.commit()
        db.rollback()

    def test_a_species_cannot_carry_the_same_name_twice_for_one_kind(self, db: Session):
        species = create_species(db)
        # Not "clownfish", and not merely a *unique* "clownfish <hex>" either - the search
        # is a substring match, so anything containing a real name still surfaces in the dev
        # instance's picker, and these rows are global and never cleaned up. See
        # `create_species` for why that matters here and nowhere else in the suite.
        name = f"zzfixture-name-{uuid7().hex[-8:]}"
        db.add(SpeciesName(species_id=species.id, name=name, kind="common", source="wikidata"))
        db.commit()

        db.add(SpeciesName(species_id=species.id, name=name, kind="common", source="worms"))
        with pytest.raises(IntegrityError, match="ux_species_name_species_id_name_kind"):
            db.commit()
        db.rollback()

    def test_the_same_name_is_legal_under_a_different_kind(self, db: Session):
        """The constraint is on `(species_id, name, kind)`, not `(species_id, name)`: a
        taxon's accepted binomial can legitimately also be recorded as somebody's synonym."""
        species = create_species(db)
        # The real case this stands for is a binomial that is one taxon's accepted name and
        # another's synonym; the string itself is synthetic for the reason above.
        name = f"zzfixture-name-{uuid7().hex[-8:]}"
        db.add(SpeciesName(species_id=species.id, name=name, kind="scientific", source="worms"))
        db.add(SpeciesName(species_id=species.id, name=name, kind="synonym", source="worms"))
        db.commit()

        assert db.query(SpeciesName).filter(SpeciesName.species_id == species.id).count() == 2


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestSpeciesSeenIsDerived:
    """`user_dive_stats.species_seen`, against real dives.

    The column has existed since the first migration and was hardcoded to 0 for as long as
    nothing populated it - which is why the dashboard tile reading it was deleted. These are
    the assertions that make the number mean something.
    """

    @pytest.mark.asyncio
    async def test_it_counts_distinct_species_not_sightings(self, db: Session, async_db: AsyncSession):
        """A diver who saw a turtle on ten dives has seen one species."""
        user = create_user(db)
        first, second = create_dive(db, user), create_dive(db, user)
        shared, only_once = create_species(db), create_species(db)
        db.add_all(
            [
                DiveSpecies(dive_id=first.id, species_id=shared.id, position=0),
                DiveSpecies(dive_id=first.id, species_id=only_once.id, position=1),
                DiveSpecies(dive_id=second.id, species_id=shared.id, position=0),
            ]
        )
        db.commit()

        stats = await recalculate_dive_stats(async_db, user_id=user.id)

        assert stats.species_seen == 2

    @pytest.mark.asyncio
    async def test_a_soft_deleted_dive_stops_contributing(self, db: Session, async_db: AsyncSession):
        """`Dive` soft-deletes, so its `dive_species` rows survive the delete - the join
        cascade never fires. Without the `is_deleted` filter the count would keep including
        a species the diver only ever saw on a dive they removed."""
        user = create_user(db)
        live, doomed = create_dive(db, user), create_dive(db, user)
        seen_on_both, seen_only_on_doomed = create_species(db), create_species(db)
        db.add_all(
            [
                DiveSpecies(dive_id=live.id, species_id=seen_on_both.id, position=0),
                DiveSpecies(dive_id=doomed.id, species_id=seen_on_both.id, position=0),
                DiveSpecies(dive_id=doomed.id, species_id=seen_only_on_doomed.id, position=1),
            ]
        )
        db.commit()

        doomed.is_deleted = True
        db.commit()
        stats = await recalculate_dive_stats(async_db, user_id=user.id)

        assert stats.species_seen == 1

    @pytest.mark.asyncio
    async def test_another_divers_sightings_are_not_counted(self, db: Session, async_db: AsyncSession):
        """The catalog is shared; the sightings are not. This is the assertion that keeps
        "global table" from quietly meaning "global count"."""
        mine, theirs = create_user(db), create_user(db)
        my_dive, their_dive = create_dive(db, mine), create_dive(db, theirs)
        species = create_species(db)
        db.add_all(
            [
                DiveSpecies(dive_id=my_dive.id, species_id=species.id, position=0),
                DiveSpecies(dive_id=their_dive.id, species_id=species.id, position=0),
            ]
        )
        db.commit()

        assert (await recalculate_dive_stats(async_db, user_id=mine.id)).species_seen == 1

    @pytest.mark.asyncio
    async def test_an_existing_stats_row_is_brought_up_to_date(self, db: Session, async_db: AsyncSession):
        """The update branch against a real row - the one nearly every write takes, and the
        one a narrow reading of "derive it" would leave at 0 forever."""
        user = create_user(db)
        stats = await recalculate_dive_stats(async_db, user_id=user.id)
        assert stats.species_seen == 0

        dive = create_dive(db, user)
        db.add(DiveSpecies(dive_id=dive.id, species_id=create_species(db).id, position=0))
        db.commit()

        assert (await recalculate_dive_stats(async_db, user_id=user.id)).species_seen == 1


class TestWriteDiveEmbedsSightings:
    """`POST /dive`'s species half, with the route's collaborators stubbed - the
    `test_dive_update.py` style, since what is under test is the route's own logic rather
    than any query.

    The PATCH side lives in `test_dive_update.py::TestSpeciesReplacement`.
    """

    @staticmethod
    def _stub(monkeypatch: pytest.MonkeyPatch, *, resolves: bool = True) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        created = MagicMock()
        created.id = 11

        async def resolve(*, db: Any, species_uuids: list[uuid_pkg.UUID]) -> dict | None:
            return {value: index + 100 for index, value in enumerate(species_uuids)} if resolves else None

        async def replace(*, db: Any, dive_id: int, species_ids: list[int]) -> None:
            seen["species_ids"] = species_ids

        monkeypatch.setattr(dives_module.crud_dives, "create", AsyncMock(return_value=created))
        monkeypatch.setattr(dives_module.crud_dives, "get", AsyncMock(return_value={}))
        monkeypatch.setattr(dives_module, "resolve_trip_id_for_user", AsyncMock(return_value=77))
        monkeypatch.setattr(dives_module, "resolve_dive_site_ids_for_user", AsyncMock(return_value={}))
        monkeypatch.setattr(dives_module, "resolve_gear_item_ids_for_user", AsyncMock(return_value={}))
        monkeypatch.setattr(dives_module, "resolve_species_ids", AsyncMock(side_effect=resolve))
        monkeypatch.setattr(dives_module, "replace_species_for_dive", AsyncMock(side_effect=replace))
        for name in ("replace_mixtures_for_dive", "replace_dive_sites_for_dive", "replace_gear_items_for_dive"):
            monkeypatch.setattr(dives_module, name, AsyncMock())
        for name in ("recalculate_dive_stats", "recalculate_gear_dive_counts"):
            monkeypatch.setattr(dives_module, name, AsyncMock())
        for name in ("invalidate_dive_caches", "invalidate_gear_caches"):
            monkeypatch.setattr(dives_module, name, AsyncMock())
        monkeypatch.setattr(dives_module, "get_mixtures_for_dive", AsyncMock(return_value=[]))
        monkeypatch.setattr(dives_module, "get_dive_sites_for_dive", AsyncMock(return_value=[]))
        monkeypatch.setattr(dives_module, "get_gear_items_for_dive", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            dives_module,
            "get_species_for_dive",
            AsyncMock(
                return_value=[
                    SpeciesInfo(uuid=uuid7(), scientific_name="Mobula birostris", rank="Species"),
                    SpeciesInfo(uuid=uuid7(), scientific_name="Muraenidae", rank="Family"),
                ]
            ),
        )
        monkeypatch.setattr(dives_module, "_to_public_dive_with_mixtures", lambda *a, **kw: kw)
        return seen

    async def _write(self, species_uuids: list[uuid_pkg.UUID]) -> Any:
        user_uuid = uuid7()
        body = DiveCreateRequest.model_validate(
            {
                "user_uuid": str(user_uuid),
                "dive_number": 1,
                "start_time": "2026-06-01T09:00:00+02:00",
                "duration": 1800,
                "notes": "",
                "species_uuids": [str(value) for value in species_uuids],
            }
        )
        return await dives_module.write_dive(
            request=MagicMock(),
            dive=body,
            current_user={"id": 1, "uuid": user_uuid},
            db=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_species_are_resolved_and_written_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._stub(monkeypatch)

        result = await self._write([uuid7(), uuid7()])

        assert seen["species_ids"] == [100, 101]
        # And they come back embedded, rather than needing a second request to see what was
        # just saved.
        assert [s.scientific_name for s in result["species"]] == ["Mobula birostris", "Muraenidae"]

    @pytest.mark.asyncio
    async def test_an_unknown_species_is_a_422_before_the_dive_is_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Named, like an unknown trip or gear item. There is no ownership to fail - the
        catalog is global - so this is purely "no such species"."""
        from src.app.core.exceptions.http_exceptions import UnprocessableEntityException

        seen = self._stub(monkeypatch, resolves=False)

        with pytest.raises(UnprocessableEntityException, match="Species not found."):
            await self._write([uuid7()])

        assert "species_ids" not in seen
        # The resolve runs *before* the insert, so a bad uuid costs no write at all.
        create = cast(AsyncMock, dives_module.crud_dives.create)
        create.assert_not_awaited()


class TestTheReadTransactionIsReleasedBeforeGoingOutbound:
    """Every db-touching step here runs *before* the slow part, and `AsyncSession` autobegins
    on the first `execute()` - so without an explicit release the connection that ran a
    sub-millisecond `SELECT` sits idle-in-transaction for the whole outbound call: up to six
    seconds on search and twenty-five on resolve, against a pool of five plus ten overflow.
    The event loop is free throughout, which is what makes it invisible until the pool runs
    dry and unrelated endpoints start timing out.

    Ordering tests rather than behavioural ones, matching
    `test_dive_files.py::TestProfileExtractionReleasesTheTransaction`: what regresses is
    somebody moving a query back above the release, and nothing else would catch it.
    """

    @staticmethod
    def _tracking_db(calls: list[str], *, existing: Any = None) -> MagicMock:
        result = MagicMock()
        result.all.return_value = []
        result.scalar_one_or_none.return_value = existing

        def record_query(*args: Any, **kwargs: Any) -> MagicMock:
            calls.append("query")
            return result

        db = MagicMock()
        db.execute = AsyncMock(side_effect=record_query)
        db.rollback = AsyncMock(side_effect=lambda: calls.append("release"))
        db.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
        db.scalar = AsyncMock(return_value=None)
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.refresh = AsyncMock()
        return db

    @staticmethod
    def _marking_providers(calls: list[str], **kwargs: Any) -> _Providers:
        inner = _registers(**kwargs)
        handler = inner._handler

        def record(request: httpx.Request) -> httpx.Response:
            calls.append("outbound")
            return handler(request)

        return _Providers(record)

    @staticmethod
    def _assert_no_query_is_held_open(calls: list[str]) -> None:
        """No `query` may sit between a release and the outbound call that follows it.

        Positional rather than `calls.index(...)`, which returns the *first* occurrence and so
        cannot see the regression this class exists for: a read added back between the release
        and the fan-out leaves `["query", "release", "query", "outbound"]`, where every
        index-based comparison still holds while the connection is pinned open again.
        """
        assert "outbound" in calls, "nothing went outbound; the test is not exercising the path"
        assert "release" in calls, "the local read's transaction is never released"

        for position, call in enumerate(calls):
            if call != "outbound":
                continue
            preceding = calls[:position]
            assert "release" in preceding, f"went outbound at {position} before any release: {calls}"
            # Everything after the last release, up to this outbound call, must be free of
            # database work - that window is exactly what would be held idle-in-transaction.
            window = preceding[len(preceding) - preceding[::-1].index("release") :]
            assert "query" not in window, f"a query is held open across the outbound call: {calls}"

    @pytest.mark.asyncio
    async def test_search_releases_before_the_fan_out(self, no_redis: None) -> None:
        calls: list[str] = []
        db = self._tracking_db(calls)

        with self._marking_providers(calls, by_name=[CLOWNFISH_RECORD]):
            await species_service.search_species(db, "clownfish")

        assert calls[0] == "query", "the local catalog is read first, or this proves nothing"
        self._assert_no_query_is_held_open(calls)

    @pytest.mark.asyncio
    async def test_resolve_releases_before_asking_worms(self, no_redis: None) -> None:
        calls: list[str] = []
        db = self._tracking_db(calls)

        with self._marking_providers(calls, record=CLOWNFISH_RECORD):
            await species_service.resolve_species(db, 278400)

        self._assert_no_query_is_held_open(calls)

    @pytest.mark.asyncio
    async def test_the_synonym_branch_releases_before_its_second_fetch(self, no_redis: None) -> None:
        """The branch DECISIONS.md calls the worst case - two record fetches, the second
        preceded by another local lookup - and the one `CLOWNFISH_RECORD` cannot reach, since
        its `valid_AphiaID` is its own `AphiaID`. Without this, the third release could be
        deleted with the whole suite still green.
        """
        calls: list[str] = []
        db = self._tracking_db(calls)
        valid_id = MANTA_SYNONYM_RECORD["valid_AphiaID"]
        accepted = {
            "AphiaID": valid_id,
            "scientificname": "Mobula birostris",
            "status": "accepted",
            "rank": "Species",
            "valid_AphiaID": valid_id,
        }

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append("outbound")
            url = str(request.url)
            if "wikidata" in url:
                return httpx.Response(200, json={"query": {"search": []}})
            if f"AphiaRecordByAphiaID/{valid_id}" in url:
                return httpx.Response(200, json=accepted)
            if "AphiaRecordByAphiaID" in url:
                return httpx.Response(200, json=MANTA_SYNONYM_RECORD)
            return httpx.Response(200, json=[])

        with _Providers(handle) as providers:
            species = await species_service.resolve_species(db, 105857)

        # That the *fold* happened, not merely that requests were made: `resolve_species`
        # always fires four (the record, two enrichment calls and a Wikidata search), so a
        # count of them stays green with the branch ripped out. Two record fetches, and the
        # accepted taxon coming back, are what only this branch can produce.
        record_fetches = [url for url in providers.urls() if "AphiaRecordByAphiaID" in url]
        assert len(record_fetches) == 2, record_fetches
        assert species.scientific_name == "Mobula birostris"
        # And two separate lookups were released, not just the first.
        assert calls.count("release") >= 2
        self._assert_no_query_is_held_open(calls)

    @pytest.mark.asyncio
    async def test_an_already_known_species_is_returned_without_a_release(self, no_redis: None) -> None:
        """The early-return path holds a live `Species`, and `rollback` expires ORM objects
        regardless of `expire_on_commit=False` - which applies to commit only. Releasing here
        would turn the caller's next attribute access into a silent reload."""
        calls: list[str] = []
        existing = MagicMock()
        db = self._tracking_db(calls, existing=existing)

        with self._marking_providers(calls, record=CLOWNFISH_RECORD):
            species = await species_service.resolve_species(db, 278400)

        assert species is existing
        assert "release" not in calls
        assert "outbound" not in calls


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestConcurrentResolvesDoNotExhaustThePool:
    """The failure `_release_read_transaction` exists to prevent, against a real pool.

    The ordering tests above pin *that* the release happens; this pins what it buys, which is
    the only part a reader can check against the symptom. Worth having as a real-pool test
    rather than a mock because the whole bug lives in SQLAlchemy's checkout lifecycle - a
    mocked session has no pool to exhaust and would pass either way.

    **Concurrency here is the designed load, not an unlucky burst.** The web picker keeps its
    menu open after a pick, so a diver adding a dive's worth of sightings fires several
    resolves within a second or two from one browser; the client deliberately does not
    serialise them, since that is the interaction the pending rows exist to support.
    """

    @staticmethod
    def _record(url: str) -> httpx.Response:
        aphia_id = int(str(url).rsplit("/", 1)[-1].split("?")[0])
        return httpx.Response(
            200,
            json={
                "AphiaID": aphia_id,
                "scientificname": f"zzfixture-concurrent-{aphia_id}",
                "status": "accepted",
                "rank": "Species",
                "valid_AphiaID": aphia_id,
            },
        )

    @pytest.mark.asyncio
    async def test_the_pool_is_free_while_every_resolve_is_outbound(self, no_redis: None) -> None:
        # The engine's own defaults, spelled out so the numbers below are readable.
        engine = create_async_engine(
            settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI, pool_size=5, max_overflow=10
        )
        sessions = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        concurrent, outbound_seconds = 15, 0.5  # 15 is exactly the pool ceiling: 5 + 10 overflow

        def build(**kwargs: Any) -> httpx.AsyncClient:
            async def handle(request: httpx.Request) -> httpx.Response:
                await anyio.sleep(outbound_seconds)
                url = str(request.url)
                if "wikidata" in url:
                    return httpx.Response(200, json={"query": {"search": []}})
                if "AphiaRecordByAphiaID" in url:
                    return TestConcurrentResolvesDoNotExhaustThePool._record(url)
                return httpx.Response(200, json=[])

            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handle), **kwargs)

        # Fresh AphiaIDs every run, from uuid7's random tail like `create_species`. A fixed
        # base is the trap here: these rows persist in the developer's database, so the second
        # run finds them all, returns before any outbound call, and the test silently stops
        # measuring anything while still passing.
        base = int(uuid7().hex[-6:], 16) * 100

        async def resolve(offset: int) -> None:
            async with sessions() as db:
                await species_service.resolve_species(db, base + offset)

        checkouts: list[int] = []
        # `AsyncEngine.pool` is typed as the base `Pool`, which does not declare the checkout
        # counters; the pool actually in use here is a queue pool and does.
        pool = cast(QueuePool, engine.pool)

        async def sample() -> None:
            for _ in range(int(outbound_seconds * 10)):
                await anyio.sleep(0.1)
                checkouts.append(pool.checkedout())

        try:
            with (
                patch("src.app.services.species_service.httpx.AsyncClient", side_effect=build),
                patch("src.app.services.species_service.enforce_rate_limit", new_callable=AsyncMock),
            ):
                async with anyio.create_task_group() as tasks:
                    for offset in range(concurrent):
                        tasks.start_soon(resolve, offset)
                    tasks.start_soon(sample)
        finally:
            await engine.dispose()

        # Sampled across the middle of the burst, when every resolve is waiting on the
        # register. Without the release this reads 15 for the whole window and an unrelated
        # endpoint asking for a connection waits out `pool_timeout` and fails.
        steady = checkouts[len(checkouts) // 3 : 2 * len(checkouts) // 3]
        assert steady, "the sampler never ran; the burst finished too fast to measure"
        assert max(steady) < concurrent, f"connections pinned across the outbound calls: {checkouts}"


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestLocalSearchAgainstPostgres:
    """`_local_search`'s statement, executed rather than merely built.

    Every other search test hands `search_species` a mocked session, which is right for the
    merge and degradation logic but means the SQL itself is constructed in Python and thrown
    away. That statement is the one non-trivial query in this feature - an outer join, a
    `CASE`, two aggregates over it, `GROUP BY` on the primary key leaning on Postgres's
    functional-dependency inference, and an `ORDER BY` naming a string label - and it backs the
    half of search that is supposed to keep working when both registers are down. A defect in
    it would surface as a 500 on exactly the path the degradation promise rests on, while
    `test_both_registers_down_falls_back_to_the_catalog` stayed green, because that test stubs
    the row shape this query would have produced.

    Names are `zzfixture-*` so the rows these leave behind cannot be reached by a real query -
    see `create_species`.
    """

    @staticmethod
    def _seed(db: Session, *names: tuple[str, str], scientific_name: str | None = None) -> Any:
        species = create_species(
            db, scientific_name=scientific_name or f"zzfixture-local-{uuid7().hex[-8:]}", common_name=None
        )
        for name, kind in names:
            db.add(SpeciesName(species_id=species.id, name=name, kind=kind, source="worms"))
        db.commit()
        return species

    @pytest.mark.asyncio
    async def test_a_species_with_several_matching_aliases_comes_back_once(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The `GROUP BY` doing its job. The outer join fans out one row per matching alias, and
        a diver wants one row per species."""
        token = f"zzq{uuid7().hex[-8:]}"
        species = self._seed(db, (f"{token} one", "common"), (f"{token} two", "common"), (f"{token} three", "synonym"))

        results, _ = await species_service._local_search(async_db, token)

        assert [r.uuid for r in results] == [species.uuid]

    @pytest.mark.asyncio
    async def test_exact_matches_outrank_prefix_which_outranks_substring(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The `CASE`/`min`/`ORDER BY` chain, which is the part that cannot be checked by
        building the statement alone."""
        token = f"zzq{uuid7().hex[-8:]}"
        exact = self._seed(db, (token, "common"))
        prefix = self._seed(db, (f"{token}tail", "common"))
        substring = self._seed(db, (f"head{token}tail", "common"))

        results, _ = await species_service._local_search(async_db, token)

        assert [r.uuid for r in results] == [exact.uuid, prefix.uuid, substring.uuid]

    @pytest.mark.asyncio
    async def test_a_row_is_findable_by_its_own_scientific_name_with_no_aliases(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The reason the join is an *outer* one: a catalog row whose `species_name` rows failed
        to write must still be findable."""
        token = f"zzq{uuid7().hex[-8:]}"
        species = self._seed(db, scientific_name=f"zzfixture-local-{token}")

        results, _ = await species_service._local_search(async_db, token)

        assert [r.uuid for r in results] == [species.uuid]

    @pytest.mark.asyncio
    async def test_the_matched_name_explains_a_hit_and_is_dropped_when_it_would_not(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        token = f"zzq{uuid7().hex[-8:]}"
        alias = f"{token} alias"
        species = self._seed(db, (alias, "synonym"))

        by_alias, _ = await species_service._local_search(async_db, token)
        assert by_alias[0].matched_name == alias

        # Matched by the row's own scientific name, which the result already shows - so the
        # hint would be noise and is nulled.
        by_name, _ = await species_service._local_search(async_db, species.scientific_name.casefold())
        assert by_name[0].matched_name is None

    @pytest.mark.asyncio
    async def test_a_wildcard_in_the_query_is_escaped_rather_than_matching_everything(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """`escape_like` against the real `ILIKE ... ESCAPE`, not against a compiled string.
        Unescaped, `%` would return the whole catalog."""
        token = f"zzq{uuid7().hex[-8:]}"
        literal = self._seed(db, (f"{token}%pct", "common"))
        # The discriminating row: this matches `{token}%pct` only if the `%` is left as a
        # wildcard. A decoy that simply fails to match either way proves nothing - which is
        # what the first version of this test did, and it passed with `escape_like` removed.
        self._seed(db, (f"{token}ANYTHINGpct", "common"))

        results, _ = await species_service._local_search(async_db, f"{token}%pct")

        assert [r.uuid for r in results] == [literal.uuid]


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestReadSpeciesRoute:
    """`GET /species/{uuid}`, which had no test at all.

    Called directly rather than through `TestClient`, so it runs against a real session - the
    query and the 404 are the whole of this route, and both need a database to mean anything.
    The 401 is covered by `TestSearchRoute`'s anonymous case, which needs no session because
    the dependency rejects before the body runs.
    """

    @pytest.mark.asyncio
    async def test_it_returns_the_catalog_row(self, db: Session, async_db: AsyncSession) -> None:
        species = create_species(db, aphia_id=int(uuid7().hex[-7:], 16), common_name="zzfixture common")

        result = await read_species(uuid=species.uuid, current_user=CURRENT_USER, db=async_db)

        assert result.uuid == species.uuid
        assert (result.aphia_id, result.scientific_name) == (species.aphia_id, species.scientific_name)
        assert result.common_name == "zzfixture common"

    @pytest.mark.asyncio
    async def test_an_unknown_uuid_is_a_404(self, async_db: AsyncSession) -> None:
        """And it means "not in this catalog" rather than the "not yours" the same status means
        on every other `/{uuid}` route here - there is no owner to fail against."""
        with pytest.raises(NotFoundException):
            await read_species(uuid=uuid7(), current_user=CURRENT_USER, db=async_db)
