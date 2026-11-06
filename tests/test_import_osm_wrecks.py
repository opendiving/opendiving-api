"""Tests for `scripts/import_osm_wrecks.py`, whose floors decide what enters a pool nothing is ever
removed from. They read an Overpass answer written out here, never the network."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_osm_wrecks.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("import_osm_wrecks", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wrecks = _load_script()


def _element(
    identifier: int = 1, kind: str = "node", tags: dict[str, str] | None = None, **more: str
) -> dict[str, Any]:
    position: dict[str, Any] = {"lat": 8.7404, "lon": 167.7109}
    if kind != "node":
        position = {"center": position}
    return {
        "type": kind,
        "id": identifier,
        **position,
        "tags": {"historic": "wreck", "name": "Moltke", "wreck:depth": "30", **(tags or {}), **more},
    }


def _response(*elements: dict[str, Any]) -> dict[str, Any]:
    return {"osm3s": {"timestamp_osm_base": "2026-11-06T17:03:35Z"}, "elements": list(elements)}


def _kept(*elements: dict[str, Any], pool: set[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = wrecks.candidates(_response(*elements), pool or set())[0]
    return kept


class TestTheName:
    @pytest.mark.parametrize(
        "name",
        [
            "Barco hundido",
            "Wrecked schooner",
            "Ship Graveyard",
            "Plane Wreckage #3",
            "1954",
            "Wreck",
            "Épave",
            "Затонувшая баржа",
            "The Barge",
            "Unknown 1923",
            "Lastwagen-Wrack",
            "Tug II",
            "#",
        ],
    )
    def test_one_that_says_only_what_it_is_is_out(self, name: str) -> None:
        assert not wrecks.is_a_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "SS Kennedy",
            "Saudi Golden Arrow",
            "Wreck of the Resurgam II",
            "ЭНС-73",
            "Moltke",
            "エモンズ",
            "LCU-22",
            "Eller Island Wreck #1",
        ],
    )
    def test_one_with_a_word_of_its_own_is_in(self, name: str) -> None:
        assert wrecks.is_a_name(name)


class TestTheDepth:
    @pytest.mark.parametrize(
        ("tags", "depths"),
        [
            ({"wreck:depth": "30"}, (30.0, 30.0)),
            ({"depth": "12.2"}, (12.2, 12.2)),
            ({"wreck:depth": "15 m"}, (15.0, 15.0)),
            ({"wreck:depth": "8 metres"}, (8.0, 8.0)),
            ({"wreck:depth": "100 ft"}, (30.5, 30.5)),
            ({"depth": "1-25m"}, (1.0, 25.0)),
            ({"depth": "28 - 40 m"}, (28.0, 40.0)),
            ({"depth": "10'-80'"}, (3.0, 24.4)),
            ({"wreck:min_depth": "27", "wreck:max_depth": "42"}, (27.0, 42.0)),
            ({"scuba_diving:depth": "25", "scuba_diving:maxdepth": "28"}, (25.0, 28.0)),
            ({"maxdepth": "9", "mindepth": "3"}, (3.0, 9.0)),
            ({"seamark:wreck:depth": "11.52"}, (11.5, 11.5)),
            ({"depth": "about thirty", "wreck:depth": "30m"}, (30.0, 30.0)),
        ],
    )
    def test_what_is_read_as_one(self, tags: dict[str, str], depths: tuple[float, float]) -> None:
        assert wrecks.depth_of(tags) == depths

    @pytest.mark.parametrize(
        "tags",
        [
            {},
            {"ele": "-30"},
            {"depth": "-3.4"},
            {"depth": "above sea level"},
            {"depth": "92m;64 m"},
            {"depth": "30 fathoms"},
        ],
        ids=["none", "ele", "negative", "free-text", "two-values", "another-unit"],
    )
    def test_what_is_not(self, tags: dict[str, str]) -> None:
        assert wrecks.depth_of(tags) is None

    @pytest.mark.parametrize(
        ("value", "passes"),
        [
            ("0m", False),
            ("2.9", False),
            ("3", True),
            ("9 ft", False),
            ("10 ft", True),
            ("60", True),
            ("60.1", False),
            ("196 ft", True),
            ("200 ft", False),
        ],
    )
    def test_the_deepest_is_at_least_3_m_and_at_most_60_m(self, value: str, passes: bool) -> None:
        assert bool(_kept(_element(tags={"wreck:depth": value}))) is passes

    @pytest.mark.parametrize(("value", "passes"), [("40-80", False), ("1-2", False), ("1-25", True)])
    def test_a_range_is_judged_by_its_deepest_end(self, value: str, passes: bool) -> None:
        assert bool(_kept(_element(tags={"wreck:depth": value}))) is passes


class TestWhatShowsOrIsClosed:
    @pytest.mark.parametrize(
        ("key", "value", "passes"),
        [
            ("seamark:wreck:category", "hull_showing", False),
            ("seamark:wreck:category", "mast_showing", False),
            ("seamark:wreck:category", "hull_visible", False),
            ("seamark:wreck:category", "hull_visible,_non-dangerous", False),
            ("seamark:wreck:category", "non-dangerous", True),
            ("seamark:wreck:category", "dangerous", True),
            ("seamark:wreck:water_level", "submerged", True),
            ("seamark:wreck:water_level", "always_submerged", True),
            ("seamark:wreck:water_level", "covers", False),
            ("seamark:wreck:water_level", "awash", False),
            ("seamark:wreck:water_level", "35", False),
            ("wreck:visible_at_low_tide", "yes", False),
            ("wreck:visible_at_low_tide", "yes;no", False),
            ("wreck:visible_at_low_tide", "no", True),
            ("wreck:visible_at_high_tide", "yes", False),
            ("wreck:visible_at_high_tide", "no", True),
            ("access", "no", False),
            ("access", "private", False),
            ("access", "yes", True),
            ("access", "permit", True),
        ],
    )
    def test_the_tag(self, key: str, value: str, passes: bool) -> None:
        assert bool(_kept(_element(tags={key: value}))) is passes


class TestHeritage:
    @pytest.mark.parametrize(
        "tags",
        [
            {"heritage": "2"},
            {"protection_title": "Protected wreck"},
            {"ref:nz:heritage": "1234"},
            {"archaeological_site": "shipwreck"},
            {"war_grave": "yes"},
            {"raä:ref": "Boo 58"},
            {"historic": "archaeological_site"},
            {"seamark:status": "historic"},
            {"seamark:status": "permanent;historic"},
            {"seamark:information": "Underwater Cultural Heritage"},
            {"description": "Site archéologique sous-marin"},
            {"note": "A war grave; do not enter"},
        ],
    )
    def test_what_marks_a_wreck_as_heritage_leaves_it_out(self, tags: dict[str, str]) -> None:
        assert not _kept(_element(tags=tags))

    @pytest.mark.parametrize(
        "tags",
        [
            {"website": "https://example.org/maritime-heritage/wreck"},
            {"seamark:status": "permanent"},
            {"name:en": "Heritage"},
            {"description": "Lies on a gravel bottom"},
            {"note": "Engraved plate on the bow"},
        ],
    )
    def test_what_only_contains_the_word_does_not(self, tags: dict[str, str]) -> None:
        assert _kept(_element(tags=tags))


class TestTheEntriesLeftOutByName:
    @pytest.mark.parametrize(
        ("identifier", "name"),
        [(1347780019, "HMAS Perth"), (6516992724, "Riksäpplet")],
    )
    def test_no_tag_is_needed_and_the_entry_is_what_decides(self, identifier: int, name: str) -> None:
        kept, left_out = wrecks.candidates(_response(_element(identifier, name=name)), set())

        assert kept == []
        assert left_out == {"left out by name": 1}

    def test_the_same_name_on_another_entry_is_not_left_out(self) -> None:
        assert _kept(_element(5, name="HMAS Perth"))

    def test_every_entry_says_why(self) -> None:
        assert all(reason.strip() for reason in wrecks._LEFT_OUT_BY_NAME.values())


class TestThePool:
    def test_a_wreck_the_pool_holds_is_untouched(self) -> None:
        assert not _kept(_element(5), pool={("openstreetmap", "node/5")})

    def test_a_wreck_whose_wikidata_entry_the_pool_holds_is_out(self) -> None:
        assert not _kept(_element(wikidata="Q42"), pool={("wikidata", "Q42")})

    def test_any_other_is_not(self) -> None:
        assert _kept(_element(5, wikidata="Q42"), pool={("openstreetmap", "node/6"), ("wikidata", "Q43")})

    def test_the_entries_are_every_record_s(self, tmp_path: Path) -> None:
        def write(path: str, entries: list[dict[str, str]]) -> None:
            file = tmp_path / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps({"properties": {"external_ids": entries}}), encoding="utf-8")

        write("sites/openstreetmap/node/1.geojson", [{"registry": "openstreetmap", "identifier": "node/1"}])
        write("sites/wikidata/Q2.geojson", [{"registry": "wikidata", "identifier": "Q2"}])
        write("sites/opendivingcommons/a.geojson", [])

        assert wrecks.pool_entries(tmp_path) == {("openstreetmap", "node/1"), ("wikidata", "Q2")}


class TestTheCandidate:
    def test_it_is_a_record_in_the_repository_s_shape(self) -> None:
        tags = {"name": "SS Thistlegorm", "name:en": "Thistlegorm", "wreck:depth": "16-32m"}
        element = {"type": "node", "id": 255316037, "lat": 27.8140922, "lon": 33.9200481, "tags": tags}

        assert _kept(element) == [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [33.920048, 27.814092]},
                "properties": {
                    "uuid": "74c091ba-1ec3-5a6b-b70e-fe31c5438605",
                    "name": "SS Thistlegorm",
                    "external_ids": [{"registry": "openstreetmap", "identifier": "node/255316037"}],
                    "other_names": ["Thistlegorm"],
                    "depth_from": 16.0,
                    "depth_to": 32.0,
                    "extensions": {
                        "opendivingcommons": {
                            "source": "openstreetmap",
                            "license": "ODbL-1.0",
                            "retrieved": "2026-11-06",
                            "seeded_from": {"registry": "openstreetmap", "identifier": "node/255316037"},
                        }
                    },
                },
            }
        ]

    def test_one_depth_is_a_depth_to_alone(self) -> None:
        properties = _kept(_element())[0]["properties"]

        assert properties["depth_to"] == 30.0 and "depth_from" not in properties

    @pytest.mark.parametrize("name_en", ["Moltke", " moltke ", ""])
    def test_an_english_name_saying_what_the_name_says_is_no_other_name(self, name_en: str) -> None:
        assert "other_names" not in _kept(_element(tags={"name:en": name_en}))[0]["properties"]

    def test_a_way_stands_at_its_centre(self) -> None:
        record = _kept(_element(kind="way"))[0]

        assert record["geometry"]["coordinates"] == [167.7109, 8.7404]
        assert record["properties"]["external_ids"][0]["identifier"] == "way/1"

    def test_candidates_are_written_in_the_order_of_their_entry(self) -> None:
        kept = _kept(_element(30), _element(2, "way"), _element(4))

        assert [record["properties"]["external_ids"][0]["identifier"] for record in kept] == [
            "node/4",
            "node/30",
            "way/2",
        ]


class TestTheCounts:
    def test_each_element_counts_against_the_first_floor_it_fails(self) -> None:
        response = _response(
            _element(1),
            _element(2, name="Shipwreck", access="no"),
            _element(3, access="no"),
            _element(4, tags={"wreck:depth": "90"}),
        )

        kept, left_out = wrecks.candidates(response, {("openstreetmap", "node/1")})

        assert kept == []
        assert left_out == {
            "already in the pool": 1,
            "not a name": 1,
            "closed": 1,
            "not at least 3 m and at most 60 m": 1,
        }


class TestTheRequest:
    def test_it_names_the_project_and_nobody_in_it(self) -> None:
        assert "OpenDiving" in wrecks._USER_AGENT
        assert "@" not in wrecks._USER_AGENT

    def test_a_partial_answer_is_refused(self) -> None:
        body = json.dumps({"elements": [], "remark": "runtime error: Query timed out"}).encode()

        with pytest.raises(ValueError, match="timed out"):
            wrecks.overpass_answer(body)

    def test_a_whole_answer_reads_through(self) -> None:
        assert wrecks.overpass_answer(json.dumps(_response()).encode()) == _response()
