"""Tests for the dive-site suggestion catalog - the vendored file, the service that searches
it (`services/dive_site_catalog.py`) and the route on top (`GET /dive-sites/suggest`).

Most of the coordinates and names below are real records read out of `src/app/data/`, so
these are as much a check on the data as on the arithmetic: a refresh that drops a source,
loses the country resolution or renames a field fails here rather than in a diver's form.

Two themes are not conveniences. **Degradation** - a truncated or missing data file must
produce "no suggestion" rather than a 500 out of the dive-site form, exactly as
`test_marine_areas.py` requires of the sea polygons. And **attribution** - the file is an
ODbL Derivative Database, so a record that reaches a client without the credit its source's
licence requires is a licence breach, not a cosmetic defect.
"""

import json
import re
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.setup import create_application
from src.app.services import dive_site_catalog
from src.app.services.dive_site_catalog import SUGGESTION_LIMIT, CatalogSite, search_sites

CURRENT_USER = {"id": 7, "username": "ada", "is_superuser": False}

_REAL_DATA = dive_site_catalog._DATA_PATH

# What the generator actually produced, on the run that is checked in. Pinned in three places
# - here, in `services/dive_site_catalog.py`'s docstring and in `DECISIONS.md` - following
# `marine_areas`' 293. See `TestTheVendoredFile` for why this assertion is the whole refresh
# policy rather than a trivia check.
CATALOG_RECORDS = 3702

OSM_ATTRIBUTION = "[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)"

# An ISO 3166 code, alpha-2 or a subdivision of one: `EG`, `EG-JS`, `MY-08`. Matching the
# *shape* rather than counting characters, because a length rule cannot separate the two
# things that matter here. `EG-JS` is five characters, so "longer than three" would wave a
# subdivision code straight through; and the catalog carries seven genuine three-character
# region names - `Ain`, `Goa`, `Lot`, `Osh`, `Uri`, `Var`, `Zug` - so the same rule fails a
# legitimate refresh the moment one of them lands in a result. Nothing in the shipped file
# matches this pattern, and nothing should: these are display names.
_ISO_CODE = re.compile(r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")


def _site(
    name: str,
    latitude: float = 0.0,
    longitude: float = 0.0,
    *,
    name_en: str | None = None,
    country: str | None = None,
    region: str | None = None,
    source: str = "osm",
) -> CatalogSite:
    return CatalogSite(
        name=name,
        name_en=name_en,
        latitude=latitude,
        longitude=longitude,
        country_code="XX",
        country=country,
        region=region,
        source=source,
        source_id=f"node/{abs(hash(name)) % 10**8}",
        attribution=OSM_ATTRIBUTION,
        _folded=tuple(value.casefold() for value in (name, name_en) if value),
    )


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Swap the loaded catalog for a synthetic one.

    Patches `_sites` rather than the file, so the ranking tests state the geometry they mean
    instead of depending on which real sites happen to share a name this month.
    """

    def load(*sites: CatalogSite) -> None:
        monkeypatch.setattr(dive_site_catalog, "_sites", lambda: tuple(sites))

    return load


class TestMatching:
    def test_finds_a_substring_case_insensitively(self, catalog: Any):
        """A diver looking for the Thistlegorm types "thistlegorm"; the record is called
        "SS Thistlegorm". Prefix matching alone would find neither."""
        catalog(_site("SS Thistlegorm"))

        results, _ = search_sites("thistlegorm")

        assert [site.name for site in results] == ["SS Thistlegorm"]

    def test_the_english_name_is_searched_too(self, catalog: Any):
        """The whole reason `name_en` is carried: dozens of the catalog's names contain no Latin
        letter at all and are otherwise unreachable from a Latin keyboard."""
        catalog(_site("砂辺", name_en="Sunabe"))

        results, _ = search_sites("sunabe")

        assert [site.name for site in results] == ["砂辺"]

    def test_the_local_name_still_matches(self, catalog: Any):
        catalog(_site("砂辺", name_en="Sunabe"))

        assert [site.name for site in search_sites("砂辺")[0]] == ["砂辺"]

    def test_no_match_is_an_empty_answer_rather_than_everything(self, catalog: Any):
        catalog(_site("Blue Hole"), _site("Shark Point"))

        assert search_sites("nowhere at all") == ([], False)

    def test_a_blank_query_matches_nothing(self, catalog: Any):
        """The route's `min_length=2` makes this unreachable over HTTP, but a substring
        search for the empty string matches every row, so the service refuses it itself
        rather than relying on a caller it does not control."""
        catalog(_site("Blue Hole"))

        assert search_sites("   ") == ([], False)


class TestRanking:
    def test_without_a_position_an_exact_name_beats_a_prefix_beats_a_substring(self, catalog: Any):
        catalog(_site("Blue Hole Annex"), _site("The Blue Hole"), _site("Blue Hole"))

        results, _ = search_sites("blue hole")

        assert [site.name for site in results] == ["Blue Hole", "Blue Hole Annex", "The Blue Hole"]

    def test_with_a_position_the_nearest_wins(self, catalog: Any):
        """The scenario this parameter exists for, and the one thing that separates a
        same-name cluster: five `Shark Point` records in four countries are identical to
        every other ranking signal there is."""
        catalog(
            _site("Shark Point", latitude=5.0, longitude=100.0),
            _site("Shark Point", latitude=-8.0, longitude=116.0),
            _site("Shark Point", latitude=4.0, longitude=103.0),
        )

        results, _ = search_sites("shark point", latitude=4.1, longitude=103.1)

        assert [(site.latitude, site.longitude) for site in results][0] == (4.0, 103.0)

    def test_a_position_outranks_match_quality(self, catalog: Any):
        """Deliberate, and worth pinning because the opposite is just as defensible. A diver
        who dropped a pin before searching is asking about where they are, so the near
        substring match comes above the far exact one."""
        catalog(
            _site("Blue Hole", latitude=50.0, longitude=0.0),
            _site("Blue Hole Annex", latitude=0.1, longitude=0.1),
        )

        results, _ = search_sites("blue hole", latitude=0.0, longitude=0.0)

        assert [site.name for site in results] == ["Blue Hole Annex", "Blue Hole"]

    def test_half_a_position_is_ignored_rather_than_half_applied(self, catalog: Any):
        """The route rejects a half pair with a 422, so this only says what the service does
        if one ever reaches it: rank by match quality, never by a latitude against a
        longitude of zero - which would sort every result by its distance from Null Island."""
        catalog(
            _site("Blue Hole", latitude=50.0, longitude=0.0),
            _site("Blue Hole Annex", latitude=0.1, longitude=0.1),
        )

        results, _ = search_sites("blue hole", latitude=0.0, longitude=None)

        assert [site.name for site in results] == ["Blue Hole", "Blue Hole Annex"]

    def test_identical_rows_stay_in_file_order_rather_than_raising(self, catalog: Any):
        """Seven `Diving Spot` records sit within four kilometres of each other in one
        Indonesian bay, sharing a name, a country and a region. Searched with no position
        they tie on every sort key, and a sort that fell through to comparing the records
        themselves would raise - `CatalogSite` is not orderable."""
        catalog(*[_site("Diving Spot", latitude=-5.0 + index / 100, longitude=106.0) for index in range(7)])

        results, has_more = search_sites("diving spot")

        assert [site.latitude for site in results] == [-5.0 + index / 100 for index in range(7)]
        assert has_more is False


class TestTheCap:
    def test_caps_the_answer_and_says_it_was_cut(self, catalog: Any):
        catalog(*[_site(f"Reef {index}") for index in range(SUGGESTION_LIMIT + 5)])

        results, has_more = search_sites("reef")

        assert len(results) == SUGGESTION_LIMIT
        assert has_more is True

    def test_exactly_the_cap_is_not_more(self, catalog: Any):
        """The off-by-one that would render "keep typing to narrow" under a complete answer."""
        catalog(*[_site(f"Reef {index}") for index in range(SUGGESTION_LIMIT)])

        results, has_more = search_sites("reef")

        assert len(results) == SUGGESTION_LIMIT
        assert has_more is False


class TestDegradation:
    """Reading the file is the only thing in this module that touches the world, and the
    dive-site form it hangs off promises a suggestion or nothing. A truncated data file must
    not turn that form into a 500."""

    @pytest.fixture(autouse=True)
    def unloaded(self) -> Generator[None]:
        """`_sites` remembers a successful read, so a test that swaps the path has to reset
        it on both sides - or it reads the real file instead of the broken one."""
        dive_site_catalog._loaded = None
        yield
        dive_site_catalog._loaded = None

    @pytest.mark.parametrize(
        "contents",
        ["", "{ truncated", '{"records": []}', '{"sources": [], "records": [{"name": "x"}]}'],
    )
    def test_an_unusable_file_means_no_suggestions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str
    ):
        broken = tmp_path / "dive_site_catalog.json"
        broken.write_text(contents, encoding="utf-8")
        monkeypatch.setattr(dive_site_catalog, "_DATA_PATH", broken)

        assert search_sites("thistlegorm") == ([], False)

    def test_a_missing_file_means_no_suggestions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(dive_site_catalog, "_DATA_PATH", tmp_path / "not-here.json")

        assert search_sites("thistlegorm") == ([], False)

    def test_a_repaired_file_recovers_without_a_restart(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Only a successful read is remembered. Memoizing the failure would leave one bad
        read to disable the picker for the life of the process, on evidence no stronger than
        a single `OSError` - and a mid-regeneration read is exactly what that looks like."""
        path = tmp_path / "dive_site_catalog.json"
        monkeypatch.setattr(dive_site_catalog, "_DATA_PATH", path)
        assert search_sites("thistlegorm") == ([], False)

        path.write_text(_REAL_DATA.read_text(encoding="utf-8"), encoding="utf-8")

        results, _ = search_sites("thistlegorm")
        assert [site.name for site in results] == ["SS Thistlegorm"]

    def test_a_record_with_no_credit_is_dropped_rather_than_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The one drop rule in the loader, and it is a licence rule rather than a parsing
        one. Every record travels with the attribution its source requires; a record naming a
        source the provenance block does not credit has none, and serving it would be exactly
        the ODbL 4.2 breach that block exists to prevent."""
        path = tmp_path / "dive_site_catalog.json"
        path.write_text(
            json.dumps(
                {
                    "sources": [{"source": "osm", "attribution": OSM_ATTRIBUTION}],
                    "records": [
                        {
                            "name": "Credited Reef",
                            "latitude": 1.0,
                            "longitude": 2.0,
                            "source": "osm",
                            "source_id": "node/1",
                        },
                        {
                            "name": "Uncredited Reef",
                            "latitude": 1.0,
                            "longitude": 2.0,
                            "source": "somewhere-else",
                            "source_id": "x/1",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(dive_site_catalog, "_DATA_PATH", path)

        results, _ = search_sites("reef")

        assert [site.name for site in results] == ["Credited Reef"]


class TestTheVendoredFile:
    def test_records_where_every_field_came_from(self):
        """The provenance block is a licence compliance artifact, not documentation: ODbL 4.2
        requires the notice to travel "within the data or metadata", and OSMF's attribution
        guidelines say the same for databases specifically. It also has to name all three
        upstreams, because the block's job is to say where every *field* came from - and
        Natural Earth supplies the country and region on nearly every row."""
        document = json.loads(_REAL_DATA.read_text(encoding="utf-8"))

        assert document["licence"] == "ODbL 1.0"
        assert "opendatacommons.org/licenses/odbl" in document["licence_url"]
        assert "OpenStreetMap contributors" in document["notice"]
        assert "osm.org/copyright" in document["notice"]
        assert document["generated"]
        assert {source["name"] for source in document["sources"]} == {
            "OpenStreetMap",
            "Wikidata",
            "Natural Earth",
        }
        assert all(source["retrieved"] and source["licence"] for source in document["sources"])

    def test_the_generator_is_named_so_a_refresh_is_possible(self):
        """ODbL 4.6 wants recipients to be able to obtain the derivative database *or the
        method of making it*; checking in the generator is how that is satisfied, and naming
        it inside the file is what makes it findable."""
        document = json.loads(_REAL_DATA.read_text(encoding="utf-8"))

        assert document["generated_by"] == "scripts/build_dive_site_catalog.py"
        assert (Path(__file__).resolve().parent.parent / document["generated_by"]).exists()

    def test_holds_the_catalog_it_is_documented_to_hold(self):
        """The assertion that makes a refresh a decision rather than a diff to accept, and
        the only tripwire this file has. Everything upstream drops quietly by design -
        businesses, indoor pools, unnamed features, Wikidata rows that deduped away - so a
        source that changed shape could shed hundreds of records and leave every other test
        here passing on the handful of names they mention.

        This number is quoted in `services/dive_site_catalog.py` and in `DECISIONS.md`. If a
        refresh moves it, move all three and say in the PR what moved."""
        document = json.loads(_REAL_DATA.read_text(encoding="utf-8"))

        assert document["count"] == CATALOG_RECORDS
        assert len(document["records"]) == CATALOG_RECORDS
        assert len(dive_site_catalog._sites()) == CATALOG_RECORDS

    def test_every_record_is_usable(self):
        """Four things a record cannot be shipped without, and Submersion's bundled catalog
        gets one of them wrong: 356 of its 3,612 rows carry no coordinates at all, because it
        ran Overpass `out;` rather than `out center;`."""
        for site in dive_site_catalog._sites():
            assert site.name.strip()
            assert -90 <= site.latitude <= 90
            assert -180 <= site.longitude <= 180
            assert site.source_id
            assert site.attribution

    def test_both_sources_are_present_and_credited_differently(self):
        """Wikidata is in the catalog as a regional patch rather than decoration - OSM has
        three named dive sites in the whole of South Africa - and CC0 is not ODbL, so the two
        cannot share a credit string."""
        sites = dive_site_catalog._sites()
        by_source = {site.source for site in sites}

        assert by_source == {"osm", "wikidata"}
        assert len({site.attribution for site in sites}) == 2

    def test_the_openstreetmap_credit_matches_the_geocoder_byte_for_byte(self):
        """Not a coincidence and not free to change. The site form renders catalog hits and
        geocoder hits in one list under one credit line that collapses repeats by exact
        string, so a second wording for the same licence shows a diver the same credit
        twice."""
        from src.app.services.geocoding_service import _DEFAULT_ATTRIBUTION

        osm = {site.attribution for site in dive_site_catalog._sites() if site.source == "osm"}

        assert osm == {_DEFAULT_ATTRIBUTION}

    def test_the_place_resolution_actually_ran(self):
        """The failure this catches is the one the suite is otherwise blind to. Dive sites are
        in water and administrative polygons are land, so a generator written to strict
        point-in-polygon resolves only 44.5% of the catalog and leaves the country empty for
        the rest - while every test naming an inshore site still passes. Containment plus
        nearest-within-50 km is what takes it to nearly all of them."""
        sites = dive_site_catalog._sites()
        resolved = sum(1 for site in sites if site.country)

        assert resolved / len(sites) > 0.9

    def test_some_records_resolved_to_nothing_and_shipped_anyway(self):
        """Generator rule 3 is a live path, not a corner: a wreck a hundred kilometres out in
        the Atlantic belongs to no administrative area, and a site with no country is still a
        site. A client that assumed `country` was always set would be wrong on real rows."""
        assert any(site.country is None for site in dive_site_catalog._sites())

    def test_the_region_never_comes_from_a_different_country(self):
        """The Gulf of Aqaba failure. Admin-0 and admin-1 are separately generalised outlines
        and most records resolve by *nearest* rather than containment, so resolved
        independently a record can take its country from Egypt and its region from Saudi
        Arabia's Tabuk. A region with no country is the shape that mistake would leave
        behind."""
        assert not [site for site in dive_site_catalog._sites() if site.region and not site.country]

    def test_the_place_names_are_the_english_ones(self):
        """Admin-1 `name` and `name_en` differ on 1,265 of Natural Earth's 4,596 features.
        Reading `name` would ship `Janub Sina', Egypt` where a diver expects `South Sinai,
        Egypt`, reproducing on the region half exactly the local-language defect this catalog
        avoids on the country half."""
        thistlegorm = next(site for site in dive_site_catalog._sites() if site.source_id == "node/255316037")

        assert (thistlegorm.region, thistlegorm.country) == ("South Sinai", "Egypt")

    def test_france_and_norway_kept_their_country_code(self):
        """Natural Earth writes the *string* `-99` into `ISO_A2` for 22 of its 258 countries
        and into `ISO_A2_EH` for 13, and the 13 are a strict subset - so a generator reading
        `ISO_A2` silently strips the country from every French and Norwegian dive site while
        looking like it worked. These are the two biggest diving nations among the nine that
        repairs."""
        codes = {site.country: site.country_code for site in dive_site_catalog._sites() if site.country}

        assert codes.get("France") == "FR"
        assert codes.get("Norway") == "NO"

    def test_no_display_name_anywhere_in_the_file_is_really_a_code(self):
        """Over the whole file rather than the ten records a query returns, which is the
        difference between a guarantee and a coincidence: the route-level check can only see
        whatever matched, so on its own it would pass by luck. Two ways a code reaches a
        diver - the resolver writing `country_code` into `country`, or Natural Earth handing
        back a subdivision code as a region name - and both look like a working feature until
        somebody reads `EG` in a Location field."""
        for site in dive_site_catalog._sites():
            for value in (site.country, site.region):
                assert value is None or not _ISO_CODE.match(value), value
                # Guarded on the code rather than the value: the unresolved records carry
                # `None` in all three fields, and an unguarded `!=` reads that as a match.
                assert site.country_code is None or value != site.country_code, value

    def test_the_malaysian_shark_points_come_apart_on_region(self):
        """The whole argument for carrying `region` rather than country alone. Five `Shark
        Point` records resolve to four countries; the two Malaysian ones are ~300 km apart,
        Langkawi on the west coast and Perhentian on the east, and a country-only hint would
        present them as the same place."""
        shark_points = [site for site in dive_site_catalog._sites() if site.name == "Shark Point"]
        malaysian = {site.region for site in shark_points if site.country == "Malaysia"}

        assert len(malaysian) == 2
        assert None not in malaysian


@pytest.fixture(scope="module")
def catalog_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`, like `test_geocoding.py` - nothing
    below this route touches a database."""
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def client(catalog_app: Any) -> Generator[TestClient]:
    catalog_app.dependency_overrides[get_current_user] = lambda: CURRENT_USER
    with TestClient(catalog_app) as test_client:
        yield test_client
    catalog_app.dependency_overrides = {}


@pytest.fixture
def anonymous_client(catalog_app: Any) -> Generator[TestClient]:
    with TestClient(catalog_app) as test_client:
        yield test_client
    catalog_app.dependency_overrides = {}


class TestTheEndpoint:
    def test_requires_authentication(self, anonymous_client: TestClient):
        """Like every other `/api/v1` route, and for the reason both comparable global-data
        endpoints authenticate despite the answer not depending on who is asking.
        `test_route_authentication.py` enforces this structurally; this asserts it over the
        wire."""
        assert anonymous_client.get("/api/v1/dive-sites/suggest", params={"q": "blue"}).status_code == 401

    def test_returns_exactly_the_documented_fields(self, client: TestClient):
        """**The wire contract**, and it is enumerated rather than sampled because the web
        client builds its whole pick handler from this shape: it writes Location from
        `region`/`country`, renders a hint, mints a namespaced id from `source`/`source_id`
        and fills Name from `name`.

        Two absences are load-bearing. There is no `country_code` - the catalog carries one
        internally, and putting an ISO code on the wire invites it into a place's `name`, whose
        own example is `Dahab, Egypt`. And there is no distance, because the web tier
        already formats one against the diver's unit preference and a pre-formatted or
        metric-only field on the wire would silently break that."""
        body = client.get("/api/v1/dive-sites/suggest", params={"q": "thistlegorm"}).json()

        assert set(body) == {"results", "has_more"}
        assert body["results"], "the checked-in catalog should contain SS Thistlegorm"
        assert set(body["results"][0]) == {
            "name",
            "name_en",
            "latitude",
            "longitude",
            "country",
            "region",
            "source",
            "source_id",
            "attribution",
        }

    def test_answers_with_a_real_site(self, client: TestClient):
        body = client.get("/api/v1/dive-sites/suggest", params={"q": "thistlegorm"}).json()
        first = body["results"][0]

        assert first["name"] == "SS Thistlegorm"
        assert (first["region"], first["country"]) == ("South Sinai", "Egypt")
        assert first["source"] == "osm"
        assert first["attribution"] == OSM_ATTRIBUTION
        assert body["has_more"] is False

    def test_the_stable_key_is_not_on_the_wire_under_any_name(self, client: TestClient):
        """`country_code` is absent as a field, and no place field is carrying one instead."""
        body = client.get("/api/v1/dive-sites/suggest", params={"q": "reef"}).json()

        assert body["results"]
        for result in body["results"]:
            assert "country_code" not in result
            for field in ("country", "region"):
                assert result[field] is None or not _ISO_CODE.match(result[field])

    def test_a_short_query_is_refused_rather_than_matching_everything(self, client: TestClient):
        assert client.get("/api/v1/dive-sites/suggest", params={"q": "b"}).status_code == 422
        assert client.get("/api/v1/dive-sites/suggest", params={"q": ""}).status_code == 422

    def test_a_position_is_sent_whole_or_not_at_all(self, client: TestClient):
        """The same rule `WholeCoordinatePair` puts on the write schemas. A client that sent a
        latitude alone believes it asked for proximity ranking; answering it by match quality
        instead is the kind of wrongness nobody notices, so it is a 422."""
        assert client.get("/api/v1/dive-sites/suggest", params={"q": "shark", "latitude": 4.1}).status_code == 422
        assert client.get("/api/v1/dive-sites/suggest", params={"q": "shark", "longitude": 103.1}).status_code == 422
        assert (
            client.get(
                "/api/v1/dive-sites/suggest", params={"q": "shark", "latitude": 4.1, "longitude": 103.1}
            ).status_code
            == 200
        )

    def test_a_position_changes_the_order(self, client: TestClient):
        """Scenario 10 from the other side, against the real catalog: the two query
        parameters have to reach the ranking rather than being accepted and dropped."""
        near_perhentian = client.get(
            "/api/v1/dive-sites/suggest", params={"q": "shark point", "latitude": 5.9, "longitude": 102.7}
        ).json()["results"]
        near_maldives = client.get(
            "/api/v1/dive-sites/suggest", params={"q": "shark point", "latitude": 4.2, "longitude": 73.5}
        ).json()["results"]

        assert near_perhentian[0]["country"] == "Malaysia"
        assert near_maldives[0]["country"] == "Maldives"

    def test_a_broken_catalog_is_an_empty_answer_rather_than_a_500(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The degradation promise at the layer a diver actually meets it. `_loaded` is reset
        on both sides, or this reads the real file instead of the broken one."""
        monkeypatch.setattr(dive_site_catalog, "_loaded", None)
        monkeypatch.setattr(dive_site_catalog, "_DATA_PATH", tmp_path / "not-here.json")
        try:
            response = client.get("/api/v1/dive-sites/suggest", params={"q": "thistlegorm"})
        finally:
            dive_site_catalog._loaded = None

        assert response.status_code == 200
        assert response.json() == {"results": [], "has_more": False}
