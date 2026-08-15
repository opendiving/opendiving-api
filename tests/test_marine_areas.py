"""Tests for `services/marine_areas.py` - the vendored sea polygons that answer a pin in
open water, where the geocoding provider has no row at all.

Every coordinate below is a real position checked against the file in `src/app/data/`, so
these are as much a check on the data as on the arithmetic: a bad refresh - a dropped
feature, a rounding pass that collapses a ring, a source that starts stitching polygons
across the antimeridian - fails here rather than in production.
"""

import json

import pytest

from src.app.services import marine_areas
from src.app.services.marine_areas import sea_name


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
        assert sea_name(latitude, longitude) == expected

    @pytest.mark.parametrize(
        "latitude,longitude",
        [
            (48.8, 2.35),  # Paris
            (23.4, 25.0),  # the Sahara
            (-24.0, 133.0),  # central Australia
        ],
    )
    def test_land_has_no_sea_name(self, latitude: float, longitude: float):
        """The dataset holds water, not coastlines, so "not in any polygon" is the only thing
        this can know - and it is the right answer for a point on land."""
        assert sea_name(latitude, longitude) is None

    def test_an_island_inside_a_sea_is_not_the_sea(self):
        """Islands arrive as holes in the surrounding polygon, and a hole that went unread
        would name every Greek island "Aegean Sea"."""
        assert sea_name(37.0, 25.2) == "Aegean Sea"
        assert sea_name(37.1, 25.5) is None  # Naxos, a hole in that same polygon

    def test_decides_a_boundary_rather_than_smearing_it(self):
        """Two positions 0.1° apart across the Red Sea's eastern shore. The pair matters more
        than either point: a ray-casting bug usually reads as "everything is inside" or
        "nothing is", and one assertion alone catches neither."""
        assert sea_name(20.0, 40.4) == "Red Sea"
        assert sea_name(20.0, 40.5) is None


class TestOverlappingAreas:
    """Overlap is the norm in this dataset, not an edge case - almost every sea sits inside
    an ocean - so which of the two comes back is a decision, not an accident."""

    def test_the_smaller_area_wins(self):
        """A diver wants "Red Sea"; "Indian Ocean", which also contains this point, is
        technically true and useless."""
        assert sea_name(20.0, 38.5) == "Red Sea"

    def test_open_ocean_still_gets_its_ocean(self):
        """The flip side: nothing smaller contains this point, so the coarse name is not a
        fallback failure but the whole answer."""
        assert sea_name(30.0, -40.0) == "North Atlantic Ocean"


class TestTheAntimeridian:
    """The Bering Sea straddles ±180 and arrives from the source as two separate polygons,
    which is what lets this module stay flat-plane arithmetic with no wrap-around case.
    Both sides are asserted, because a refresh that merged them into one ring would still
    answer correctly on one side."""

    def test_east_of_the_meridian(self):
        assert sea_name(58.0, 170.0) == "Bering Sea"

    def test_west_of_the_meridian(self):
        assert sea_name(60.0, -170.0) == "Bering Sea"

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


class TestTheVendoredFile:
    def test_records_where_it_came_from(self):
        """Public domain is the reason this can be vendored at all, and the source URL is the
        only thing that makes a refresh possible. Both live in the file, not in a comment
        that can drift away from it."""
        document = json.loads(marine_areas._DATA_PATH.read_text(encoding="utf-8"))

        assert "natural-earth" in document["source"]
        assert "public domain" in document["licence"].casefold()
        assert document["retrieved"]

    def test_every_part_carries_a_name(self):
        """Natural Earth ships eleven unnamed features; `scripts/build_marine_areas.py` drops
        them, because an unnamed polygon can only ever produce a blank suggestion."""
        assert all(part.name for part in marine_areas._parts())
