"""Tests for `scripts/build_dive_site_catalog.py`, the hand-run generator behind
`src/app/data/dive_site_catalog.json`.

It is tested despite never running in production because its output is what production reads,
and because two of its rules fail *silently*: a selection filter that deletes real dive sites
still produces a plausible catalog, and a place resolution written to strict point-in-polygon
still resolves every inshore site anyone would think to check by hand.

`test_dive_site_catalog.py` asserts the same invariants from the other side, against the file
that actually shipped. These assert them against inputs the current upstream does not happen
to contain - which is what a refresh could introduce at any time.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_dive_site_catalog.py"


def _load_generator() -> Any:
    """`scripts/` is not a package and is not on the path - it is build-time tooling that
    ships nowhere - so it is loaded by location rather than imported."""
    spec = importlib.util.spec_from_file_location("build_dive_site_catalog", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build = _load_generator()


_EDGE_VERTICES = 20


def _square(code: str | None, name: str, west: float, east: float, south: float, north: float) -> Any:
    """A rectangular boundary whose edges carry intermediate vertices.

    The subdivision is not decoration. `_nearest_within` measures vertex-to-point, which
    over-states the true distance to a polygon by however far apart its vertices are - so a
    bare four-corner rectangle is a much worse model of a coastline than Natural Earth's own
    outlines, and a test built on one would fail at distances the real data resolves fine.
    """
    corners = [(west, south), (east, south), (east, north), (west, north)]
    ring: list[tuple[float, float]] = []
    for index, (start_lon, start_lat) in enumerate(corners):
        end_lon, end_lat = corners[(index + 1) % len(corners)]
        for step in range(_EDGE_VERTICES):
            fraction = step / _EDGE_VERTICES
            ring.append((start_lon + (end_lon - start_lon) * fraction, start_lat + (end_lat - start_lat) * fraction))
    ring.append(ring[0])
    return build._Boundary(
        code=code,
        name=name,
        min_lon=west,
        min_lat=south,
        max_lon=east,
        max_lat=north,
        rings=(tuple(ring),),
    )


class TestTheBusinessExclusion:
    @pytest.mark.parametrize(
        "tags",
        [
            {"amenity": "dive_centre"},
            {"shop": "scuba_diving"},
            {"shop": "sports"},
            {"club": "scuba_diving"},
            {"office": "company"},
            # OSM's `;` multi-value convention. Without splitting on it this reads as neither
            # a restaurant nor a dive centre, and the resort ships as a place to dive.
            {"amenity": "restaurant;dive_centre"},
        ],
    )
    def test_a_business_is_not_a_dive_site(self, tags: dict[str, str]):
        """Submersion's bundled catalog skipped this step, and ships `Dive Otago` and
        `Go Dive Pacific` among the first five records of its site list."""
        assert build._is_business(tags)

    @pytest.mark.parametrize("tags", [{"natural": "reef"}, {"historic": "wreck"}, {"leisure": "divespot"}, {}])
    def test_a_dive_site_is_not_a_business(self, tags: dict[str, str]):
        assert not build._is_business(tags)


class TestTheIndoorExclusion:
    @pytest.mark.parametrize(
        "tags",
        [
            {"leisure": "sports_centre"},
            {"leisure": "swimming_pool"},
            {"leisure": "fitness_centre"},
            {"leisure": "sports_hall"},
            {"building": "yes"},
            {"building": "school"},
            {"amenity": "school"},
            {"amenity": "community_centre"},
            {"amenity": "sport_school"},
            {"amenity": "public_bath"},
        ],
    )
    def test_an_indoor_facility_is_not_a_dive_site(self, tags: dict[str, str]):
        assert build._is_indoor(tags)

    @pytest.mark.parametrize("value", ["pitch", "water_park"])
    def test_the_dutch_convention_survives(self, value: str):
        """**The trap in the whole selection rule, and it cost the research a wrong first
        answer.** `leisure=pitch` is how Zeeland tags a dive site - `Flauwers West`,
        `Goese Sas`, `Noordbout` - and 35 of its 43 named features carry scuba attributes;
        `water_park` is the same story at 6 of 7. A filter that excluded sports-shaped
        `leisure` values wholesale would delete every one of them."""
        assert not build._is_indoor({"leisure": value, "sport": "scuba_diving"})

    def test_a_mapper_saying_it_is_a_divespot_overrides_the_shape(self):
        """A flooded quarry tagged `leisure=sports_centre` because a club runs it is still
        somewhere people dive, and the explicit tag is better evidence than the `leisure`
        value - which is exactly what rescues `Oolderhuuske` and `Berendonk`."""
        assert not build._is_indoor({"leisure": "sports_centre", "scuba_diving:divespot": "yes"})
        assert not build._is_indoor({"building": "yes", "scuba_diving:divespot": "yes"})

    def test_any_other_divespot_value_does_not_rescue(self):
        """`=yes` specifically. The key carries 65 distinct values, some 55 of them site names
        misused as the value, and none of those is a mapper asserting anything."""
        assert build._is_indoor({"leisure": "swimming_pool", "scuba_diving:divespot": "Blue Lagoon"})


class TestSelection:
    def test_a_way_takes_the_centre_overpass_computed(self):
        """`out center;`, never bare `out;`. A way has no position of its own, and the bare
        form is how Submersion's catalog came to carry 356 records with no coordinates at all
        - exactly its 320 ways plus its 36 relations."""
        records = build._osm_records(
            [{"type": "way", "id": 1, "center": {"lat": 1.5, "lon": 2.5}, "tags": {"name": "Wall"}}]
        )

        assert [(record.latitude, record.longitude, record.source_id) for record in records] == [(1.5, 2.5, "way/1")]

    def test_an_element_with_no_position_is_dropped_rather_than_shipped_at_null_island(self):
        assert build._osm_records([{"type": "relation", "id": 2, "tags": {"name": "Reef"}}]) == []

    def test_an_unnamed_feature_is_dropped(self):
        """An unnamed record can only ever produce a blank suggestion."""
        assert build._osm_records([{"type": "node", "id": 3, "lat": 1, "lon": 2, "tags": {"name": "  "}}]) == []

    def test_an_english_name_equal_to_the_local_one_is_not_carried_twice(self):
        records = build._osm_records(
            [{"type": "node", "id": 4, "lat": 1, "lon": 2, "tags": {"name": "Blue Hole", "name:en": "Blue Hole"}}]
        )

        assert records[0].name_en is None

    def test_an_english_name_that_says_something_new_is_kept(self):
        records = build._osm_records(
            [{"type": "node", "id": 5, "lat": 1, "lon": 2, "tags": {"name": "砂辺", "name:en": "Sunabe"}}]
        )

        assert (records[0].name, records[0].name_en) == ("砂辺", "Sunabe")


class TestTheIsoCode:
    def test_natural_earths_missing_code_is_a_string(self):
        """`-99`, not null and not empty - which is why a plain truthiness check passes it
        straight through into the data as if it were a country code."""
        assert build._iso_a2("-99") is None

    @pytest.mark.parametrize("value", [None, "", 0, -99])
    def test_anything_else_missing_is_also_none(self, value: Any):
        assert build._iso_a2(value) is None

    def test_a_real_code_survives(self):
        assert build._iso_a2("EG") == "EG"


class TestPlaceResolution:
    """The largest quiet failure in this generator. Dive sites are in water and administrative
    polygons are land, so only 44.5% of the catalog falls inside any admin-0 polygon - and a
    generator written to containment alone empties the country for the other 55.5% while every
    test naming an inshore site still passes."""

    def test_a_point_inside_a_boundary_takes_it(self):
        egypt = _square("EG", "Egypt", 33.0, 34.0, 27.0, 28.0)

        assert build._resolve([egypt], 33.5, 27.5) is egypt

    def test_an_offshore_point_takes_the_nearest_boundary(self):
        """The rule that carries the majority of this catalog. Of an offshore sample the
        median distance to land is about 3 km."""
        egypt = _square("EG", "Egypt", 33.0, 34.0, 27.0, 28.0)

        assert build._resolve([egypt], 34.1, 27.5) is egypt

    def test_a_point_far_out_to_sea_resolves_to_nothing_and_the_record_still_ships(self):
        """Generator rule 3. A wreck a hundred kilometres out in the Atlantic belongs to no
        administrative area, and 47 real records are in exactly that state."""
        egypt = _square("EG", "Egypt", 33.0, 34.0, 27.0, 28.0)

        assert build._resolve([egypt], 36.0, 27.5) is None

    def test_the_smaller_boundary_wins_a_containment_tie(self):
        """Enclaves. Lesotho is inside South Africa's outline, and the smaller polygon is
        always the more useful answer."""
        big = _square("ZA", "South Africa", 0.0, 10.0, 0.0, 10.0)
        small = _square("LS", "Lesotho", 4.0, 6.0, 4.0, 6.0)

        assert build._resolve(build._smallest_first([big, small]), 5.0, 5.0) is small

    def test_the_region_is_restricted_to_the_resolved_country(self):
        """**The Gulf of Aqaba invariant.** The two layers are separately generalised
        outlines and most records resolve by *nearest*, so run independently they disagree:
        here the closest admin-1 unit of any country is Saudi Arabia's Tabuk, while the
        record's country is Egypt. Unrestricted, this record ships `Tabuk, Egypt` - and
        nothing in the suite or in a live walk that only checks Location is non-empty and in
        English would catch it."""
        admin_0 = [
            _square("EG", "Egypt", 33.0, 34.0, 27.0, 28.0),
            _square("SA", "Saudi Arabia", 35.0, 36.0, 27.0, 28.0),
        ]
        admin_1 = [
            _square("EG", "South Sinai", 33.0, 34.0, 27.0, 28.0),
            _square("SA", "Tabuk", 34.15, 36.0, 27.0, 28.0),
        ]
        record = build._Record(
            name="Wreck", name_en=None, latitude=27.5, longitude=34.1, source="osm", source_id="node/1"
        )

        build._resolve_places([record], admin_0, admin_1)

        assert (record.country, record.region) == ("Egypt", "South Sinai")

    def test_no_region_in_the_resolved_country_is_null_rather_than_a_neighbours(self):
        admin_0 = [_square("EG", "Egypt", 33.0, 34.0, 27.0, 28.0)]
        admin_1 = [_square("SA", "Tabuk", 34.15, 36.0, 27.0, 28.0)]
        record = build._Record(
            name="Wreck", name_en=None, latitude=27.5, longitude=34.1, source="osm", source_id="node/1"
        )

        build._resolve_places([record], admin_0, admin_1)

        assert (record.country, record.region) == ("Egypt", None)

    def test_a_country_with_no_iso_code_resolves_to_a_name_and_no_region(self):
        """Thirteen Natural Earth countries have no `ISO_A2_EH` - Somaliland, Northern Cyprus
        and eleven others. There is no code to restrict admin-1 by, so the region is null
        rather than guessed, and the name is still the useful half."""
        admin_0 = [_square(None, "Somaliland", 43.0, 49.0, 8.0, 11.0)]
        admin_1 = [_square("SO", "Woqooyi Galbeed", 43.0, 49.0, 8.0, 11.0)]
        record = build._Record(
            name="Wreck", name_en=None, latitude=9.5, longitude=45.0, source="osm", source_id="node/1"
        )

        build._resolve_places([record], admin_0, admin_1)

        assert (record.country, record.country_code, record.region) == ("Somaliland", None, None)


class TestTheWikidataPatch:
    def test_a_shared_qid_is_the_same_site(self):
        osm = [build._Record("Blue Hole", None, 28.57, 34.54, "osm", "node/1")]
        wikidata = [build._Record("Blue Hole", None, 28.60, 34.60, "wikidata", "Q123")]

        assert build._deduped(wikidata, osm, {"Q123"}) == []

    def test_two_records_at_the_same_spot_are_the_same_site(self):
        osm = [build._Record("Blue Hole", None, 28.57, 34.54, "osm", "node/1")]
        wikidata = [build._Record("Anything At All", None, 28.5701, 34.5401, "wikidata", "Q123")]

        assert build._deduped(wikidata, osm, set()) == []

    def test_a_kilometre_apart_needs_the_names_to_agree_as_well(self):
        """Deliberately not proximity alone at a kilometre. Two genuinely different sites on
        one reef are routinely that close, and the whole point of this source is the coverage
        it adds - 302 of its 345 records are South African, where OSM has three."""
        osm = [build._Record("Blue Hole", None, 28.57, 34.54, "osm", "node/1")]
        near_same_name = [build._Record("blue hole", None, 28.576, 34.54, "wikidata", "Q1")]
        near_other_name = [build._Record("Bells", None, 28.576, 34.54, "wikidata", "Q2")]

        assert build._deduped(near_same_name, osm, set()) == []
        assert [record.source_id for record in build._deduped(near_other_name, osm, set())] == ["Q2"]

    def test_a_site_nowhere_near_the_osm_set_is_kept(self):
        osm = [build._Record("Blue Hole", None, 28.57, 34.54, "osm", "node/1")]
        wikidata = [build._Record("Maidstone Rock", None, -34.19, 18.46, "wikidata", "Q14213816")]

        assert [record.source_id for record in build._deduped(wikidata, osm, set())] == ["Q14213816"]


class TestTheProvenanceBlock:
    """It is a licence compliance artifact, not documentation: ODbL 4.2 requires the notice to
    travel within the data or its metadata, and 4.6 wants the method of making it reachable."""

    def test_names_all_three_upstreams_and_credits_the_two_that_supply_records(self):
        document = build._document([], "2026-08-31")
        sources = {source["name"]: source for source in document["sources"]}

        assert set(sources) == {"OpenStreetMap", "Wikidata", "Natural Earth"}
        assert sources["OpenStreetMap"]["source"] == "osm"
        assert sources["Wikidata"]["source"] == "wikidata"
        # Natural Earth supplies no record for a per-result credit to hang on, and asks for
        # none. The service drops any record whose source has no `attribution` here, so this
        # absence is what stops "natural-earth" ever becoming a shippable record source.
        assert "source" not in sources["Natural Earth"]
        assert "attribution" not in sources["Natural Earth"]

    def test_records_the_queries_that_would_reproduce_it(self):
        document = build._document([], "2026-08-31")
        sources = {source["name"]: source for source in document["sources"]}

        assert sources["OpenStreetMap"]["query"] == build.OVERPASS_QUERY
        # `out tags center;`, never a bare `out;` - the difference between a way that ships
        # with a position and one that ships with none.
        assert sources["OpenStreetMap"]["query"].endswith("out tags center;")
        assert "Q2141554" in sources["Wikidata"]["query"]

    def test_the_file_is_odbl_whatever_the_repository_is(self):
        """`LICENSE` here is the AGPL, a *software* copyleft that says nothing about a
        vendored database, so the carve-out has to be stated rather than inferred."""
        document = build._document([], "2026-08-31")

        assert document["licence"] == "ODbL 1.0"
        assert "OpenStreetMap contributors" in document["notice"]

    def test_an_unresolved_record_ships_without_its_empty_place_fields(self):
        """Rule 3 records carry no `country` key at all rather than three nulls - and the
        service reads them back as `None` either way."""
        record = build._Record("Deep Wreck", None, 34.5, -74.7, "osm", "node/1")

        emitted = build._document([record], "2026-08-31")["records"][0]

        assert set(emitted) == {"name", "latitude", "longitude", "source", "source_id"}
