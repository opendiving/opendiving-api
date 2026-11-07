"""Tests for the importers of the curated sources, whose floors decide what enters a pool nothing is
ever removed from. They read rows copied from each publisher's layer, never the network."""

import importlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "commons_registries"

# `scripts/` is not a package, and an importer reads its sibling as a script does.
sys.path.insert(0, str(_SCRIPTS))
curated: Any = importlib.import_module("curated_source")
keys: Any = importlib.import_module("import_noaa_florida_keys")
thunder_bay: Any = importlib.import_module("import_noaa_thunder_bay")
vessels: Any = importlib.import_module("import_fwc_artificial_reefs")

_RETRIEVED = "2026-11-07"
_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_DEPLOY_ID = re.compile(r"[A-Z]{2}[0-9]{4}")


def _rows(name: str) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))["features"]
    return features


def _by_name(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {record["properties"]["name"]: record for record in records}


def _provenance(record: dict[str, Any]) -> dict[str, Any]:
    provenance: dict[str, Any] = record["properties"]["extensions"]["opendivingcommons"]
    return provenance


def _identifier(record: dict[str, Any]) -> str:
    identifier: str = _provenance(record)["seeded_from"]["identifier"]
    return identifier


class TestTheRecord:
    def test_it_is_the_repository_s_shape_with_no_registry_entry(self) -> None:
        record = curated.candidate(
            "noaa-florida-keys-moorings",
            "french-reef",
            "French Reef",
            (-80.34854888899996, 25.034149170000024),
            license="LicenseRef-US-Gov-PD",
            retrieved=_RETRIEVED,
            depths=(7.0104, 16.764),
        )

        assert record == {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-80.348549, 25.034149]},
            "properties": {
                "uuid": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "https://github.com/opendiving/opendiving-commons/sites/noaa-florida-keys-moorings/french-reef",
                    )
                ),
                "name": "French Reef",
                "depth_from": 7.0,
                "depth_to": 16.8,
                "extensions": {
                    "opendivingcommons": {
                        "source": "noaa-florida-keys-moorings",
                        "license": "LicenseRef-US-Gov-PD",
                        "retrieved": _RETRIEVED,
                        "seeded_from": {"registry": "noaa-florida-keys-moorings", "identifier": "french-reef"},
                    }
                },
            },
        }

    @pytest.mark.parametrize(
        ("depths", "members"),
        [
            ((None, None), {}),
            ((None, 30.48), {"depth_to": 30.5}),
            ((7.3152, 7.3152), {"depth_to": 7.3}),
            ((7.31, 7.33), {"depth_to": 7.3}),
        ],
        ids=["none", "one-end", "one-depth", "one-depth-once-rounded"],
    )
    def test_a_single_depth_is_written_as_the_pool_writes_one(
        self, depths: tuple[float | None, float | None], members: dict[str, float]
    ) -> None:
        record = curated.candidate("t", "i", "N", (1.0, 2.0), license="L", retrieved=_RETRIEVED, depths=depths)

        assert {key: value for key, value in record["properties"].items() if key.startswith("depth")} == members


class TestTheSlug:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("French Reef", "french-reef"),
            ("Alex’s Mountains", "alexs-mountains"),
            ("T.J.’s Rise", "t-j-s-rise"),
            ("Hens & Chickens", "hens-chickens"),
            ("Carysfort Shoal (in)", "carysfort-shoal-in"),
            ("E. B. Allen", "e-b-allen"),
            ("New Orleans (1838)", "new-orleans-1838"),
        ],
    )
    def test_it_is_a_form_the_repository_takes(self, name: str, expected: str) -> None:
        assert curated.slug(name) == expected
        assert _SLUG.fullmatch(expected)

    def test_two_sites_on_one_identifier_stop_the_run(self) -> None:
        row = {"attributes": {"Location": "Dry Rocks", "BuoyType": "Mooring", "Depth__ft_": 10}}
        rows = [{**row, "geometry": {"x": -80.0, "y": 25.0}}]
        other = {"attributes": {**row["attributes"], "Location": "Dry-Rocks"}, "geometry": {"x": -80.0, "y": 25.0}}

        with pytest.raises(ValueError, match="dry-rocks"):
            keys.candidates([*rows, other], _RETRIEVED)


class TestTheLayerAnswer:
    def test_a_refused_query_is_not_an_empty_register(self) -> None:
        with pytest.raises(ValueError, match="refused"):
            curated.layer_answer(b'{"error": {"code": 400, "message": "Invalid query"}}')

    def test_a_partial_answer_is_not_a_smaller_register(self) -> None:
        with pytest.raises(ValueError, match="in part"):
            curated.layer_answer(b'{"features": [{}], "exceededTransferLimit": true}')

    def test_the_request_names_the_project_and_nobody_in_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = []

        class Answer:
            def __enter__(self) -> Answer:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"features": []}'

        def urlopen(request: Any, timeout: int) -> Answer:
            sent.append(request)
            return Answer()

        monkeypatch.setattr(curated.urllib.request, "urlopen", urlopen)

        assert curated.fetch_layer("https://example.test/0") == []
        agent = sent[0].get_header("User-agent")
        assert "OpenDiving" in agent and "@" not in agent


class TestTheFloridaKeys:
    @pytest.fixture
    def kept(self) -> dict[str, dict[str, Any]]:
        return _by_name(keys.candidates(_rows("florida_keys.json"), _RETRIEVED)[0])

    def test_a_site_is_a_location_with_a_mooring(self) -> None:
        kept, left_out = keys.candidates(_rows("florida_keys.json"), _RETRIEVED)

        assert {record["properties"]["name"] for record in kept} == {
            "Alex’s Mountains",
            "French Reef",
            "Grecian Rocks",
            "New Found Harbor",
            "T.J.’s Rise",
            "The Spiegel Grove",
        }
        assert left_out == {"not a mooring buoy": 3, "left out by name": 2}

    @pytest.mark.parametrize("name", ["Archer Key", "Cottrell Key", "Grecian", "Marker 48", "Mule Key", "Mule Key 2"])
    def test_a_site_left_out_by_name_is_out(self, name: str) -> None:
        mooring = {
            "attributes": {"Location": name, "BuoyType": "Mooring", "Depth__ft_": 8},
            "geometry": {"x": 0, "y": 1},
        }

        assert keys.candidates([mooring], _RETRIEVED) == ([], {"left out by name": 1})

    def test_every_entry_left_out_by_name_is_an_identifier_with_a_reason(self) -> None:
        assert len(keys._LEFT_OUT_BY_NAME) == 6
        assert all(_SLUG.fullmatch(identifier) and reason for identifier, reason in keys._LEFT_OUT_BY_NAME.items())

    def test_the_depths_are_the_ends_of_its_moorings_in_metres(self, kept: dict[str, dict[str, Any]]) -> None:
        properties = kept["French Reef"]["properties"]

        assert (properties["depth_from"], properties["depth_to"]) == (7.0, 16.8)

    def test_a_mooring_with_no_depth_adds_nothing(self) -> None:
        moorings = [
            {
                "attributes": {"Location": "Reef", "BuoyType": "Mooring", "Depth__ft_": depth},
                "geometry": {"x": 0, "y": 1},
            }
            for depth in (None, 10, 20)
        ]

        properties = keys.candidates(moorings, _RETRIEVED)[0][0]["properties"]

        assert (properties["depth_from"], properties["depth_to"]) == (3.0, 6.1)

    def test_a_site_none_of_whose_moorings_says_has_no_depth(self, kept: dict[str, dict[str, Any]]) -> None:
        properties = kept["New Found Harbor"]["properties"]

        assert "depth_from" not in properties and "depth_to" not in properties

    def test_the_pin_is_a_real_buoy_and_one_misplaced_row_does_not_move_it(self) -> None:
        moorings = [
            {"geometry": {"x": -80.0, "y": 25.0}},
            {"geometry": {"x": -80.001, "y": 25.001}},
            {"geometry": {"x": -80.002, "y": 25.002}},
            {"geometry": {"x": -60.0, "y": 40.0}},
        ]

        assert keys.pin_of(moorings) in {(-80.001, 25.001), (-80.002, 25.002)}
        assert keys.pin_of(moorings) == keys.pin_of(list(reversed(moorings)))

    def test_the_candidates_are_in_the_identifier_s_order_and_the_repository_s_form(self) -> None:
        identifiers = [_identifier(record) for record in keys.candidates(_rows("florida_keys.json"), _RETRIEVED)[0]]

        assert identifiers == sorted(identifiers)
        assert all(_SLUG.fullmatch(identifier) for identifier in identifiers)

    def test_a_location_is_trimmed_before_it_is_a_site(self) -> None:
        mooring = {
            "attributes": {"Location": "Dry Rocks ", "BuoyType": "Mooring", "Depth__ft_": 8},
            "geometry": {"x": 0, "y": 1},
        }

        assert _identifier(keys.candidates([mooring], _RETRIEVED)[0][0]) == "dry-rocks"

    def test_trimming_comes_before_the_list_left_out_by_name(self) -> None:
        mooring = {
            "attributes": {"Location": "Mule Key ", "BuoyType": "Mooring", "Depth__ft_": 8},
            "geometry": {"x": 0, "y": 1},
        }

        assert keys.candidates([mooring], _RETRIEVED)[0] == []


class TestThunderBay:
    @pytest.fixture
    def kept(self) -> dict[str, dict[str, Any]]:
        return _by_name(thunder_bay.candidates(_rows("thunder_bay.json"), _RETRIEVED)[0])

    def test_the_row_about_the_map_is_no_site(self) -> None:
        kept, left_out = thunder_bay.candidates(_rows("thunder_bay.json"), _RETRIEVED)

        assert len(kept) == 3
        assert left_out == {"no depth in its description": 1}

    def test_the_name_is_the_title_and_never_what_the_buoy_is_doing(self, kept: dict[str, dict[str, Any]]) -> None:
        assert set(kept) == {"E. B. Allen", "Monohansett", "New Orleans (1838)"}

    def test_a_site_is_fresh_water_to_the_depth_its_description_says(self, kept: dict[str, dict[str, Any]]) -> None:
        properties = kept["E. B. Allen"]["properties"]

        assert properties["depth_to"] == 30.5
        assert properties["water_type"] == "fresh"
        assert "depth_from" not in properties

    def test_a_name_the_florida_keys_leave_out_stays_here(self) -> None:
        row = {
            "attributes": {"Icon_Title": "Grecian", "Name": "Grecian", "Description": "<b>Depth:</b> 106 Feet"},
            "geometry": {"x": -83.201, "y": 44.9686},
        }

        (record,) = thunder_bay.candidates([row], _RETRIEVED)[0]

        assert _identifier(record) == "grecian"
        assert record["properties"]["depth_to"] == 32.3

    def test_no_depth_floor_stands_between_a_shallow_mooring_and_the_pool(self) -> None:
        assert thunder_bay.depth_of("<b>Depth:</b> 5 Feet<br>") == pytest.approx(1.524)

    @pytest.mark.parametrize("description", [None, "", "Depths of Lake Huron", "<b>Depth:</b> unknown"])
    def test_a_description_saying_none_has_none(self, description: str | None) -> None:
        assert thunder_bay.depth_of(description) is None

    def test_the_candidates_are_in_the_identifier_s_order(self) -> None:
        identifiers = [
            _identifier(record) for record in thunder_bay.candidates(_rows("thunder_bay.json"), _RETRIEVED)[0]
        ]

        assert identifiers == ["e-b-allen", "monohansett", "new-orleans-1838"]


def _vessel(**attributes: Any) -> dict[str, Any]:
    return {
        "attributes": {
            "DeployID": "MO0061",
            "DDate": 20020517,
            "Description": "A ship",
            "Depth": 100.0,
            "Relief": 20.0,
            "Name": "Spiegel Grove",
            "MatDescrip": "Freighter 200'",
            "MatCat": "Vessel",
            **attributes,
        },
        "geometry": {"x": -80.3108, "y": 25.0667},
    }


class TestTheFloridaVessels:
    @pytest.fixture
    def kept(self) -> dict[str, dict[str, Any]]:
        return _by_name(vessels.candidates(_rows("fwc_vessels.json"), _RETRIEVED)[0])

    def test_each_floor_is_counted_against_the_first_a_row_fails(self) -> None:
        _, left_out = vessels.candidates(_rows("fwc_vessels.json"), _RETRIEVED)

        assert left_out == {
            "a barge": 1,
            "no depth above 0": 1,
            "deeper than 60 m": 1,
            "a permit or site code": 2,
            "left out by name": 3,
        }

    def test_of_the_two_drydocks_the_one_dated_first_stays(self, kept: dict[str, dict[str, Any]]) -> None:
        assert _identifier(kept["Navy Drydock"]) == "SJ0016"
        assert "OSJ Site - Drydocks" not in kept

    @pytest.mark.parametrize("deployment", ["DU0071", "MO0024", "MO0062"])
    def test_a_vessel_left_out_by_name_is_out(self, deployment: str) -> None:
        assert vessels.candidates([_vessel(DeployID=deployment)], _RETRIEVED) == ([], {"left out by name": 1})

    def test_every_entry_left_out_by_name_is_a_deployment_with_a_reason(self) -> None:
        assert len(vessels._LEFT_OUT_BY_NAME) == 3
        assert all(_DEPLOY_ID.fullmatch(key) and reason for key, reason in vessels._LEFT_OUT_BY_NAME.items())

    def test_a_row_that_is_no_vessel_is_out(self) -> None:
        assert vessels.candidates([_vessel(MatCat="Concrete")], _RETRIEVED)[1] == {"not a vessel": 1}

    @pytest.mark.parametrize("described", ["Barge 110'", "Steel Barge (120')", "BARGE", "Barges"])
    def test_a_barge_is_out_however_it_is_written(self, described: str) -> None:
        assert vessels.candidates([_vessel(MatDescrip=described)], _RETRIEVED)[1] == {"a barge": 1}

    @pytest.mark.parametrize(
        ("feet", "floor"),
        [(0.0, "no depth above 0"), (None, "no depth above 0"), (196.9, "deeper than 60 m")],
    )
    def test_the_depth_floor_is_read_in_metres(self, feet: float | None, floor: str) -> None:
        assert vessels.candidates([_vessel(Depth=feet)], _RETRIEVED)[1] == {floor: 1}

    def test_sixty_metres_is_within_reach(self) -> None:
        kept, left_out = vessels.candidates([_vessel(Depth=196.85)], _RETRIEVED)

        assert len(kept) == 1 and not left_out

    @pytest.mark.parametrize("name", ["ESERS311", "MB 218", "MB-125", "Site #3_1995", "Site #3", "", "  ", None])
    def test_a_code_alone_is_not_a_name(self, name: str | None) -> None:
        assert not vessels.is_a_name(_vessel(Name=name)["attributes"])

    @pytest.mark.parametrize(
        "name", ["Rodeo 25", "Mariner 1", "C-One", "Spiegel Grove", "Site C - Rebel", "Site #9_Tug Reef"]
    )
    def test_a_name_with_a_number_in_it_is_a_name(self, name: str) -> None:
        assert vessels.is_a_name(_vessel(Name=name)["attributes"])

    def test_a_hull_number_the_register_quotes_as_the_vessel_is_its_name(self, kept: dict[str, dict[str, Any]]) -> None:
        assert "PC-1174" in kept

    def test_the_depths_are_converted_and_the_relief_rises_from_the_bottom(
        self, kept: dict[str, dict[str, Any]]
    ) -> None:
        properties = kept["Spiegel Grove"]["properties"]

        assert (properties["depth_from"], properties["depth_to"]) == (15.5, 41.1)

    @pytest.mark.parametrize(
        ("name", "depth_to"),
        [("Ft. Lauderdale, C-1 Reef", 7.6), ("Lead Wreck", 21.3), ("Veterans Reef - 40' Steel Hulled Sailboat", 13.7)],
        ids=["relief-past-the-surface", "no-relief", "null-relief"],
    )
    def test_a_top_that_says_nothing_is_not_written(
        self, kept: dict[str, dict[str, Any]], name: str, depth_to: float
    ) -> None:
        properties = kept[name]["properties"]

        assert "depth_from" not in properties
        assert properties["depth_to"] == depth_to

    def test_the_identifier_is_the_deployment_s_and_the_repository_s_form(
        self, kept: dict[str, dict[str, Any]]
    ) -> None:
        assert _identifier(kept["Spiegel Grove"]) == "MO0061"
        assert all(_DEPLOY_ID.fullmatch(_identifier(record)) for record in kept.values())

    def test_a_name_s_runs_of_spaces_are_closed_up(self) -> None:
        kept, _ = vessels.candidates([_vessel(Name="Navy  Drydock")], _RETRIEVED)

        assert kept[0]["properties"]["name"] == "Navy Drydock"

    def test_the_oldest_comes_first_an_unknown_date_last_and_the_deployment_breaks_a_tie(self) -> None:
        identifiers = [
            _identifier(record)
            for record in vessels.candidates(
                [
                    _vessel(DeployID="PB0001", DDate=0),
                    _vessel(DeployID="FR0032", DDate=19970211),
                    _vessel(DeployID="FR0031", DDate=19970211),
                    _vessel(DeployID="BE0008", DDate=19420503),
                ],
                _RETRIEVED,
            )[0]
        ]

        assert identifiers == ["BE0008", "FR0031", "FR0032", "PB0001"]
