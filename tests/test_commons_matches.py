"""The rule that says which Commons records a site may be, ported from the Commons repository
with its tests: the distances are floors calibrated on the catalogue the pool was seeded from,
so these pin the behaviour each was chosen for, and tuning one is a visible decision.

No database: what the rule does with the rows is `test_commons_proposals.py`.
"""

import math

import pytest

from src.app.schemas.dive_site import CommonsMatchRule
from src.app.services.commons_matches import (
    SAME_NAME_METRES,
    judge,
    longitude_ranges,
    metres_between,
    names_similar,
    normalise,
    normalised_names,
)

_METRES_PER_DEGREE_LATITUDE = 111_195.0


def _judged(a: str, b: str, metres: float, *, also: tuple[str, ...] = ()) -> tuple[CommonsMatchRule, bool] | None:
    return judge(normalised_names([a, *also]), normalised_names([b]), metres)


class TestTheRules:
    def test_an_unrelated_name_fifty_metres_away_is_not_flagged(self) -> None:
        """Most close pairs are differently named features of one lake or one reef."""
        assert _judged("Bells", "Blue Hole", 50) is None

    def test_anything_within_twenty_metres_is_flagged(self) -> None:
        assert _judged("Canyon", "Blue Hole", 15) == (CommonsMatchRule.WITHIN_20_M, False)

    def test_the_same_name_one_hundred_and_fifty_metres_away_is_flagged_as_similar(self) -> None:
        assert _judged("Blue Hole", "Blue Hole", 150) == (CommonsMatchRule.SIMILAR_NAME_WITHIN_200_M, False)

    def test_shark_point_four_hundred_kilometres_away_is_not_flagged(self) -> None:
        """`Shark Point` is in four countries and never nearer itself than 300 km."""
        assert _judged("Shark Point", "Shark Point", 400_000) is None

    def test_the_same_name_two_kilometres_away_is_flagged(self) -> None:
        assert _judged("SS Thistlegorm", "SS Thistlegorm", 2_000) == (CommonsMatchRule.SAME_NAME_WITHIN_5_KM, False)

    def test_a_generic_name_two_kilometres_away_is_not_flagged(self) -> None:
        """The seven `Diving Spot` placeholders of one archipelago."""
        assert _judged("Diving Spot", "Diving Spot", 2_000) is None

    def test_another_name_is_compared_as_an_alias(self) -> None:
        judged = _judged("Thistlegorm", "SS Thistlegorm", 2_000, also=("SS Thistlegorm",))

        assert judged == (CommonsMatchRule.SAME_NAME_WITHIN_5_KM, False)

    def test_qualifier_siblings_are_flagged_as_such(self) -> None:
        """`Housereef West` and `Housereef East` are similar names and two sites."""
        assert _judged("Housereef West", "Housereef East", 120) == (CommonsMatchRule.SIMILAR_NAME_WITHIN_200_M, True)


class TestSimilarNames:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Blue Hole", "blue hole"),
            ("Umbria", "SS Umbria, Wreck"),
            ("Marsa Nakari South reef", "Marsa Nakari North reef"),
            ("Ras Ghozlani", "Ras el-Ghazlani"),
            ("Café del Mar", "Cafe del Mar"),
        ],
    )
    def test_similar(self, a: str, b: str) -> None:
        assert names_similar(normalise(a), normalise(b))

    @pytest.mark.parametrize(("a", "b"), [("Bells", "Canyon"), ("The Alternatives", "Shark Point"), ("Di", "Dive")])
    def test_not_similar(self, a: str, b: str) -> None:
        assert not names_similar(normalise(a), normalise(b))


class TestNormalising:
    def test_accents_case_and_punctuation_go(self) -> None:
        assert normalise("  Café-du-Port, (Nord)!") == "cafe du port nord"

    def test_a_name_in_another_script_survives(self) -> None:
        """An ASCII strip empties every name with no Latin letter, and then they all match."""
        assert normalise("砂辺") == "砂辺"
        assert normalise("砂辺") != normalise("真栄田岬")


def test_the_distance_is_a_haversine() -> None:
    expected = _METRES_PER_DEGREE_LATITUDE * math.cos(math.radians(28.5))

    assert metres_between(28.5, 34.5, 28.5, 35.5) == pytest.approx(expected, rel=1e-3)


class TestTheLongitudesInReach:
    def _reaches(self, ranges: list[tuple[float, float]] | None, longitude: float) -> bool:
        return ranges is None or any(west <= longitude <= east for west, east in ranges)

    def test_away_from_the_edges_it_is_one_range_around_the_point(self) -> None:
        ranges = longitude_ranges(28.5, 34.5, SAME_NAME_METRES)

        assert ranges is not None and len(ranges) == 1
        west, east = ranges[0]
        assert west < 34.5 < east
        assert east - west < 0.2

    @pytest.mark.parametrize("latitude", [0.0, 45.0, -70.0, 85.0, 89.9])
    def test_a_point_just_inside_the_reach_is_in_the_range(self, latitude: float) -> None:
        """Due east at the same latitude, found by bisection, a metre short of the reach."""
        low, high = 0.0, 179.0
        for _ in range(60):
            middle = (low + high) / 2
            if metres_between(latitude, 10.0, latitude, 10.0 + middle) < SAME_NAME_METRES - 1:
                low = middle
            else:
                high = middle

        assert self._reaches(longitude_ranges(latitude, 10.0, SAME_NAME_METRES), 10.0 + low)

    @pytest.mark.parametrize(("longitude", "neighbour"), [(179.9999, -179.9999), (-179.9999, 179.9999)])
    def test_the_antimeridian_splits_it_in_two(self, longitude: float, neighbour: float) -> None:
        ranges = longitude_ranges(10.0, longitude, SAME_NAME_METRES)

        assert ranges is not None and len(ranges) == 2
        assert self._reaches(ranges, neighbour)

    @pytest.mark.parametrize("latitude", [89.99, -89.99, 90.0, -90.0])
    def test_near_a_pole_every_longitude_is_in_reach(self, latitude: float) -> None:
        assert longitude_ranges(latitude, 10.0, SAME_NAME_METRES) is None
