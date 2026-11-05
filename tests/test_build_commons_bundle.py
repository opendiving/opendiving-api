"""Tests for `scripts/build_commons_bundle.py`, the hand-run script behind
`src/app/data/commons_dive_sites.geojson`.

It is tested despite never running in production because its output is what production reads,
and because its rules fail *silently*: a place resolution written to strict point-in-polygon
still resolves every inshore site anyone would think to check by hand, and a reader of one
repository shape still writes a plausible file from the other.

`test_commons_bundle.py` asserts the same invariants from the other side, against the file that
actually shipped. These assert them against inputs the tree at that commit does not happen to
contain - which is what the next refresh could introduce at any time.
"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_commons_bundle.py"


def _load_script() -> Any:
    """`scripts/` is not a package and is not on the path - it is build-time tooling that
    ships nowhere - so it is loaded by location rather than imported."""
    spec = importlib.util.spec_from_file_location("build_commons_bundle", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build = _load_script()


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
        record = build._Place(latitude=27.5, longitude=34.1)

        build._resolve_places([record], admin_0, admin_1)

        assert (record.country, record.region) == ("Egypt", "South Sinai")

    def test_no_region_in_the_resolved_country_is_null_rather_than_a_neighbours(self):
        admin_0 = [_square("EG", "Egypt", 33.0, 34.0, 27.0, 28.0)]
        admin_1 = [_square("SA", "Tabuk", 34.15, 36.0, 27.0, 28.0)]
        record = build._Place(latitude=27.5, longitude=34.1)

        build._resolve_places([record], admin_0, admin_1)

        assert (record.country, record.region) == ("Egypt", None)

    def test_a_country_with_no_iso_code_resolves_to_a_name_and_no_region(self):
        """Thirteen Natural Earth countries have no `ISO_A2_EH` - Somaliland, Northern Cyprus
        and eleven others. There is no code to restrict admin-1 by, so the region is null
        rather than guessed, and the name is still the useful half."""
        admin_0 = [_square(None, "Somaliland", 43.0, 49.0, 8.0, 11.0)]
        admin_1 = [_square("SO", "Woqooyi Galbeed", 43.0, 49.0, 8.0, 11.0)]
        record = build._Place(latitude=9.5, longitude=45.0)

        build._resolve_places([record], admin_0, admin_1)

        assert (record.country, record.country_code, record.region) == ("Somaliland", None, None)


def _seed(properties: dict[str, Any] | None = None, **provenance: Any) -> dict[str, Any]:
    """A seed as the tree held it at the commit the first bundle was built from."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [33.920048, 27.814092]},
        "properties": {
            "uuid": "74c091ba-1ec3-5a6b-b70e-fe31c5438605",
            "name": "SS Thistlegorm",
            "external_ids": [{"registry": "openstreetmap", "identifier": "node/255316037"}],
            "extensions": {
                "opendivingcommons": {
                    "source": "openstreetmap",
                    "license": "ODbL-1.0",
                    "retrieved": "2026-10-17",
                    **provenance,
                }
            },
            **(properties or {}),
        },
    }


_PATH = "sites/openstreetmap/node/255316037.geojson"


class TestTheOlderShape:
    def test_an_english_name_moves_into_the_other_names(self) -> None:
        record = build.bundled(_PATH, _seed(name_en="Thistlegorm"))

        assert record["properties"]["other_names"] == ["Thistlegorm"]
        assert "name_en" not in record["properties"]["extensions"]["opendivingcommons"]

    @pytest.mark.parametrize("name_en", ["SS Thistlegorm", " ss thistlegorm "])
    def test_an_english_name_repeating_the_name_is_dropped_not_moved(self, name_en: str) -> None:
        assert "other_names" not in build.bundled(_PATH, _seed(name_en=name_en))["properties"]

    def test_an_english_name_repeating_another_name_is_dropped_too(self) -> None:
        record = build.bundled(_PATH, _seed({"other_names": ["Thistlegorm"]}, name_en="THISTLEGORM"))

        assert record["properties"]["other_names"] == ["Thistlegorm"]

    def test_a_seed_s_entry_is_read_off_its_path(self) -> None:
        provenance = build.bundled(_PATH, _seed())["properties"]["extensions"]["opendivingcommons"]

        assert provenance["seeded_from"] == {"registry": "openstreetmap", "identifier": "node/255316037"}

    def test_a_wikidata_seed_s_identifier_has_no_slash_of_its_own(self) -> None:
        assert build._seed_entry("sites/wikidata/Q14213816.geojson") == {
            "registry": "wikidata",
            "identifier": "Q14213816",
        }


class TestTheShapeTheRepositoryPublishes:
    def test_it_reads_through_unchanged(self) -> None:
        entry = {"registry": "openstreetmap", "identifier": "node/255316037"}
        feature = _seed({"other_names": ["Thistlegorm"], "location": {"name": "South Sinai, Egypt"}}, seeded_from=entry)

        assert build.bundled(_PATH, feature) == feature

    def test_the_entry_comes_from_the_provenance_and_not_the_path(self) -> None:
        entry = {"registry": "openstreetmap", "identifier": "node/1"}

        record = build.bundled(_PATH, _seed(seeded_from=entry))

        assert record["properties"]["extensions"]["opendivingcommons"]["seeded_from"] == entry

    def test_a_contribution_has_no_entry_and_no_date(self) -> None:
        feature = _seed()
        feature["properties"]["extensions"]["opendivingcommons"] = {"source": "contribution", "license": "CC0-1.0"}

        record = build.bundled("sites/opendivingcommons/74c091ba-1ec3-5a6b-b70e-fe31c5438605.geojson", feature)

        assert record["properties"]["extensions"]["opendivingcommons"] == {
            "source": "contribution",
            "license": "CC0-1.0",
        }

    def test_a_locality_keeps_its_name_alone(self) -> None:
        feature = _seed({"location": {"name": "Dahab, Egypt", "latitude": 28.5, "longitude": 34.5}})

        assert build.bundled(_PATH, feature)["properties"]["location"] == {"name": "Dahab, Egypt"}


class TestTheLocality:
    def test_the_finer_name_comes_first(self) -> None:
        assert build.locality_name(build._Place(0, 0, country="Egypt", region="South Sinai")) == "South Sinai, Egypt"

    def test_a_country_alone_is_a_locality(self) -> None:
        assert build.locality_name(build._Place(0, 0, country="Egypt")) == "Egypt"

    def test_neither_is_none(self) -> None:
        assert build.locality_name(build._Place(0, 0)) is None

    def test_a_record_already_placed_is_not_placed_again(self) -> None:
        placed = build.bundled(_PATH, _seed({"location": {"name": "Somewhere"}}))
        egypt = _square("EG", "Egypt", 30.0, 35.0, 25.0, 30.0)

        build._place_records([placed], [egypt], [])

        assert placed["properties"]["location"] == {"name": "Somewhere"}

    def test_a_record_far_out_to_sea_ships_without_one(self) -> None:
        record = build.bundled(_PATH, _seed())

        build._place_records([record], [_square("FR", "France", 1.0, 2.0, 45.0, 46.0)], [])

        assert "location" not in record["properties"]


class TestTheNoticeBlock:
    @pytest.fixture
    def notice(self) -> dict[str, Any]:
        block: dict[str, Any] = build.notice_block("f96a9ed313d5b545a8030456a3a592b940ec6452", "2026-11-05", 1)
        return block

    def test_it_names_the_commit_it_was_built_from(self, notice: dict[str, Any]) -> None:
        assert notice["commit"] == "f96a9ed313d5b545a8030456a3a592b940ec6452"

    def test_it_credits_every_source_a_record_can_have(self, notice: dict[str, Any]) -> None:
        credited = {source["source"] for source in notice["sources"] if source.get("attribution")}

        assert credited == {"openstreetmap", "wikidata", "contribution"}

    def test_natural_earth_is_named_and_credits_no_record(self, notice: dict[str, Any]) -> None:
        natural_earth = next(source for source in notice["sources"] if source["name"] == "Natural Earth")

        assert "source" not in natural_earth and "attribution" not in natural_earth

    def test_the_file_is_odbl_whatever_the_repository_is(self, notice: dict[str, Any]) -> None:
        assert notice["licence"] == "ODbL 1.0"
        assert "Derivative Database of OpenStreetMap" in notice["notice"]
        assert "osm.org/copyright" in notice["notice"]

    def test_the_text_is_one_json_document_with_a_feature_per_line(self, notice: dict[str, Any]) -> None:
        text = build.serialised(notice, [_seed(), _seed()])

        document = json.loads(text)
        assert document["type"] == "FeatureCollection"
        assert document["features"] == [_seed(), _seed()]
        assert len(text.splitlines()) == 4
