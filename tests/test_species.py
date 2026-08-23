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
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeout
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
from src.app.schemas.species import SpeciesSearchResponse, SpeciesSearchResult
from src.app.services import species_service
from src.app.services.dive_stats import recalculate_dive_stats
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_species, create_user

_REAL_ASYNC_CLIENT = httpx.AsyncClient

CURRENT_USER = {"id": 7, "uuid": uuid7(), "username": "ada", "is_superuser": False}

# Wikidata's taxonomic-rank items, named because a bare QID in a payload says nothing to the
# next reader. Verified against the live entities: *Orcinus orca* (Q26843) and the swordfish
# carry the species item, the genus *Orca* (Q41156273) the genus one, the subgenus *Orca*
# (Q61884050 - the item with an AphiaID and no taxon name) the subgenus one, and *Mysticeti*
# carries the parvorder and the suborder, in that order, as two live statements.
_SPECIES_RANK_ITEM = "Q7432"
_GENUS_RANK_ITEM = "Q34740"
_SUBGENUS_RANK_ITEM = "Q3238261"
_PARVORDER_RANK_ITEM = "Q6311258"
_SUBORDER_RANK_ITEM = "Q5867959"
# A real taxonomic-rank item outside WoRMS's vocabulary, so deliberately absent from the map:
# "cultivar", which is what an unmapped rank looks like when one turns up.
_UNMAPPED_RANK_ITEM = "Q4886"


def _generator_search(*pages: str | tuple[str, ...], more: bool = False) -> dict[str, Any]:
    """A `generator=search` answer in the live shape - the search path's Wikidata payload.

    Each page is either a bare QID, a candidate whose terms the test does not care about and
    which therefore carries no `entityterms` at all, or a `(qid, label, *aliases)` tuple where
    the terms are the point. An empty string in the label position means *aliases only*, which
    is Q733595's live shape - the `nudibranch` front-runner has no English label whatsoever.

    Four details of the real answer are reproduced rather than tidied away, because a reader
    that gets any of them wrong has to fail here rather than in production:

    - `query.pages` is keyed by **pageid**, and that key order is *not* the relevance order.
      The keys below are laid out to iterate in the reverse of `index` deliberately, so a
      reader that takes the object in iteration order gets the page order backwards.
    - `entityterms` **omits** a key rather than sending an empty list, independently for
      `label` and `alias`.
    - Truncation is signalled only by the top-level `continue` key (`more=True`). This variant
      never reports `totalhits`, so there is nothing else to read it from.
    - **Empty is `{"batchcomplete": ""}` with no `query` key at all** - twenty bytes on the
      wire, and what `_generator_search()` with no pages returns. `list=search` answers a miss
      with `query.search` present and empty instead, which is the asymmetry the two readers
      exist for.
    """
    entries: list[tuple[str, dict[str, Any]]] = []
    for index, page in enumerate(pages, start=1):
        qid, *terms = (page,) if isinstance(page, str) else page
        pageid = 1000 + index
        entry: dict[str, Any] = {"pageid": pageid, "ns": 0, "title": qid, "index": index}
        entityterms: dict[str, list[str]] = {}
        if terms[:1] and terms[0]:
            entityterms["label"] = [terms[0]]
        if terms[1:]:
            entityterms["alias"] = list(terms[1:])
        if entityterms:
            entry["entityterms"] = entityterms
        entries.append((str(pageid), entry))

    payload: dict[str, Any] = {"batchcomplete": ""}
    if more:
        payload["continue"] = {"gsroffset": len(entries), "continue": "gsroffset||"}
    if entries:
        # Reversed, so the object iterates worst-ranked first and only `index` can restore it.
        payload["query"] = {"pages": dict(reversed(entries))}
    return payload

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

# The killer whale, which this catalog stored as "Orca gladiator" until the reject list
# landed. Kept as a payload set rather than a unit-test argument list because the defect only
# exists end to end: Wikidata offers the junior scientific synonym as its first English alias,
# and only WoRMS - through a second endpoint, on the resolve path - knows what it is.
ORCA_RECORD = {
    "AphiaID": 137102,
    "scientificname": "Orcinus orca",
    "authority": "(Linnaeus, 1758)",
    "status": "accepted",
    "rank": "Species",
    "valid_AphiaID": 137102,
    "valid_name": "Orcinus orca",
    "genus": "Orcinus",
}
ORCA_SYNONYMS = [{"scientificname": "Orca gladiator"}, {"scientificname": "Orca capensis"}]

# The same taxon as two different Wikidata answers, because the two paths ask two different
# questions and get two differently shaped replies. **Search** asks `generator=search` for the
# typed word and gets pages carrying entity terms; **resolve** asks `list=search` for one exact
# `haswbstatement:P850=<id>` and gets a `search` array of titles. Handing either shape to the
# other's reader fails silently rather than loudly - it simply looks like a register with
# nothing to say - which is why `_registers` routes them apart by URL and why there are two
# constants here rather than one shared between the paths.
ORCA_WIKIDATA_SEARCH = _generator_search(
    ("Q26843", "Orcinus orca", "Orca gladiator", "orca whale", "killer whale")
)
ORCA_WIKIDATA_LOOKUP = {"query": {"search": [{"title": "Q26843"}]}}
ORCA_WIKIDATA_ENTITIES = {
    "entities": {
        "Q26843": {
            "labels": {"en": {"language": "en", "value": "Orcinus orca"}},
            # Wikidata's own alias order, verified against the live entity: the synonym comes
            # first, so nothing but the reject list stands between it and the dive card.
            "aliases": {
                "en": [{"value": "Orca gladiator"}, {"value": "orca whale"}, {"value": "killer whale"}],
            },
            "claims": {
                "P850": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "137102"}}}],
                "P225": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "Orcinus orca"}}}],
                "P105": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": _SPECIES_RANK_ITEM}}}}],
            },
        }
    }
}

WIKIDATA_SEARCH = _generator_search(
    ("Q1126155", "Amphiprion ocellaris", "ocellaris clownfish", "Common clownfish")
)
WIKIDATA_LOOKUP = {"query": {"search": [{"title": "Q1126155"}]}}
WIKIDATA_ENTITIES = {
    "entities": {
        "Q1126155": {
            # The English *label* is the binomial itself, which is the norm for taxa and the
            # exact reason `_choose_common_name` prefers a label that differs from it.
            "labels": {"en": {"language": "en", "value": "Amphiprion ocellaris"}},
            "aliases": {"en": [{"value": "ocellaris clownfish"}, {"value": "Common clownfish"}]},
            "claims": {
                # External identifiers are strings in Wikidata, whatever they look like;
                # P105's value is an *item*, so it arrives as a dict carrying its QID. Every
                # statement carries its own `rank`, which is how Wikidata says which of
                # several values is current - trimmed out of these payloads until the reader
                # started honouring it.
                "P850": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "278400"}}}],
                "P225": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "Amphiprion ocellaris"}}}],
                "P105": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": _SPECIES_RANK_ITEM}}}}],
            },
        }
    }
}


def _entity(
    qid: str, *, aphia_id: str, taxon_name: str | None, rank_item: str | None = None, label: str | None = None
) -> dict[str, Any]:
    """One `wbgetentities` entity in the live shape, with only the pieces a test cares about.

    `taxon_name=None` is the shape the P225 gate exists for: an item tagged with an AphiaID
    that names no taxon at all.
    """
    claims: dict[str, Any] = {"P850": [{"rank": "normal", "mainsnak": {"datavalue": {"value": aphia_id}}}]}
    if taxon_name is not None:
        claims["P225"] = [{"rank": "normal", "mainsnak": {"datavalue": {"value": taxon_name}}}]
    if rank_item is not None:
        claims["P105"] = [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": rank_item}}}}]
    entity: dict[str, Any] = {"claims": claims}
    if label is not None:
        entity["labels"] = {"en": {"language": "en", "value": label}}
    return entity


def _worms_record(aphia_id: int, scientific_name: str, *, rank: str = "Species") -> dict[str, Any]:
    """One accepted `AphiaRecord`, trimmed to the fields the search path reads."""
    return {
        "AphiaID": aphia_id,
        "scientificname": scientific_name,
        "status": "accepted",
        "rank": rank,
        "valid_AphiaID": aphia_id,
        "valid_name": scientific_name,
    }


def _ajax_row(aphia_id: int, display_name: str, vername: str | None, language: str | None = "eng") -> dict[str, Any]:
    """One `AjaxAphiaRecordsByNamePart` row in the live six-key shape.

    `id` is raw and unfolded and there is no `valid_AphiaID` or `status` anywhere - which is
    exactly why these rows annotate and never become rows of their own.
    """
    return {
        "id": aphia_id,
        "authority": "Linnaeus, 1758",
        "displayname": display_name,
        "vername": vername,
        "language": language,
        "text": display_name,
    }


def _result(
    aphia_id: int,
    scientific_name: str,
    *,
    common_name: str | None = None,
    rank: str = "Species",
    matched_name: str | None = None,
) -> SpeciesSearchResult:
    """A finished search row, for the tests that exercise the ranking key on its own."""
    return SpeciesSearchResult(
        aphia_id=aphia_id,
        scientific_name=scientific_name,
        common_name=common_name,
        rank=rank,
        status="accepted",
        matched_name=matched_name,
        source="worms",
        attribution="World Register of Marine Species (marinespecies.org)",
    )


@pytest.fixture(scope="module")
def species_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`, like `test_geocoding.py` - the route
    tests here stub the database out entirely."""
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


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
    ajax: Any = (),
    by_name: Any = (),
    by_vernacular: Any = (),
    record: Any = None,
    synonyms: Any = (),
    vernaculars: Any = (),
    wikidata_search: Any = None,
    wikidata_lookup: Any = None,
    wikidata_entities: Any = None,
    worms_status: int = 200,
    wikidata_status: int = 200,
) -> _Providers:
    """Both registers answering from canned payloads, routed by path.

    Defaults are "answered, and had nothing", which is the shape a lot of these tests want
    for the source they are *not* exercising.

    **The two Wikidata searches are routed apart, and never share a default.** Search asks
    `generator=search` and resolve asks `list=search`, and their answers are shaped
    differently enough that each reader treats the other's payload as a failed or empty
    register - silently, since neither shape raises. One `wikidata_search=` serving both would
    therefore let a test pass while exercising nothing, which is the same trap the `ajax`
    ordering below exists for. So `wikidata_search=` is the generator payload
    (`_generator_search`), `wikidata_lookup=` is resolve's `list=search` one, and the empty
    default for each is that variant's own measured empty.

    `synonyms` is the *whole* list and gets served the way WoRMS serves it, a page at a time.
    `_worms_synonyms` walks offsets until a short page comes back, so a canned list handed
    back whole on every offset would never terminate - and one longer than a page has to
    arrive in pieces or the walk it exists to exercise never happens.

    **`ajax` is routed first, and the ordering is the whole point.**
    `AjaxAphiaRecordsByNamePart` *contains* the substring `AphiaRecordsByName`, so under the
    obvious ordering the annotation call silently collects the by-name payload - which is
    `AphiaRecord`-shaped, an entirely different schema from the six-key ajax row. A broken
    vername normalizer would then be fed happily and never noticed, since neither shape
    raises. `[]` is the right default: it is what a 204, WoRMS's real "no match", becomes.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "wikidata" in url:
            if "wbgetentities" in url:
                return httpx.Response(wikidata_status, json=wikidata_entities or {"entities": {}})
            if "generator=search" in url:
                return httpx.Response(wikidata_status, json=wikidata_search or _generator_search())
            return httpx.Response(wikidata_status, json=wikidata_lookup or {"query": {"search": []}})
        if "AjaxAphiaRecordsByNamePart" in url:
            return httpx.Response(worms_status, json=list(ajax))
        if "AphiaRecordsByName" in url:
            return httpx.Response(worms_status, json=list(by_name))
        if "AphiaRecordsByVernacular" in url:
            return httpx.Response(worms_status, json=list(by_vernacular))
        if "AphiaRecordByAphiaID" in url:
            return httpx.Response(worms_status, json=record)
        if "AphiaSynonymsByAphiaID" in url:
            page = species_service._WORMS_PAGE_SIZE
            start = int(parse_qs(urlparse(url).query).get("offset", ["1"])[0]) - 1
            return httpx.Response(worms_status, json=list(synonyms)[start : start + page])
        if "AphiaVernacularsByAphiaID" in url:
            return httpx.Response(worms_status, json=list(vernaculars))
        raise AssertionError(f"unexpected request to {url}")

    return _Providers(handle)


def _unreachable(host: str) -> _Providers:
    """One register that raises on every request; the other answers with nothing."""

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if host in url:
            raise httpx.ConnectError("unreachable")
        if "wbgetentities" in url:
            return httpx.Response(200, json={"entities": {}})
        if "generator=search" in url:
            return httpx.Response(200, json=_generator_search())
        if "wikidata" in url:
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
        # Capitalised here as well as at resolve, because one `_choose_common_name` serves
        # both paths. Only the capitalisation carries across, though: the reject list is an
        # input search cannot supply, and
        # `test_search_shows_the_unvetted_name_until_a_resolve_fixes_it` pins what that costs.
        assert result.common_name == "Ocellaris clownfish"
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
            (278400, "Ocellaris clownfish", "wikidata")
        ]
        # No WoRMS record behind this row, and it still says what the taxon is: P105 is where
        # that comes from, and it is why the row is not stranded on the sentinel.
        assert response.results[0].rank == "Species"

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
        with _registers(wikidata_search=_generator_search("Q999"), wikidata_entities=entities):
            response = await species_service.search_species(db, "something")

        assert response.results == []

    @pytest.mark.asyncio
    async def test_the_page_does_not_depend_on_which_worms_leg_answered_first(self, no_redis: None):
        """Both WoRMS sources reach the merge as `worms` rows, so the old sort - "did this
        answer's first row come from WoRMS" - could not tell them apart and left their
        relative order to whichever finished first. Harmless while their rows were the same
        shape; not harmless once by-vernacular rows carry vernames, because then network
        weather decides whether a folded row's hint reads as the synonym the diver typed or as
        somebody's common name. Run twice with the slow leg swapped, byte for byte.
        """
        synonym = {
            "AphiaID": 384056,
            "scientificname": "Orca tethyos",
            "status": "unaccepted",
            "rank": "Species",
            "valid_AphiaID": 137107,
            "valid_name": "Stenella coeruleoalba",
        }

        async def page_with(slow_endpoint: str) -> str:
            real_worms = species_service._worms

            async def paced(endpoint: str, segment: Any, params: Any = None) -> Any:
                if endpoint == slow_endpoint:
                    await anyio.sleep(0.05)
                return await real_worms(endpoint, segment, params)

            with (
                _registers(
                    by_name=[synonym],
                    by_vernacular=[_worms_record(137107, "Stenella coeruleoalba")],
                    ajax=[_ajax_row(137107, "Stenella coeruleoalba", "orca dolphin")],
                ),
                patch.object(species_service, "_worms", paced),
            ):
                return (await species_service._remote_search("orca")).model_dump_json()

        by_name_last = await page_with("AphiaRecordsByName")
        by_vernacular_last = await page_with("AphiaRecordsByVernacular")

        assert by_name_last == by_vernacular_last
        # And by-name is the writer that wins, on its position in `sources` rather than on
        # having been quick: one rule, the same one the merge follows inside a source.
        results = SpeciesSearchResponse(**json.loads(by_name_last)).results
        assert [(r.aphia_id, r.matched_name) for r in results] == [(137107, "Orca tethyos")]

    @pytest.mark.asyncio
    async def test_exact_matches_rank_above_the_rest(self, no_redis: None):
        db = _empty_db()
        other = {**CLOWNFISH_RECORD, "AphiaID": 999, "scientificname": "Amphiprion percula", "valid_AphiaID": 999}
        with _registers(by_name=[other, CLOWNFISH_RECORD]):
            response = await species_service.search_species(db, "amphiprion ocellaris")

        assert [r.scientific_name for r in response.results] == ["Amphiprion ocellaris", "Amphiprion percula"]


class TestWhatAnEntityIsWorth:
    """What a Wikidata entity has to carry to become a row, and what it says about the taxon.

    Two claims decide both. **P225 is the admission ticket**: an item with an AphiaID and no
    taxon name is not a taxon this app can stand behind, however confidently its label reads.
    **P105 is the rank**, translated through an explicit QID map into WoRMS's own spelling, so
    a Wikidata-only row and the WoRMS row it may merge with never disagree about what a rank
    is called.
    """

    @pytest.mark.asyncio
    async def test_an_item_with_no_taxon_name_builds_no_row(self, no_redis: None):
        """The live `?q=orca` defect, in the two items that caused it. Both are labelled
        "Orca", both carry an AphiaID, and only one carries a taxon name - so the page opened
        with two indistinguishable bare rows, and nothing merged over either of them.

        The surviving row's rank is asserted here too, because the pair is exactly where a
        genus and a subgenus are told apart by nothing else.
        """
        db = _empty_db()
        entities = {
            "entities": {
                "Q41156273": _entity("Q41156273", aphia_id="380520", taxon_name="Orca", rank_item=_GENUS_RANK_ITEM),
                "Q61884050": _entity(
                    "Q61884050", aphia_id="383535", taxon_name=None, rank_item=_SUBGENUS_RANK_ITEM, label="Orca"
                ),
            }
        }
        search = _generator_search(("Q41156273", "Orca"), ("Q61884050", "Orca"))
        with _registers(wikidata_search=search, wikidata_entities=entities):
            response = await species_service.search_species(db, "orca")

        assert [(r.aphia_id, r.scientific_name, r.rank) for r in response.results] == [(380520, "Orca", "Genus")]

    @pytest.mark.asyncio
    async def test_an_unmapped_rank_item_stays_the_sentinel(self, no_redis: None):
        """The recorded residual, pinned so it is a known shape rather than a surprise. A rank
        item outside the map leaves the row saying it does not know, which is honest - and is
        the value that would sit between the ranks under an order that tiers them, which is
        why the map is enumerated up front rather than grown as escapes turn up."""
        db = _empty_db()
        entities = {
            "entities": {
                "Q1": _entity("Q1", aphia_id="278400", taxon_name="Amphiprion ocellaris", rank_item=_UNMAPPED_RANK_ITEM)
            }
        }
        with _registers(wikidata_search=_generator_search("Q1"), wikidata_entities=entities):
            response = await species_service.search_species(db, "amphiprion")

        assert [r.rank for r in response.results] == ["unknown"]

    @pytest.mark.asyncio
    async def test_an_entity_with_no_rank_claim_at_all_stays_the_sentinel(self, no_redis: None):
        db = _empty_db()
        entities = {"entities": {"Q1": _entity("Q1", aphia_id="278400", taxon_name="Amphiprion ocellaris")}}
        with _registers(wikidata_search=_generator_search("Q1"), wikidata_entities=entities):
            response = await species_service.search_species(db, "amphiprion")

        assert [r.rank for r in response.results] == ["unknown"]

    @pytest.mark.asyncio
    async def test_worms_still_owns_the_rank_where_both_registers_answered(self, no_redis: None):
        """Merge precedence is unchanged by any of this: WoRMS owns the taxonomy, so its rank
        stands even when Wikidata now has one of its own to offer. Asserted with the two
        disagreeing, because agreeing proves nothing."""
        db = _empty_db()
        entities = {
            "entities": {
                "Q1": _entity("Q1", aphia_id="278400", taxon_name="Amphiprion ocellaris", rank_item=_GENUS_RANK_ITEM)
            }
        }
        with _registers(
            by_name=[CLOWNFISH_RECORD],
            wikidata_search=_generator_search("Q1"),
            wikidata_entities=entities,
        ):
            response = await species_service.search_species(db, "amphiprion ocellaris")

        assert [(r.rank, r.source) for r in response.results] == [("Species", "worms")]

    @pytest.mark.asyncio
    async def test_two_live_rank_statements_take_the_first(self, no_redis: None):
        """*Mysticeti* is the live case: a parvorder statement and a suborder statement, both
        `normal`, neither disowned. Serialization order decides, which makes the answer the
        same for every reader of a given entity revision - the property this reads P105 for at
        all."""
        db = _empty_db()
        claims = {
            "P850": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "148724"}}}],
            "P225": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "Mysticeti"}}}],
            "P105": [
                {"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": _PARVORDER_RANK_ITEM}}}},
                {"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": _SUBORDER_RANK_ITEM}}}},
            ],
        }
        entities = {"entities": {"Q168366": {"claims": claims}}}
        with _registers(wikidata_search=_generator_search("Q168366"), wikidata_entities=entities):
            response = await species_service.search_species(db, "mysticeti")

        assert [r.rank for r in response.results] == ["Parvorder"]

    @pytest.mark.asyncio
    async def test_a_deprecated_statement_loses_to_the_one_beneath_it(self, no_redis: None):
        """`deprecated` is Wikidata saying the community ruled a value wrong. Reading claims in
        serialization order let it win on position alone, which is how a disowned rank - or a
        disowned AphiaID - reaches a diver."""
        db = _empty_db()
        claims = {
            "P850": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "278400"}}}],
            "P225": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "Amphiprion ocellaris"}}}],
            "P105": [
                {"rank": "deprecated", "mainsnak": {"datavalue": {"value": {"id": _GENUS_RANK_ITEM}}}},
                {"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": _SPECIES_RANK_ITEM}}}},
            ],
        }
        entities = {"entities": {"Q1": {"claims": claims}}}
        with _registers(wikidata_search=_generator_search("Q1"), wikidata_entities=entities):
            response = await species_service.search_species(db, "amphiprion")

        assert [r.rank for r in response.results] == ["Species"]

    @pytest.mark.asyncio
    async def test_a_preferred_statement_wins_from_further_down_the_list(self, no_redis: None):
        """The other half of the same rule, and the reason position alone was never the answer:
        `preferred` is how Wikidata marks the current value where several are true, and it does
        not have to be written first."""
        db = _empty_db()
        claims = {
            "P850": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "278400"}}}],
            "P225": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "Amphiprion ocellaris"}}}],
            "P105": [
                {"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": _GENUS_RANK_ITEM}}}},
                {"rank": "preferred", "mainsnak": {"datavalue": {"value": {"id": _SPECIES_RANK_ITEM}}}},
            ],
        }
        entities = {"entities": {"Q1": {"claims": claims}}}
        with _registers(wikidata_search=_generator_search("Q1"), wikidata_entities=entities):
            response = await species_service.search_species(db, "amphiprion")

        assert [r.rank for r in response.results] == ["Species"]


class TestWikidataBreadthThenEnrichment:
    """The Wikidata search is deliberately much wider than what it enriches, and the two halves
    are held apart by a cut in the middle.

    **Phase 1 asks for names, not claims.** `generator=search` with `prop=entityterms` returns
    fifty candidates and their English terms in about eight kilobytes; the same fifty entities
    *with* their claims run to well over a megabyte. That gap is the only reason breadth is
    affordable, and there is no server-side claim filter to soften it with.

    **Phase 2 enriches the survivors of a pre-rank cut**, scored on those terms with the same
    `_match_bucket` the page ordering uses. So the expensive call stays the size it always was
    while the candidate list grows fivefold.

    The shapes below are all measured against the live endpoint, and every one of them is a way
    a tidier fixture would let a broken reader pass.
    """

    def test_the_search_asks_for_more_candidates_than_it_enriches(self):
        """The premise the whole two-phase shape rests on. If the search asked for no more than
        it enriches, the cut would never cut and Phase 1's cheap breadth would buy nothing at
        all - which was exactly the old arrangement: ten candidates, *below* the enrichment
        limit, so the good rows a diver meant sat past CirrusSearch's tenth and no amount of
        ranking downstream could reach them.

        The width itself is a tuning decision and deliberately not pinned to a figure here; the
        inequality is the design, and it is what a revert would break first.
        """
        assert species_service._WIKIDATA_SEARCH_LIMIT > species_service._WIKIDATA_ENRICH_LIMIT
        # Wikidata's anonymous ceiling for the parameter, measured: 501 comes back with a
        # "must be between 1 and 500" warning instead of the page that was asked for.
        assert species_service._WIKIDATA_SEARCH_LIMIT <= 500

    def test_candidates_come_back_in_relevance_order_not_object_order(self):
        """`query.pages` is keyed by pageid, and that key order is arbitrary - on the live
        `whale` answer the first three keys carry indices 31, 30 and 1. `index` is the only
        thing that says what CirrusSearch actually ranked first, and since the cut keeps the
        head of the list, reading the object in iteration order would discard the wrong end.
        """
        payload = _generator_search("Q1", "Q2", "Q3")

        assert [page["title"] for page in payload["query"]["pages"].values()] == ["Q3", "Q2", "Q1"], (
            "the fixture has stopped handing these over out of order, so this test proves nothing"
        )
        pages = species_service._wikidata_search_pages(payload)
        assert pages is not None
        assert [page.qid for page in pages] == ["Q1", "Q2", "Q3"]

    def test_a_candidate_with_no_english_label_keeps_its_aliases(self):
        """Q733595, the live `nudibranch` front-runner, has aliases and **no English label at
        all**; 22 of that query's 50 pages carry no `alias` key. `entityterms` omits a key
        rather than emptying it, independently for each, so a reader that indexes both loses a
        whole candidate to a `KeyError` - and it would be the top-ranked one here.
        """
        pages = species_service._wikidata_search_pages(
            _generator_search(("Q733595", "", "Nudibranchs"), ("Q2", "sea slug"))
        )

        assert pages is not None
        assert [(page.qid, page.terms) for page in pages] == [("Q733595", ("Nudibranchs",)), ("Q2", ("sea slug",))]

    def test_a_candidate_with_no_terms_at_all_is_still_a_candidate(self):
        """Never observed across the sampled payloads, and handled deliberately rather than by
        luck: a page with no `entityterms` whatsoever contributes no names, which sinks it to
        the bottom of the pre-rank - it does not raise on the way there."""
        assert species_service._wikidata_search_pages(_generator_search("Q1")) == [
            species_service._WikidataPage(qid="Q1", terms=())
        ]

    @pytest.mark.parametrize(
        ("label", "payload", "expected"),
        [
            # Twenty bytes, measured stable across distinct no-match queries, and the *only*
            # shape that means "matched nothing" on this variant.
            ("the measured empty body", {"batchcomplete": ""}, []),
            # The Action API reports read-only mode and a busy backend this way; nothing about
            # the transport says anything went wrong.
            ("a 200 carrying an error", {"error": {"code": "readonly", "info": "read-only"}}, None),
            ("an unrecognizable shape", {"unexpected": True}, None),
            ("a transport failure", None, None),
            # The reason the two readers are separate, and the reason the fixture routes them
            # apart by URL: `list=search`'s empty answer carries `query.search`, present and
            # empty, so a reader treating a missing `pages` key as "nothing found" would call
            # every one of this variant's failures an empty register.
            ("the other variant's empty answer", {"query": {"search": []}}, None),
        ],
    )
    def test_the_three_outcomes(self, label: str, payload: Any, expected: list[Any] | None):
        """Three outcomes rather than two, and the third is the one worth having: "the register
        had nothing" and "the register never answered" are the same empty list downstream, and
        conflating them is what pins a half-answer under the thirty-day TTL."""
        assert species_service._wikidata_search_pages(payload) == expected

    @pytest.mark.asyncio
    async def test_the_cut_keeps_a_late_match_over_an_early_one_that_matches_nothing(self, no_redis: None):
        """The cut has to cut by relevance rather than by position, or breadth buys nothing.
        CirrusSearch ranks well for charismatic megafauna and much less well elsewhere, so a
        candidate whose own name is a plain hit can sit well down the list under candidates
        that say nothing about what was typed - and a positional cut would enrich the latter.
        """
        db = _empty_db()
        filler = [(f"Q{n}", f"Nothing {n}") for n in range(1, species_service._WIKIDATA_ENRICH_LIMIT + 1)]
        entities = {"entities": {"Q999": _entity("Q999", aphia_id="105809", taxon_name="Rhincodon typus")}}

        with _registers(
            wikidata_search=_generator_search(*filler, ("Q999", "whale shark")), wikidata_entities=entities
        ) as providers:
            response = await species_service.search_species(db, "whale")

        asked = [
            qid
            for url in providers.urls()
            if "wbgetentities" in url
            for qid in parse_qs(urlparse(url).query)["ids"][0].split("|")
        ]
        assert len(asked) == species_service._WIKIDATA_ENRICH_LIMIT
        assert "Q999" in asked, "the one candidate that matched the query was cut for sitting last"
        # And something had to give way for it: the last filler is the one over the line.
        assert f"Q{species_service._WIKIDATA_ENRICH_LIMIT}" not in asked
        assert [r.scientific_name for r in response.results] == ["Rhincodon typus"]

    @pytest.mark.asyncio
    async def test_a_row_says_which_of_its_names_the_query_matched(self, no_redis: None):
        """*Orcinus orca* on `?q=whale`, which is the case that makes this worth threading at
        all: the row displays "Orca gladiator", which accounts for nothing a diver typed, and
        the reason it is on the page is the alias "orca whale". The entity fetch cannot say
        that - it lists the names without saying which one was hit - so the term has to travel
        down from the candidate that matched. Under the ranking key a hint places a row only
        where the visible names place it nowhere, so explaining a row never promotes it.
        """
        db = _empty_db()
        payload = _generator_search(("Q26843", "Orcinus orca", "Orca gladiator", "orca whale", "killer whale"))

        with _registers(wikidata_search=payload, wikidata_entities=ORCA_WIKIDATA_ENTITIES):
            response = await species_service.search_species(db, "whale")

        assert [(r.common_name, r.matched_name) for r in response.results] == [("Orca gladiator", "orca whale")]

    def test_a_term_that_repeats_the_displayed_name_is_not_an_explanation(self):
        """The schema's own contract for the field - null when the display name already explains
        the match - kept at the source, where both of the row's names are in hand. The merged
        pass afterwards catches only what a single source cannot see, so leaving it all to that
        would put the rule in one place and the knowledge in another.
        """
        entity = species_service._WikidataEntity(
            qid="Q1126155",
            aphia_id=278400,
            scientific_name="Amphiprion ocellaris",
            label="Amphiprion ocellaris",
            aliases=("ocellaris clownfish",),
            rank="Species",
        )

        for term, expected in (
            ("ocellaris clownfish", None),
            # Casefolded, because the display name is capitalised on the way out while the term
            # is quoted exactly as the register wrote it.
            ("Ocellaris Clownfish", None),
            ("Amphiprion ocellaris", None),
            ("anemonefish", "anemonefish"),
        ):
            result = species_service._wikidata_result(entity, term)
            assert result is not None
            assert result.matched_name == expected, term

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "candidates", "more", "expected"),
        [
            ("wikidata itself held hits back", 1, True, True),
            # `nudibranch` is the measured case for this clause: fifty candidates and no
            # `continue` at all, because fifty *is* the whole result set - and then most of them
            # are discarded here. A flag reading only `continue` would call that page complete.
            ("more candidates than the cut enriches", species_service._WIKIDATA_ENRICH_LIMIT + 1, False, True),
            ("everything found, everything enriched", species_service._WIKIDATA_ENRICH_LIMIT, False, False),
        ],
    )
    async def test_both_kinds_of_truncation_reach_has_more(
        self, no_redis: None, label: str, candidates: int, more: bool, expected: bool
    ):
        """Two ways this page can fall short of the truth, and Wikidata knows about only one of
        them. It sets `continue` when it held hits back; the pre-rank cut is this app's own
        truncation and is invisible from there."""
        payload = _generator_search(*[f"Q{n}" for n in range(candidates)], more=more)

        with _registers(wikidata_search=payload):
            answer = await species_service._wikidata_search("whale")

        assert answer.page_was_full is expected


class TestHowWellANameMatches:
    """`_match_bucket` is the one predicate three different things ask, so the table it
    implements is pinned here rather than inferred from the orderings it produces."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("whale", species_service._MATCH_EXACT),
            # Prefix above word boundary is an owner decision with a known cost: "Whale louse
            # family" outranks "Blue whale" for `?q=whale`, and the whale sharks divers
            # actually log are what it buys.
            ("Whale shark", species_service._MATCH_PREFIX),
            ("blue whale", species_service._MATCH_WORD),
            # `\b` gets hyphenated names for free, which is the reason it is a regex at all.
            ("killer-whale", species_service._MATCH_WORD),
            # A plural is *not* a word-boundary match, and the substring bucket is the right
            # place for it: above the unmatched mass, below the exact word.
            ("toothed whales", species_service._MATCH_SUBSTRING),
            ("Balaenoptera musculus", species_service._MATCH_NONE),
        ],
    )
    def test_the_bucket_table(self, name: str, expected: int) -> None:
        assert species_service._match_bucket("whale", name) == expected

    def test_a_multi_word_query_matches_on_its_whole_boundary(self) -> None:
        assert species_service._match_bucket("killer whale", "false killer whale") == species_service._MATCH_WORD

    def test_the_name_side_is_casefolded(self) -> None:
        assert species_service._match_bucket("whale", "WHALE") == species_service._MATCH_EXACT

    @pytest.mark.parametrize(
        ("rank", "expected"),
        [("Species", 0), ("Subspecies", 0), ("Forma", 0), ("unknown", 1), ("Genus", 2), ("Family", 2)],
    )
    def test_the_rank_tier(self, rank: str, expected: int) -> None:
        assert species_service._rank_tier(rank) == expected

    def test_a_rank_nobody_enumerated_falls_below_the_sentinel_rather_than_above_it(self) -> None:
        """The direction the tier's rule form exists for. "Infraorder" is in no list here, and
        it has to sort *with* genus and family rather than between them and the species - an
        enumerated higher tier would have floated every rank it forgot."""
        assert species_service._rank_tier("Infraorder") == species_service._rank_tier("Genus")


class TestHowRowsAreRanked:
    """The ranking key, exercised on finished rows so each term can be isolated.

    The governing rule is one sentence: rows rank by the names the diver can see, and a hidden
    name places a row only where the visible names place it nowhere.
    """

    def test_a_prefix_outranks_a_word_boundary_which_outranks_a_substring(self) -> None:
        rows = [
            _result(3, "Balaenidae", common_name="right whales and bowhead whales"),
            _result(2, "Balaenoptera musculus", common_name="blue whale"),
            _result(1, "Rhincodon typus", common_name="whale shark"),
        ]
        assert [r.aphia_id for r in species_service._ordered(rows, "whale")] == [1, 2, 3]

    def test_a_hint_placed_row_sits_below_every_visibly_matching_row(self) -> None:
        """The measured case, and the finding that made "visible" the first key term rather
        than one bucket among many. The genus *Orcinus* contains no "orca" a diver can read -
        it is placed entirely by its synonym, an *exact* hint - and it must still not beat a
        row whose own binomial merely contains the query.
        """
        orcinus = _result(137021, "Orcinus", rank="Genus", matched_name="Orca")
        pseudorca = _result(137104, "Pseudorca crassidens")
        assert [r.aphia_id for r in species_service._ordered([orcinus, pseudorca], "orca")] == [137104, 137021]

    def test_inside_the_hint_band_rows_order_by_their_own_hint(self) -> None:
        """`?q=swordfish`: the orca really does carry "swordfish" as a WoRMS vernacular, so it
        belongs on the page - explained, second, under the animal that owns the name - and the
        remora whose vername merely starts with it belongs under that."""
        orca = _result(137102, "Orcinus orca", common_name="Orca whale", matched_name="swordfish")
        remora = _result(126413, "Remora brachyptera", matched_name="swordfish sucker")
        assert [r.aphia_id for r in species_service._ordered([remora, orca], "swordfish")] == [137102, 126413]

    def test_a_species_outranks_a_higher_taxon_in_the_same_bucket(self) -> None:
        """The owner's ruling - "as a diver I'm most interested in the species I spotted" -
        and the thing that softens the prefix-first cost without reopening it: the whale louse
        family still prefix-matches, and the whale shark still opens the page. Asserted with
        the alphabet pointing the other way, or it proves nothing."""
        louse = _result(1, "Cyamidae", common_name="Whale louse family", rank="Family")
        shark = _result(2, "Rhincodon typus", common_name="Whale shark")
        assert [r.aphia_id for r in species_service._ordered([louse, shark], "whale")] == [2, 1]

    def test_the_sentinel_rank_sits_between_the_two_tiers(self) -> None:
        """ "We do not know" is not "we know it is a genus", and the middle is the only honest
        place for it. The names run backwards against the wanted order so the tier is what is
        being measured."""
        rows = [
            _result(1, "Whale aaa", rank="Genus"),
            _result(2, "Whale bbb", rank="unknown"),
            _result(3, "Whale ccc", rank="Species"),
        ]
        assert [r.aphia_id for r in species_service._ordered(rows, "whale")] == [3, 2, 1]

    def test_a_named_row_outranks_a_bare_binomial_in_the_same_bucket_and_tier(self) -> None:
        """Today's `?q=orca` absurdity inverted: the animal sat at position 17 while two
        indistinguishable bare "Orcadia" genus rows opened the page. Same bucket, same tier -
        the row that can tell the diver what it is goes first, and the alphabet is set against
        it here so the tie-break cannot be what passes this."""
        bare = _result(1, "Orca aaa")
        named = _result(2, "Orcinus orca", common_name="Orca zzz")
        assert [r.aphia_id for r in species_service._ordered([bare, named], "orca")] == [2, 1]

    def test_the_tie_break_reads_the_displayed_name_and_ignores_case(self) -> None:
        """Two artefacts at once. The old key sorted on `scientific_name` - a column most rows
        are not displaying - and compared raw `str`s, so ASCII capitals sorted ahead of
        lowercase. Both rows here display a `common_name`, and "Zebra" must not lead "apple".
        """
        rows = [_result(1, "Aaa aaa", common_name="Whale Zebra"), _result(2, "Zzz zzz", common_name="whale apple")]
        assert [r.aphia_id for r in species_service._ordered(rows, "whale")] == [2, 1]

    def test_the_order_is_total_so_two_identical_rows_cannot_swap(self) -> None:
        """`aphia_id` last. Nothing above it separates these, and without it the order would
        depend on which register happened to be merged first."""
        rows = [_result(9, "Whale sp.", common_name="Whale"), _result(4, "Whale sp.", common_name="Whale")]
        assert [r.aphia_id for r in species_service._ordered(rows, "whale")] == [4, 9]

    def test_a_row_nothing_matches_sinks_below_the_explained_ones(self) -> None:
        """CirrusSearch matches page text that labels and aliases never carry - *Orca
        latirostris* on `?q=Orcinus orca` is the recorded exception to "every hit says what
        matched" - and the tail is where such a row belongs."""
        explained = _result(1, "Feresa attenuata", matched_name="Orca intermedia")
        unexplained = _result(2, "Peristedion cataphractum")
        assert [r.aphia_id for r in species_service._ordered([unexplained, explained], "orca")] == [1, 2]


class TestExplainingAVernacularHit:
    """Where `matched_name` comes from on a WoRMS row, and where it is taken away again.

    An `AphiaRecord` carries no vernacular field, so `AphiaRecordsByVernacular` returns rows
    that matched on a common name and cannot say which one - "*Batis maritima*, a saltmarsh
    plant, second for `?q=turtle`" with nothing on screen to account for it.
    `AjaxAphiaRecordsByNamePart` is the only endpoint that knows, and it rides alongside as an
    annotation: no rows of its own, no `has_more`, no fold.
    """

    @pytest.mark.asyncio
    async def test_a_vername_says_why_a_by_vernacular_row_matched(self, no_redis: None):
        db = _empty_db()
        with _registers(
            by_vernacular=[_worms_record(137102, "Orcinus orca")],
            ajax=[_ajax_row(137102, "Orcinus orca", "killer whale")],
        ):
            response = await species_service.search_species(db, "killer whale")

        assert [(r.scientific_name, r.matched_name) for r in response.results] == [("Orcinus orca", "killer whale")]

    @pytest.mark.asyncio
    async def test_a_folded_row_keeps_the_folds_own_account_of_the_match(self, no_redis: None):
        """The ajax map is keyed by the id WoRMS put on the record, before the fold moves the
        row - those rows carry no `valid_AphiaID` at all, and the `whale` answer contains two
        "blue whale" ids, one of them an unaccepted homonym of the fin whale. So the fold wins
        any collision: the diver typed a superseded binomial, and that is the true account of
        what they hit."""
        db = _empty_db()
        with _registers(
            by_vernacular=[MANTA_SYNONYM_RECORD],
            ajax=[_ajax_row(105857, "Manta birostris", "giant manta")],
        ):
            response = await species_service.search_species(db, "manta birostris")

        assert [(r.scientific_name, r.matched_name) for r in response.results] == [
            ("Mobula birostris", "Manta birostris")
        ]

    @pytest.mark.asyncio
    async def test_a_vername_in_any_language_still_explains_the_row(self, no_redis: None):
        """The owner's own live case: `?q=orca` returns a shad, and nothing visible accounts
        for it until you learn its Spanish name is "samborca". The English-only rule governs
        the display name; a foreign word that says why a row appeared is information."""
        db = _empty_db()
        with _registers(
            by_vernacular=[_worms_record(126413, "Alosa alosa")],
            ajax=[_ajax_row(126413, "Alosa alosa", "samborca", "spa")],
        ):
            response = await species_service.search_species(db, "orca")

        assert [r.matched_name for r in response.results] == ["samborca"]

    @pytest.mark.asyncio
    async def test_the_better_match_beats_the_english_one(self, no_redis: None):
        """A taxon with several vernames keeps the one that best answers what was typed, and
        language only breaks a tie underneath that."""
        db = _empty_db()
        with _registers(
            by_vernacular=[_worms_record(137111, "Feresa attenuata")],
            ajax=[
                _ajax_row(137111, "Feresa attenuata", "orca whale", "eng"),
                _ajax_row(137111, "Feresa attenuata", "orca", "spa"),
            ],
        ):
            response = await species_service.search_species(db, "orca")

        assert [r.matched_name for r in response.results] == ["orca"]

    @pytest.mark.asyncio
    async def test_english_wins_between_two_equally_good_vernames(self, no_redis: None):
        """With the alphabet set against it, and the Spanish row sent first, so neither the
        tie-break below nor arrival order can be what passes this."""
        db = _empty_db()
        with _registers(
            by_vernacular=[_worms_record(137111, "Feresa attenuata")],
            ajax=[
                _ajax_row(137111, "Feresa attenuata", "orca aaa", "spa"),
                _ajax_row(137111, "Feresa attenuata", "orca zzz", "eng"),
            ],
        ):
            response = await species_service.search_species(db, "orca")

        assert [r.matched_name for r in response.results] == ["orca zzz"]

    @pytest.mark.asyncio
    async def test_a_remaining_tie_is_broken_alphabetically_rather_than_by_response_order(self, no_redis: None):
        """*Balaena mysticetus* carries nine vernames for `whale` and their order within the
        response is measured-arbitrary. The chosen one reaches the sort key, so letting
        arrival order pick it would let WoRMS decide page order between two identical
        searches."""
        db = _empty_db()
        with _registers(
            by_vernacular=[_worms_record(137021, "Delphinidae", rank="Family")],
            ajax=[
                _ajax_row(137021, "Delphinidae", "orca zulu"),
                _ajax_row(137021, "Delphinidae", "orca alpha"),
            ],
        ):
            response = await species_service.search_species(db, "orca")

        assert [r.matched_name for r in response.results] == ["orca alpha"]

    @pytest.mark.asyncio
    async def test_a_vername_that_merely_repeats_the_binomial_is_not_a_hint(self, no_redis: None):
        """`combine_vernaculars=true` adds vernacular matching to a scientific-name search, so
        a name the row is already showing can come back as its own explanation."""
        db = _empty_db()
        with _registers(
            by_vernacular=[_worms_record(137102, "Orcinus orca")],
            ajax=[_ajax_row(137102, "Orcinus orca", "orcinus orca")],
        ):
            response = await species_service.search_species(db, "whale")

        assert [r.matched_name for r in response.results] == [None]

    @pytest.mark.asyncio
    async def test_a_hint_is_dropped_once_the_merged_display_name_explains_the_row(self, no_redis: None):
        """The schema has promised this all along - "null when the display name already
        explains the match" - and no single source can keep it. `_worms_result` sees a row
        whose `common_name` is always `None`; the name arrives from Wikidata at the merge. So
        `?q=swordfish` would have shipped *Xiphias gladius* as `Swordfish · matched
        "swordfish"`.
        """
        db = _empty_db()
        entities = {
            "entities": {
                "Q1": _entity(
                    "Q1",
                    aphia_id="127094",
                    taxon_name="Xiphias gladius",
                    rank_item=_SPECIES_RANK_ITEM,
                    label="swordfish",
                )
            }
        }
        with _registers(
            by_vernacular=[_worms_record(127094, "Xiphias gladius")],
            ajax=[_ajax_row(127094, "Xiphias gladius", "swordfish")],
            wikidata_search=_generator_search("Q1"),
            wikidata_entities=entities,
        ):
            response = await species_service.search_species(db, "swordfish")

        assert [(r.common_name, r.matched_name) for r in response.results] == [("Swordfish", None)]

    @pytest.mark.asyncio
    async def test_the_rule_is_any_match_rather_than_equality(self, no_redis: None):
        """Simulated over the live `?q=whale` payloads, four of the first sixteen rows carried
        a hint the display already covered - "Bowhead whale · matched \\"whale-fish\\"". An
        equality test lets every one of those through."""
        db = _empty_db()
        entities = {
            "entities": {
                "Q1": _entity(
                    "Q1",
                    aphia_id="137090",
                    taxon_name="Balaena mysticetus",
                    rank_item=_SPECIES_RANK_ITEM,
                    label="bowhead whale",
                )
            }
        }
        with _registers(
            by_vernacular=[_worms_record(137090, "Balaena mysticetus")],
            ajax=[_ajax_row(137090, "Balaena mysticetus", "whale-fish")],
            wikidata_search=_generator_search("Q1"),
            wikidata_entities=entities,
        ):
            response = await species_service.search_species(db, "whale")

        assert [(r.common_name, r.matched_name) for r in response.results] == [("Bowhead whale", None)]

    @pytest.mark.asyncio
    async def test_the_fold_hint_survives_where_no_visible_name_explains_the_row(self, no_redis: None):
        """The other half of the same rule, and the reconciliation it needs. `?q=manta` with
        Wikidata's "Giant oceanic manta ray" on the row no longer says `matched "Manta
        birostris"` - the display explains it now. Strip the Wikidata name and the bafflement
        the hint exists for is back, so the hint is too.
        """
        db = _empty_db()
        entities = {
            "entities": {
                "Q1": _entity(
                    "Q1",
                    aphia_id="1015526",
                    taxon_name="Mobula birostris",
                    rank_item=_SPECIES_RANK_ITEM,
                    label="giant oceanic manta ray",
                )
            }
        }
        with _registers(
            by_vernacular=[MANTA_SYNONYM_RECORD],
            wikidata_search=_generator_search("Q1"),
            wikidata_entities=entities,
        ):
            named = await species_service.search_species(db, "manta")
        with _registers(by_vernacular=[MANTA_SYNONYM_RECORD]):
            bare = await species_service.search_species(db, "manta")

        assert [(r.common_name, r.matched_name) for r in named.results] == [("Giant oceanic manta ray", None)]
        assert [(r.common_name, r.matched_name) for r in bare.results] == [(None, "Manta birostris")]

    @pytest.mark.asyncio
    async def test_the_annotation_contributes_no_rows_of_its_own(self, no_redis: None):
        """The duplicate class that made an earlier design reject ajax outright: its ids are
        raw and unfolded, so `whale` returns two "blue whale" rows under different ids with
        nothing marking the second as an unaccepted homonym. A row source would have to fold
        them; an annotation simply never sees them."""
        db = _empty_db()
        with _registers(ajax=[_ajax_row(380449, "Balaenoptera musculus", "blue whale")]):
            response = await species_service.search_species(db, "whale")

        assert response.results == []


class TestChoosingTheDisplayName:
    """`_choose_common_name`, whose order is forced by what the sources contain rather than
    by preference: a taxon's English Wikidata label is usually the binomial itself.

    Two guards on top of that order, and they catch different things. The prefix test rejects
    a candidate that *is* the accepted binomial with decoration on it; `rejected` - the
    taxon's WoRMS synonym list, which only the resolve path has - rejects one that is a
    superseded binomial under some other genus. Everything that survives is capitalised on its
    first character, because neither Wikidata field is normalised at source.
    """

    def test_a_label_that_repeats_the_binomial_is_not_a_common_name(self):
        assert (
            species_service._choose_common_name(
                scientific_name="Amphiprion ocellaris",
                label="Amphiprion ocellaris",
                aliases=("ocellaris clownfish",),
            )
            == "Ocellaris clownfish"
        )

    def test_a_label_that_differs_wins_outright(self):
        assert (
            species_service._choose_common_name(
                scientific_name="Mobula birostris", label="giant oceanic manta ray", aliases=("manta ray",)
            )
            == "Giant oceanic manta ray"
        )

    def test_a_worms_vernacular_is_the_last_resort(self):
        """Last because WoRMS's English coverage is the thin part - but still tried, since a
        taxon Wikidata has never heard of may well have one."""
        assert (
            species_service._choose_common_name(
                scientific_name="Muraenidae", label=None, aliases=(), vernaculars=("moray eels",)
            )
            == "Moray eels"
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
            == "Ocellaris clownfish"
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

    def test_a_junior_scientific_synonym_is_skipped_for_the_next_alias(self):
        """The killer whale, which this app displayed as "Orca gladiator" - a superseded
        *scientific* name. Its Wikidata label is the accepted binomial, so the first alias
        wins, and that alias shares no prefix with *Orcinus orca* for the test above to catch.
        Only WoRMS's synonym list knows it is not a name for the animal."""
        assert (
            species_service._choose_common_name(
                scientific_name="Orcinus orca",
                label="Orcinus orca",
                aliases=("Orca gladiator", "orca whale", "killer whale"),
                rejected=("Orca gladiator", "Orca capensis", "Gladiator gladiator"),
            )
            == "Orca whale"
        )

    def test_a_rejected_name_is_matched_case_insensitively(self):
        """The two registers disagree about case constantly, so an exact-case comparison would
        let the same synonym through under a different capitalisation."""
        assert (
            species_service._choose_common_name(
                scientific_name="Orcinus orca",
                label="ORCA GLADIATOR",
                aliases=("killer whale",),
                rejected=("orca gladiator",),
            )
            == "Killer whale"
        )

    def test_rejection_is_equality_rather_than_a_prefix(self):
        """Deliberately narrower than the binomial test beside it. A synonym list runs to
        dozens of names, so prefix-matching every one of them would start eating real
        vernaculars: here the reject entry is a prefix of the answer this is supposed to
        arrive at."""
        assert (
            species_service._choose_common_name(
                scientific_name="Orcinus orca", label=None, aliases=("orca whale",), rejected=("Orca",)
            )
            == "Orca whale"
        )

    def test_the_reject_list_reaches_vernaculars_too(self):
        """Constructed rather than sampled, because the failure it guards against is an
        implementation that filters only the Wikidata half and ships a WoRMS vernacular the
        synonym list had already disowned. Nothing survives here, so the row falls back to its
        binomial."""
        assert (
            species_service._choose_common_name(
                scientific_name="Orcinus orca",
                label="Orcinus orca",
                aliases=("Orca gladiator",),
                vernaculars=("Orca capensis",),
                rejected=("Orca gladiator", "Orca capensis"),
            )
            is None
        )

    def test_only_the_first_character_is_uppercased(self):
        """Lowercasing the rest is the tempting other half of this rule and would be wrong:
        both of these carry a capital that belongs to the name."""
        assert (
            species_service._choose_common_name(
                scientific_name="Amphiprion bicinctus", label="Red Sea clownfish", aliases=()
            )
            == "Red Sea clownfish"
        )
        assert (
            species_service._choose_common_name(
                scientific_name="Balaenoptera musculus", label=None, aliases=("Sibbold's Rorqual",)
            )
            == "Sibbold's Rorqual"
        )

    def test_capitalisation_reaches_every_source_field(self):
        """The defect is not "labels are lowercase and aliases are not" - "whale shark" is a
        label and "Blacktip reef shark" is a label too. Neither field is normalised at source,
        so all three candidate kinds go through the same rule."""
        chosen = [
            species_service._choose_common_name(scientific_name="Rhincodon typus", label="whale shark", aliases=()),
            species_service._choose_common_name(
                scientific_name="Physeter macrocephalus", label=None, aliases=("sperm whale",)
            ),
            species_service._choose_common_name(
                scientific_name="Muraenidae", label=None, aliases=(), vernaculars=("moray eels",)
            ),
        ]
        assert chosen == ["Whale shark", "Sperm whale", "Moray eels"]


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
                return httpx.Response(200, json=_generator_search())
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
    async def test_a_failed_annotation_keeps_the_rows_and_takes_the_short_ttl(self, fake_redis: FakeRedis):
        """The ajax leg adds no rows, so losing it costs nothing a diver can see - which is
        exactly why the hour is worth paying for. Left on the month, the flagship query would
        cache *unexplained* for thirty days and the feature would be invisible for a month
        precisely where it matters. The hour is the price of self-healing.
        """
        db = _empty_db()

        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "AjaxAphiaRecordsByNamePart" in url:
                raise httpx.ConnectError("the ajax call is down")
            if "AphiaRecordsByVernacular" in url:
                return httpx.Response(200, json=[_worms_record(137102, "Orcinus orca")])
            if "wbgetentities" in url:
                return httpx.Response(200, json={"entities": {}})
            if "wikidata" in url:
                return httpx.Response(200, json=_generator_search())
            return httpx.Response(200, json=[])

        with _Providers(handle):
            response = await species_service.search_species(db, "killer whale")

        assert [(r.scientific_name, r.matched_name) for r in response.results] == [("Orcinus orca", None)]
        assert set(fake_redis.expiries.values()) == {species_service._MISS_TTL_SECONDS}

    @pytest.mark.asyncio
    async def test_an_empty_annotation_is_a_complete_answer_not_a_failure(self, fake_redis: FakeRedis):
        """WoRMS says "no match" with a 204 that `_request` already maps to `[]`, and the ajax
        endpoint is no different. "Nothing to explain" must not be dragged down to the short
        TTL along with "we could not ask"."""
        db = _empty_db()
        with _registers(by_vernacular=[_worms_record(137102, "Orcinus orca")], ajax=[]):
            await species_service.search_species(db, "killer whale")

        assert set(fake_redis.expiries.values()) == {species_service._HIT_TTL_SECONDS}

    @pytest.mark.asyncio
    async def test_a_hung_annotation_gives_up_at_its_own_budget_not_the_searchs(self, fake_redis: FakeRedis):
        """`_request`'s own bounds sit far above `_SEARCH_BUDGET_SECONDS`, so without an inner
        one a hung ajax call would have the whole by-vernacular source cancelled by the search
        budget - throwing away rows that had already arrived, which is worse than every case
        this design accepts. The budget is patched down here; what is under test is that the
        source answers at the *inner* bound rather than the outer one.
        """
        db = _empty_db()
        real_worms = species_service._worms

        async def hang_on_the_annotation(endpoint: str, segment: Any, params: Any = None) -> Any:
            if endpoint.startswith("Ajax"):
                await anyio.sleep(species_service._SEARCH_BUDGET_SECONDS * 10)
            return await real_worms(endpoint, segment, params)

        with (
            _registers(by_vernacular=[_worms_record(137102, "Orcinus orca")]),
            patch.object(species_service, "_worms", hang_on_the_annotation),
            patch.object(species_service, "_AJAX_ANNOTATION_BUDGET_SECONDS", 0.05),
        ):
            started = anyio.current_time()
            response = await species_service.search_species(db, "killer whale")
            elapsed = anyio.current_time() - started

        assert [r.scientific_name for r in response.results] == ["Orcinus orca"]
        assert elapsed < species_service._SEARCH_BUDGET_SECONDS, "the search waited out its own budget"
        assert set(fake_redis.expiries.values()) == {species_service._MISS_TTL_SECONDS}

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
    async def test_the_annotation_call_carries_the_parameters_it_cannot_work_without(self, no_redis: None):
        """Three measured traps in one URL. `max_matches` defaults to twenty and, above its
        ceiling of fifty, is *discarded* rather than clamped - so asking for a hundred returns
        fewer rows than asking for fifty. `marine_only` is not inert here whatever it does on
        the record endpoints: `Astyanax` returns 11 rows under `true` and 50 under `false`,
        and the documented default disagrees with the observed one. And no `languages[]`
        filter is sent at all, on the owner's cross-language ruling - a Spanish "samborca" is
        what explains a shad on `?q=orca`.
        """
        db = _empty_db()
        with _registers(by_vernacular=[_worms_record(137102, "Orcinus orca")]) as providers:
            await species_service.search_species(db, "orca")

        ajax = [url for url in providers.urls() if "AjaxAphiaRecordsByNamePart" in url]
        assert len(ajax) == 1, "the annotation call went out more than once, or not at all"
        params = parse_qs(urlparse(ajax[0]).query)
        assert params["combine_vernaculars"] == ["true"]
        assert params["marine_only"] == ["false"]
        assert params["max_matches"] == [str(species_service._AJAX_MAX_MATCHES)]
        assert 0 < species_service._AJAX_MAX_MATCHES <= 50, "above the ceiling the value is discarded, not clamped"
        assert not [key for key in params if key.startswith("languages")]

    @pytest.mark.asyncio
    async def test_wikidata_is_filtered_to_entities_carrying_an_aphia_id(self, no_redis: None):
        """The filter is what keeps the result set to taxa WoRMS also knows, which is what
        makes the merge possible at all."""
        db = _empty_db()
        with _registers(wikidata_search=WIKIDATA_SEARCH, wikidata_entities=WIKIDATA_ENTITIES) as providers:
            await species_service.search_species(db, "clownfish")

        assert any("haswbstatement" in url and "P850" in url for url in providers.urls())

    @pytest.mark.asyncio
    async def test_the_search_call_asks_for_names_rather_than_claims(self, no_redis: None):
        """The whole of what makes fifty candidates affordable is in this one URL, and every way
        of getting it wrong is quiet. `prop=entityterms` returns the candidates' English names
        in about eight kilobytes; asking for the same fifty entities *with* their claims runs to
        well over a megabyte, which `_MAX_RESPONSE_BYTES` would then discard wholesale. Drop
        `wbetterms` and the terms simply vanish, taking the pre-rank cut's only input and every
        row's explanation with them - the search still answers, with worse rows.
        """
        db = _empty_db()
        with _registers() as providers:
            await species_service.search_species(db, "whale")

        url = next(url for url in providers.urls() if "wikidata" in url)
        params = parse_qs(urlparse(url).query)

        assert params["generator"] == ["search"]
        assert params["gsrsearch"] == ["whale haswbstatement:P850"]
        assert params["gsrlimit"] == [str(species_service._WIKIDATA_SEARCH_LIMIT)]
        assert params["prop"] == ["entityterms"]
        assert (params["wbetterms"], params["wbetlanguage"]) == (["label|alias"], ["en"])
        assert "claims" not in url

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
        external identifiers - about 50 KB each. Ten entities in one response routinely exceed
        `_MAX_RESPONSE_BYTES` (measured live: "shark" 667 KB, "turtle" 642 KB), which makes
        `_request` return `None` and silently costs the whole Wikidata contribution for
        exactly the words divers type most.

        The search now asks for far more candidates than it enriches, so this pins both halves
        at once: the chunk size, and the fact that what gets chunked is the *survivors* of the
        pre-rank cut rather than every hit the search returned. Conservation across the whole
        candidate list would be the wrong assertion now - discarding most of them is the design,
        and asserting the old invariant would have quietly required the cut not to exist.
        """
        qids = [f"Q{n}" for n in range(species_service._WIKIDATA_SEARCH_LIMIT)]
        db = _empty_db()

        with _registers(wikidata_search=_generator_search(*qids), wikidata_entities={"entities": {}}) as providers:
            await species_service.search_species(db, "shark")

        # Parsed rather than counted off the raw URL: `props=claims|labels|aliases` is
        # pipe-separated too, so a substring count would measure the wrong parameter.
        batches = [parse_qs(urlparse(url).query)["ids"][0] for url in providers.urls() if "wbgetentities" in url]
        survivors = min(len(qids), species_service._WIKIDATA_ENRICH_LIMIT)

        assert len(batches) > 1, "every enriched id went out in one request"
        assert sum(len(ids.split("|")) for ids in batches) == survivors, "the batches are not exactly the survivors"
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
            wikidata_lookup=WIKIDATA_LOOKUP,
            wikidata_entities=WIKIDATA_ENTITIES,
        ):
            species = await species_service.resolve_species(db, 278400)

        assert species.aphia_id == 278400
        assert species.scientific_name == "Amphiprion ocellaris"
        assert species.common_name == "Ocellaris clownfish"
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
        # Raw, uncapitalised, while `common_name` above is not: the index has no display job,
        # and `ILIKE` does not care, so there is nothing to gain by rewriting what a register
        # actually said.
        assert ("ocellaris clownfish", "common", "wikidata") in names

    @pytest.mark.asyncio
    async def test_a_deprecated_aphia_id_never_wins_on_the_write_path(self, no_redis: None):
        """The claim reader is shared, so the statement-rank rule reaches the one path that
        writes a row nobody rewrites - and this is where it is pinned.

        The assertion sits on the entity rather than on the stored `Species` because that is
        where the difference is visible: `resolve_species` takes its identity and its
        taxonomy from the WoRMS record and reads only the entity's qid and its English names.
        A deprecated AphiaID winning here would not corrupt the row's identity, then; it
        would mean this leg had matched on a withdrawn identifier, which is the fact worth
        catching before it grows a consumer.
        """
        claims = {
            "P850": [
                {"rank": "deprecated", "mainsnak": {"datavalue": {"value": "105857"}}},
                {"rank": "normal", "mainsnak": {"datavalue": {"value": "278400"}}},
            ],
            "P225": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "Amphiprion ocellaris"}}}],
        }
        entities = {"entities": {"Q1126155": {"claims": claims}}}
        with _registers(wikidata_lookup=WIKIDATA_LOOKUP, wikidata_entities=entities):
            entity = await species_service._wikidata_by_aphia_id(278400)

        assert entity is not None
        assert (entity.qid, entity.aphia_id) == ("Q1126155", 278400)

    @pytest.mark.asyncio
    async def test_wikidata_refusing_still_stores_the_row_without_a_qid(self, no_redis: None):
        """The 200-with-an-error guard, pinned on the path that still reaches `_wikidata_qids`.

        The Action API reports read-only mode and a busy CirrusSearch backend as **HTTP 200
        carrying an `error` object**, so nothing about the transport says anything went wrong
        and a reader checking only the status code sees a successful, hitless search. Search has
        its own reader for that now; what is left here is resolve's exact-statement lookup,
        where the outcome that matters is narrower and permanent - the row is written either
        way, because WoRMS supplies everything resolve actually needs, and it must be written
        with **no** `wikidata_qid` rather than with whatever could be scraped out of a refusal.
        Rows are immutable, so a qid stored wrongly here is stored wrongly forever.
        """
        db = _empty_db()
        refusal = {"error": {"code": "readonly", "info": "The wiki is read-only."}}
        with _registers(record=CLOWNFISH_RECORD, wikidata_lookup=refusal):
            species = await species_service.resolve_species(db, 278400)

        assert species.aphia_id == 278400
        assert species.scientific_name == "Amphiprion ocellaris"
        assert species.wikidata_qid is None

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


class TestVettingTheStoredName:
    """The synonym list is the one enrichment call resolve cannot do without.

    `_choose_common_name` takes it as a reject list, and it is the only input that can stop a
    junior *scientific* synonym being written as a taxon's display name - "Orca gladiator" for
    the killer whale, which is what this catalog held. Rows are immutable and there is no
    re-resolve, so a name chosen without that list is wrong forever, and the whole class here
    is about the difference between having the list and merely having *something*.
    """

    @pytest.mark.asyncio
    async def test_a_junior_synonym_is_not_stored_as_the_display_name(self, no_redis: None):
        """The end-to-end shape of the defect. Wikidata's label for *Orcinus orca* is the
        binomial, so the prefix test drops it and the first alias wins - and that alias is a
        superseded scientific name under a different genus, which no rule reading only the
        accepted binomial could recognise."""
        db = _empty_db()
        with _registers(
            record=ORCA_RECORD,
            synonyms=ORCA_SYNONYMS,
            wikidata_lookup=ORCA_WIKIDATA_LOOKUP,
            wikidata_entities=ORCA_WIKIDATA_ENTITIES,
        ):
            species = await species_service.resolve_species(db, 137102)

        assert species.common_name == "Orca whale"
        # The synonym is still findable, just not displayable: the index keeps every name.
        added = [call.args[0] for call in db.add.call_args_list]
        names = {(row.name, row.kind) for row in added if isinstance(row, species_service.SpeciesName)}
        assert ("Orca gladiator", "synonym") in names

    @pytest.mark.asyncio
    async def test_a_taxon_with_no_synonyms_is_a_complete_answer(self, no_redis: None):
        """WoRMS answers a synonym-free taxon with the 204 `_request` maps to `[]`, so an empty
        list has to stay distinguishable from the failures below - otherwise every taxon
        without synonyms would 503."""
        db = _empty_db()
        with _registers(record=CLOWNFISH_RECORD, wikidata_lookup=WIKIDATA_LOOKUP, wikidata_entities=WIKIDATA_ENTITIES):
            species = await species_service.resolve_species(db, 278400)

        assert species.common_name == "Ocellaris clownfish"

    @pytest.mark.asyncio
    async def test_a_synonym_list_that_did_not_arrive_is_a_503(self, no_redis: None):
        """The asymmetry with the other two enrichment calls. Losing the entity or the
        vernaculars can only push the choice toward the binomial fallback; losing the synonym
        list can select the *wrong* name and fix it forever, so this one leg refuses rather
        than degrading - the diver retries, which costs a spinner."""
        from fastapi import HTTPException

        db = _empty_db()

        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "AphiaSynonymsByAphiaID" in url:
                raise httpx.ConnectError("dropped")
            if "AphiaRecordByAphiaID" in url:
                return httpx.Response(200, json=ORCA_RECORD)
            if "wikidata" in url:
                return httpx.Response(200, json={"query": {"search": []}})
            return httpx.Response(200, json=[])

        with _Providers(handle), pytest.raises(HTTPException) as raised:
            await species_service.resolve_species(db, 137102)

        assert raised.value.status_code == 503
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_page_failing_mid_walk_fails_the_whole_list(self, no_redis: None):
        """The tempting reading is "keep what arrived" - and a partial list is exactly the
        truncated vet this refuses. Page one is full here, so the walk asks for page two and
        never gets it; fifty vetted names are not a vet."""
        from fastapi import HTTPException

        db = _empty_db()
        first_page = [{"scientificname": f"Orca junior{n:02d}"} for n in range(species_service._WORMS_PAGE_SIZE)]

        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "AphiaSynonymsByAphiaID" in url:
                if "offset=51" in url:
                    raise httpx.ConnectError("dropped")
                return httpx.Response(200, json=first_page)
            if "AphiaRecordByAphiaID" in url:
                return httpx.Response(200, json=ORCA_RECORD)
            if "wikidata" in url:
                return httpx.Response(200, json={"query": {"search": []}})
            return httpx.Response(200, json=[])

        with _Providers(handle), pytest.raises(HTTPException) as raised:
            await species_service.resolve_species(db, 137102)

        assert raised.value.status_code == 503
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_enrichment_budget_expiring_is_a_503_rather_than_a_blind_write(self, no_redis: None):
        """The failure mode a sentinel would have missed. `move_on_after` cancels the leg
        wherever it happens to be, so the slot keeps whatever it was initialised to - which is
        why that initialiser is `None` and not `[]`. A budget expiry and a dropped connection
        have to reach the same refusal, because neither one produced a list."""
        from fastapi import HTTPException

        db = _empty_db()

        async def never_answers(aphia_id: int) -> list[str] | None:
            await anyio.sleep(30)
            return []

        with (
            _registers(record=ORCA_RECORD),
            patch.object(species_service, "_ENRICHMENT_BUDGET_SECONDS", 0.01),
            patch.object(species_service, "_worms_synonyms", never_answers),
            pytest.raises(HTTPException) as raised,
        ):
            await species_service.resolve_species(db, 137102)

        assert raised.value.status_code == 503
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_walk_pages_until_a_short_page(self, no_redis: None):
        """WoRMS pages synonyms at fifty and nothing in a full page says whether there is
        more, so the walk has to ask. There is no page cap - the enrichment budget is the
        bound - and the extra names land in the index as well as in the reject list, which is
        the second, welcome half of paging."""
        db = _empty_db()
        synonyms = [{"scientificname": f"Orca junior{n:02d}"} for n in range(55)]

        with _registers(record=ORCA_RECORD, synonyms=synonyms) as providers:
            species = await species_service.resolve_species(db, 137102)

        offsets = [
            parse_qs(urlparse(url).query)["offset"][0] for url in providers.urls() if "AphiaSynonymsByAphiaID" in url
        ]
        assert offsets == ["1", "51"]

        assert species.aphia_id == 137102
        added = [call.args[0] for call in db.add.call_args_list]
        indexed = {row.name for row in added if isinstance(row, species_service.SpeciesName)}
        assert "Orca junior54" in indexed
        assert len([name for name in indexed if name.startswith("Orca junior")]) == 55

    @pytest.mark.asyncio
    async def test_search_shows_the_unvetted_name_until_a_resolve_fixes_it(self, no_redis: None):
        """The limitation this vet does *not* cover, pinned so that closing it is a decision
        somebody makes rather than a diff nobody notices.

        The reject list is a per-taxon WoRMS call, so a search page would need one per row
        against a keystroke budget, on a path that has already released its read transaction.
        Search therefore still offers "Orca gladiator" while the taxon is uncatalogued, and
        the resolve underneath it stores "Orca whale". Tolerable because it runs in the safe
        direction - the wrong name is never written, and one resolve fixes the row for
        everyone - but real, and this is the assertion that says so out loud."""
        db = _empty_db()
        with _registers(
            wikidata_search=ORCA_WIKIDATA_SEARCH,
            wikidata_entities=ORCA_WIKIDATA_ENTITIES,
        ):
            found = await species_service.search_species(db, "orca")

        assert [r.common_name for r in found.results] == ["Orca gladiator"]

        db = _empty_db()
        with _registers(
            record=ORCA_RECORD,
            synonyms=ORCA_SYNONYMS,
            wikidata_lookup=ORCA_WIKIDATA_LOOKUP,
            wikidata_entities=ORCA_WIKIDATA_ENTITIES,
        ):
            stored = await species_service.resolve_species(db, 137102)

        assert stored.common_name == "Orca whale"


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


# Both bound a *failing* run of `TestConcurrentResolvesDoNotExhaustThePool` and nothing else:
# on a healthy run the burst assembles in milliseconds and the unrelated connection is handed
# over immediately. They sum to well under `species_service._RESOLVE_BUDGET_SECONDS`, which is
# the clock the parked resolves are actually running against.
_BURST_ASSEMBLY_TIMEOUT = 10.0
_UNRELATED_QUERY_TIMEOUT = 5.0


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
        """Measured at a moment this test *creates*, rather than one it hopes to catch.

        The first version sampled `pool.checkedout()` every 100 ms for the length of one
        outbound call and asserted over the middle third of the samples. It flaked on CI with
        `[15, 15, 0, 0, 0]` - `assert 15 < 15`. The sample count was fixed at five by
        construction, so "the middle third" was always the readings at roughly 200 ms and
        300 ms; what moved was the burst. Fifteen connections have to be opened before the
        first resolve can go outbound, and on a loaded runner that ramp-up was still running at
        300 ms, so the window caught resolves still holding their local read. The release was
        working the whole time - the later samples read zero - and the assertion was simply
        looking at the wrong instants. Same lesson as `TestExtractAllSharesOneDecode`, which
        counts scans rather than timing them: **assert on a signal the test controls, never on
        a wall clock it only hopes to line up with.**

        So the register now holds every resolve inside its record fetch until all fifteen have
        arrived there, and nothing is read until they have. What the burst is doing at the
        moment of the assertion is a fact rather than a hope, and the two timeouts below bound
        only how long a *failing* run takes.
        """
        engine = create_async_engine(
            settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI,
            # Size and overflow are `create_async_engine`'s own defaults, spelled out so the
            # numbers below are readable. `pool_timeout` is not - it defaults to 30 s, and
            # waiting that out is the very failure under test, so a run that reintroduces it
            # should say so quickly instead of stalling the suite for half a minute.
            pool_size=5,
            max_overflow=10,
            pool_timeout=_UNRELATED_QUERY_TIMEOUT,
        )
        sessions = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        concurrent = 15  # exactly the pool ceiling: 5 + 10 overflow

        all_outbound = anyio.Event()  # every resolve is now parked in its record fetch
        measured = anyio.Event()  # ...and may go on to finish
        arrived = 0

        def build(**kwargs: Any) -> httpx.AsyncClient:
            async def handle(request: httpx.Request) -> httpx.Response:
                nonlocal arrived
                url = str(request.url)
                if "wikidata" in url:
                    return httpx.Response(200, json={"query": {"search": []}})
                if "AphiaRecordByAphiaID" in url:
                    # Exactly one of these per resolve, because `_record` reports the taxon as
                    # its own accepted id and the synonym branch never fires - which is what
                    # lets the count below be a barrier rather than something that overshoots.
                    arrived += 1
                    if arrived == concurrent:
                        all_outbound.set()
                    await measured.wait()
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

        # `AsyncEngine.pool` is typed as the base `Pool`, which does not declare the checkout
        # counters; the pool actually in use here is a queue pool and does.
        pool = cast(QueuePool, engine.pool)
        checked_out: int | None = None  # None if the burst never assembled and nothing was read
        stalled_at: int | None = None
        unrelated_error: str | None = None

        async def measure() -> None:
            """Read the pool once, with all fifteen resolves held inside their record fetch.

            Every path out of here has to set `measured`, which is why it is a `finally` and
            not a last line - the early return below is the one that would otherwise strand
            fifteen resolves waiting on an event nothing will ever set. An *exception* is the
            safe case rather than the dangerous one: the task group cancels its siblings, so
            the parked resolves unwind instead of hanging.
            """
            nonlocal checked_out, stalled_at, unrelated_error
            try:
                with anyio.move_on_after(_BURST_ASSEMBLY_TIMEOUT) as scope:
                    await all_outbound.wait()
                if scope.cancelled_caught:
                    stalled_at = arrived
                    return
                checked_out = pool.checkedout()
                # The claim itself, not a proxy for it: an unrelated caller wanting a
                # connection *now* is served, rather than waiting out `pool_timeout` and 500ing
                # on an endpoint that has nothing to do with species.
                try:
                    async with sessions() as unrelated:
                        await unrelated.execute(text("SELECT 1"))
                except PoolTimeout as exc:
                    unrelated_error = str(exc)
            finally:
                measured.set()

        try:
            with (
                patch("src.app.services.species_service.httpx.AsyncClient", side_effect=build),
                patch("src.app.services.species_service.enforce_rate_limit", new_callable=AsyncMock),
            ):
                async with anyio.create_task_group() as tasks:
                    for offset in range(concurrent):
                        tasks.start_soon(resolve, offset)
                    tasks.start_soon(measure)
        finally:
            await engine.dispose()

        assert checked_out is not None, (
            f"nothing was measured: only {stalled_at} of {concurrent} resolves reached the "
            f"register within {_BURST_ASSEMBLY_TIMEOUT}s"
        )
        # Zero rather than "fewer than fifteen", which is what the window makes provable: every
        # resolve released its read before going outbound, so nothing at all is checked out.
        assert checked_out == 0, f"{checked_out} connections pinned while every resolve was outbound"
        assert unrelated_error is None, f"an unrelated query could not get a connection: {unrelated_error}"


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
        # Two distinct tokens, so each half of this test reaches one name and only one. With a
        # shared token both names match and `min` picks between them alphabetically, which
        # decides the outcome by spelling rather than by the behaviour under test.
        alias_token = f"zzalias{uuid7().hex[-8:]}"
        alias = f"{alias_token} synonym"
        scientific_name = f"zzfixture-local-sci-{uuid7().hex[-8:]}"
        # **Seeded the way `resolve_species` really writes a row**, which matters for the second
        # half: `_name_rows` always emits a `scientific`-kind name equal to `Species.
        # scientific_name`, so a search for the binomial genuinely does match a `species_name`
        # row and `min(matched_name)` returns it. Without that row the aggregate returns SQL
        # NULL, the Python de-noising block is never reached, and the assertion below passes
        # with that block deleted - which is exactly what the first version of this test did.
        self._seed(db, (alias, "synonym"), (scientific_name, "scientific"), scientific_name=scientific_name)

        # Only the alias carries this token, so it is unambiguously what matched - and it is
        # neither the scientific name nor the common name, so the hint survives.
        by_alias, _ = await species_service._local_search(async_db, alias_token)
        assert by_alias[0].matched_name == alias

        # Matched by the row's own scientific name, which the result already shows - so the
        # hint would be noise, and the de-noising block nulls it.
        by_name, _ = await species_service._local_search(async_db, scientific_name.casefold())
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
