"""Tests for `scripts/build_marine_areas.py`, the hand-run generator behind
`src/app/data/marine_areas.geojson`.

It is tested despite never running in production because its output is what production reads,
and the three pieces below are where a quiet mistake would survive: a ring that rounding
collapsed, the deliberate asymmetry between an outer ring and a hole, and the antimeridian
check that lets `services.marine_areas` stay flat-plane arithmetic. `test_marine_areas.py`
asserts the same invariants from the other side, against the file that was actually shipped -
these assert them against inputs the current source does not happen to contain.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_marine_areas.py"


def _load_generator() -> Any:
    """`scripts/` is not a package and is not on the path - it is build-time tooling that
    ships nowhere - so it is loaded by location rather than imported."""
    spec = importlib.util.spec_from_file_location("build_marine_areas", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build = _load_generator()


class TestNames:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Red Sea", "Red Sea"),
            # Natural Earth shouts exactly two of them, and this value is headed for a dive
            # site locality's `name`.
            ("SOUTHERN OCEAN", "Southern Ocean"),
            ("INDIAN OCEAN", "Indian Ocean"),
            ("  Coral Sea  ", "Coral Sea"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str):
        assert build._name({"name": raw}) == expected

    @pytest.mark.parametrize("properties", [{}, {"name": None}, {"name": "   "}, {"name": 7}])
    def test_drops_what_cannot_be_shown_to_anyone(self, properties: dict):
        """Eleven of the source's features are unnamed, and an unnamed polygon can only ever
        produce a blank suggestion."""
        assert build._name(properties) is None


class TestRings:
    def test_closes_an_open_ring(self):
        ring = build._ring([[0, 0], [1, 0], [1, 1], [0, 1]])

        assert ring is not None
        assert ring[0] == ring[-1]

    def test_drops_vertices_that_rounding_merged(self):
        """At four decimals a handful of vertices in the densest coastlines round onto each
        other; a ring carrying its own vertices twice is just a bigger file."""
        ring = build._ring([[0, 0], [0.00001, 0], [1, 0], [1, 1], [0, 0]])

        assert ring == [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]

    @pytest.mark.parametrize("coordinates", [[], [[0, 0]], [[0, 0], [0.00001, 0.00001], [0, 0]]])
    def test_a_ring_that_collapsed_is_not_a_ring(self, coordinates: list):
        assert build._ring(coordinates) is None


class TestGeometry:
    def test_a_collapsed_hole_only_costs_the_island(self):
        """The island reads as sea, which for a rock a few metres across is the right trade."""
        square = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        speck = [[5, 5], [5.00001, 5], [5.00001, 5.00001], [5, 5]]

        geometry = build._geometry({"type": "Polygon", "coordinates": [square, speck]})

        assert geometry == {"type": "Polygon", "coordinates": [square]}

    def test_a_collapsed_outer_ring_takes_the_whole_part_with_it(self):
        """The asymmetry that matters: filtering rings as one list would promote the surviving
        hole into `rings[0]`, and `marine_areas` would read that island's outline as the sea's
        own boundary."""
        speck = [[5, 5], [5.00001, 5], [5.00001, 5.00001], [5, 5]]
        hole = [[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]

        assert build._geometry({"type": "Polygon", "coordinates": [speck, hole]}) is None

    def test_a_multipolygon_that_loses_all_but_one_part_becomes_a_polygon(self):
        square = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        speck = [[5, 5], [5.00001, 5], [5.00001, 5.00001], [5, 5]]

        geometry = build._geometry({"type": "MultiPolygon", "coordinates": [[square], [speck]]})

        assert geometry == {"type": "Polygon", "coordinates": [square]}


class TestTheAntimeridianCheck:
    def _feature(self, ring: list) -> dict:
        return {"properties": {"name": "Somewhere"}, "geometry": {"type": "Polygon", "coordinates": [ring]}}

    def test_accepts_a_ring_that_stays_on_one_side(self):
        ring = [[170, 10], [179, 10], [179, 20], [170, 20], [170, 10]]

        assert build._antimeridian_is_already_handled([self._feature(ring)])

    def test_accepts_a_circumpolar_ring_closing_along_the_pole(self):
        """The Arctic and Southern Oceans really do encircle a pole, and Natural Earth draws
        them as single rings that follow a coastline the whole way round and close along the
        very top of the map, where the seam is a point rather than a line."""
        ring = [[-180, 71], [-90, 72], [0, 71], [90, 72], [179.9, 71], [179.9, 90], [-180, 90], [-180, 71]]

        assert build._antimeridian_is_already_handled([self._feature(ring)])

    def test_refuses_a_ring_stitched_across_the_seam(self):
        """What a merged Bering Sea would look like - and its bounding box would then span the
        globe and reject nothing."""
        ring = [[179, 55], [-179, 55], [-179, 60], [179, 60], [179, 55]]

        assert not build._antimeridian_is_already_handled([self._feature(ring)])
