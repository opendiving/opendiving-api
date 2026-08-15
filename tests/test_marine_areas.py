"""Tests for `services/marine_areas.py` - the vendored sea polygons that answer a pin in
open water, where the geocoding provider has no row at all.

Every coordinate below is a real position checked against the file in `src/app/data/`, so
these are as much a check on the data as on the arithmetic: a bad refresh - a dropped
feature, a rounding pass that collapses a ring, a source that starts stitching polygons
across the antimeridian - fails here rather than in production.
"""

import json
from collections.abc import Generator
from pathlib import Path

import pytest

from src.app.services import marine_areas
from src.app.services.marine_areas import Ring, water_name

_REAL_DATA = marine_areas._DATA_PATH


def _ring_of(low: float, high: float) -> Ring:
    """A closed square ring, corners at the two coordinates."""
    return ((low, low), (high, low), (high, high), (low, high), (low, low))


def _square(name: str, low: float, high: float, holes: list[Ring] | None = None) -> marine_areas._Part:
    return marine_areas._Part(
        name=name,
        min_lon=low,
        min_lat=low,
        max_lon=high,
        max_lat=high,
        rings=(_ring_of(low, high), *(holes or ())),
    )


class TestKnownPositions:
    @pytest.mark.parametrize(
        "latitude,longitude,expected",
        [
            # The two positions this feature was written for: both answer
            # `{"error": "Unable to geocode"}` on the live provider, at every zoom.
            (27.0, 35.0, "Red Sea"),
            (30.0, -40.0, "North Atlantic Ocean"),
            (-18.0, 147.7, "Coral Sea"),
            (25.1, -80.4, "Gulf of Mexico"),
            (37.0, 25.2, "Aegean Sea"),
        ],
    )
    def test_names_the_sea(self, latitude: float, longitude: float, expected: str):
        assert water_name(latitude, longitude) == expected

    @pytest.mark.parametrize(
        "latitude,longitude",
        [
            (48.8, 2.35),  # Paris
            (23.4, 25.0),  # the Sahara
            (-24.0, 133.0),  # central Australia
        ],
    )
    def test_land_has_no_water_name(self, latitude: float, longitude: float):
        """The dataset holds water, not coastlines, so "not in any polygon" is the only thing
        this can know - and it is the right answer for a point on land."""
        assert water_name(latitude, longitude) is None

    def test_an_island_inside_a_sea_is_not_the_sea(self):
        """Islands arrive as holes in the surrounding polygon, and a hole that went unread
        would name every Greek island "Aegean Sea"."""
        assert water_name(37.0, 25.2) == "Aegean Sea"
        assert water_name(37.1, 25.5) is None  # Naxos, a hole in that same polygon

    def test_a_hole_ends_the_search_rather_than_deferring_to_a_bigger_sea(self, monkeypatch: pytest.MonkeyPatch):
        """The failure guarded against is not "the hole was ignored" but "the hole was
        honoured and then stepped over": the next candidate is always coarser, so if it does
        not carry the same island, falling through returns a plausible-looking wrong answer -
        a rock in the Red Sea coming back "Indian Ocean".

        Built from two synthetic polygons rather than a real island, because the shipped data
        does not currently contain the case: comparing the two rules over 515,520 grid points
        found no position where they disagree, since Natural Earth's oceans carry the same
        island holes their seas do. The rule is asserted anyway - it is a decision about what
        happens when they don't, and a refresh could introduce that at any time.
        """
        island_in_a_bay = _square("Small Bay", -1, 1, holes=[_ring_of(-0.5, 0.5)])
        ocean_that_missed_it = _square("Big Ocean", -10, 10)
        monkeypatch.setattr(marine_areas, "_parts", lambda: (island_in_a_bay, ocean_that_missed_it))

        assert water_name(0.9, 0.9) == "Small Bay"
        assert water_name(5.0, 5.0) == "Big Ocean"
        assert water_name(0.0, 0.0) is None

    def test_decides_a_boundary_rather_than_smearing_it(self):
        """Two positions 0.1° apart across the Red Sea's eastern shore. The pair matters more
        than either point: a ray-casting bug usually reads as "everything is inside" or
        "nothing is", and one assertion alone catches neither."""
        assert water_name(20.0, 40.4) == "Red Sea"
        assert water_name(20.0, 40.5) is None


class TestOverlappingAreas:
    """Overlap is the norm in this dataset, not an edge case - almost every sea sits inside
    an ocean - so which of the two comes back is a decision, not an accident."""

    def test_the_smaller_area_wins(self):
        """A diver wants "Red Sea"; "Indian Ocean", which also contains this point, is
        technically true and useless."""
        assert water_name(20.0, 38.5) == "Red Sea"

    def test_open_ocean_still_gets_its_ocean(self):
        """The flip side: nothing smaller contains this point, so the coarse name is not a
        fallback failure but the whole answer."""
        assert water_name(30.0, -40.0) == "North Atlantic Ocean"


class TestTheAntimeridian:
    """The Bering Sea straddles ±180 and arrives from the source as two separate polygons,
    which is what lets this module stay flat-plane arithmetic with no wrap-around case.
    Both sides are asserted, because a refresh that merged them into one ring would still
    answer correctly on one side."""

    def test_east_of_the_meridian(self):
        assert water_name(58.0, 170.0) == "Bering Sea"

    def test_west_of_the_meridian(self):
        assert water_name(60.0, -170.0) == "Bering Sea"

    def test_no_polygon_part_wraps_the_meridian(self):
        """A part whose longitudes span more than half the globe would be a ring stitched
        across the seam, and its bounding box would reject nothing. The two circumpolar
        oceans are the honest exceptions - they encircle a pole, so 360° is what they are."""
        circumpolar = {"Arctic Ocean", "Southern Ocean"}
        wrapping = [
            part.name
            for part in marine_areas._parts()
            if part.max_lon - part.min_lon > 180 and part.name not in circumpolar
        ]

        assert wrapping == []


class TestDegradation:
    """Reading the file is the only thing in this module that touches the world, and the
    geocoding path it hangs off promises to answer "no suggestion" rather than raise. A
    truncated data file must not turn the dive-site form into a 500."""

    @pytest.fixture(autouse=True)
    def unloaded(self) -> Generator[None]:
        """`_parts` remembers a successful read, so a test that swaps the path has to reset it
        on both sides - or it reads the real file instead of the broken one."""
        marine_areas._loaded = None
        yield
        marine_areas._loaded = None

    @pytest.mark.parametrize("contents", ["", "{ truncated", '{"features": [{"properties": {}}]}'])
    def test_an_unusable_file_means_no_water_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str):
        broken = tmp_path / "marine_areas.geojson"
        broken.write_text(contents, encoding="utf-8")
        monkeypatch.setattr(marine_areas, "_DATA_PATH", broken)

        assert water_name(27.0, 35.0) is None

    def test_a_missing_file_means_no_water_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(marine_areas, "_DATA_PATH", tmp_path / "not-here.geojson")

        assert water_name(27.0, 35.0) is None

    def test_a_repaired_file_recovers_without_a_restart(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Only a successful read is remembered. Memoizing the failure would leave one bad
        read to disable the fallback for the life of the process, on evidence no stronger than
        a single `OSError` - and this is exactly what a mid-regeneration read looks like."""
        path = tmp_path / "marine_areas.geojson"
        monkeypatch.setattr(marine_areas, "_DATA_PATH", path)
        assert water_name(27.0, 35.0) is None

        path.write_text(_REAL_DATA.read_text(encoding="utf-8"), encoding="utf-8")

        assert water_name(27.0, 35.0) == "Red Sea"


class TestTheVendoredFile:
    def test_records_where_it_came_from(self):
        """Public domain is the reason this can be vendored at all, and the source URL is the
        only thing that makes a refresh possible. Both live in the file, not in a comment
        that can drift away from it."""
        document = json.loads(marine_areas._DATA_PATH.read_text(encoding="utf-8"))

        assert "natural-earth" in document["source"]
        assert "public domain" in document["licence"].casefold()
        assert document["retrieved"]

    def test_holds_the_dataset_it_is_documented_to_hold(self):
        """The one assertion that makes a bad refresh loud. Everything upstream drops quietly
        by design - unnamed features, rings that rounding collapsed, parts with no outer ring -
        so a source that changed shape could shed a hundred features and leave every other test
        here still passing on the handful of coordinates they name. These numbers are quoted in
        `DECISIONS.md` and in both modules' docstrings; if a refresh moves them, that is a
        decision to take rather than a diff to accept."""
        document = json.loads(_REAL_DATA.read_text(encoding="utf-8"))

        assert len(document["features"]) == 293
        assert len(marine_areas._parts()) == 324

    def test_every_part_carries_a_name(self):
        """Natural Earth ships eleven unnamed features; `scripts/build_marine_areas.py` drops
        them, because an unnamed polygon can only ever produce a blank suggestion."""
        assert all(part.name for part in marine_areas._parts())
