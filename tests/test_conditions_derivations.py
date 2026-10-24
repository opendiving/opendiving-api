"""The derivations behind a dive's fetched conditions, on recorded Open-Meteo answers.

`dahab-2026-09-15-*` is the Blue Hole's cell (28.57, 34.54) for 2026-09-15; the weather is the
archive's two UTC days, the marine the three days around it. `monterey-2026-10-18-marine.json`
is the cell beside NOAA's Monterey station 9413450, whose predicted highs and lows for
2026-10-18 the sea level is checked against.
"""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.app.schemas.dive import Waves, Weather
from src.app.services.conditions_derivations import (
    ExtremeKind,
    TideState,
    nearest_sample,
    tide,
    waves_from_height,
    weather_from_code,
)

FIXTURES = Path(__file__).parent / "fixtures" / "conditions"


def _hourly(name: str) -> dict[str, Any]:
    hourly: dict[str, Any] = json.loads((FIXTURES / name).read_text())["hourly"]
    return hourly


def _at(year: int, month: int, day: int, hour: int, minute: int, *, offset_hours: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=offset_hours))).timestamp())


class TestWeatherFromTheWmoCode:
    @pytest.mark.parametrize(
        ("code", "weather"),
        [
            (0, Weather.CLEAR),
            (1, Weather.CLEAR),
            (2, Weather.PARTLY_CLOUDY),
            (3, Weather.OVERCAST),
            (45, Weather.FOG),
            (48, Weather.FOG),
            (51, Weather.RAIN),
            (57, Weather.RAIN),
            (65, Weather.RAIN),
            (67, Weather.RAIN),
            (82, Weather.RAIN),
            (71, Weather.SNOW),
            (77, Weather.SNOW),
            (86, Weather.SNOW),
            (95, Weather.STORM),
            (97, Weather.STORM),
            (99, Weather.STORM),
        ],
    )
    def test_each_band(self, code: int, weather: Weather) -> None:
        assert weather_from_code(code) is weather

    @pytest.mark.parametrize("code", [None, 4, 50, 100])
    def test_an_unknown_code_fills_nothing(self, code: int | None) -> None:
        assert weather_from_code(code) is None


class TestWavesFromTheDouglasScale:
    @pytest.mark.parametrize(
        ("height", "waves"),
        [
            (0.0, Waves.CALM),
            (0.49, Waves.CALM),
            (0.5, Waves.SLIGHT),
            (1.24, Waves.SLIGHT),
            (1.25, Waves.MODERATE),
            (2.49, Waves.MODERATE),
            (2.5, Waves.ROUGH),
            (9.0, Waves.ROUGH),
        ],
    )
    def test_each_band_starts_at_its_lower_bound(self, height: float, waves: Waves) -> None:
        assert waves_from_height(height) is waves

    def test_no_height_fills_nothing(self) -> None:
        assert waves_from_height(None) is None


class TestTheNearestHour:
    def test_takes_the_nearest_sample(self) -> None:
        assert nearest_sample([0, 3600, 7200], 5000) == 1

    def test_an_exact_tie_takes_the_earlier_hour(self) -> None:
        assert nearest_sample([0, 3600], 1800) == 0

    def test_an_empty_series_has_none(self) -> None:
        assert nearest_sample([], 0) is None

    def test_the_dahab_dive_reads_the_hour_it_was_at(self) -> None:
        hourly = _hourly("dahab-2026-09-15-weather.json")
        index = nearest_sample(hourly["time"], _at(2026, 9, 15, 10, 0, offset_hours=3))
        assert index is not None
        assert hourly["temperature_2m"][index] == 38.1
        assert weather_from_code(hourly["weather_code"][index]) is Weather.CLEAR


class TestTheTide:
    def test_a_plateaued_high_is_found_at_its_midpoint(self) -> None:
        """0.20 m at both 06:00 and 07:00 UTC: a strict neighbour test finds no high there."""
        hourly = _hourly("dahab-2026-09-15-marine.json")
        reading = tide(hourly["time"], hourly["sea_level_height_msl"], _at(2026, 9, 15, 7, 0))

        assert reading is not None
        assert reading.previous is not None and reading.next is not None
        assert reading.previous.kind is ExtremeKind.HIGH
        assert datetime.fromtimestamp(reading.previous.at, UTC) == datetime(2026, 9, 15, 6, 30, tzinfo=UTC)
        assert reading.previous.height == 0.2
        assert reading.next.kind is ExtremeKind.LOW
        assert datetime.fromtimestamp(reading.next.at, UTC) == datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

    def test_thirty_minutes_from_an_extreme_is_still_that_extreme(self) -> None:
        """The boundary is inclusive: the dive at 07:00 UTC is exactly half an hour after the high."""
        hourly = _hourly("dahab-2026-09-15-marine.json")
        reading = tide(hourly["time"], hourly["sea_level_height_msl"], _at(2026, 9, 15, 7, 0))
        assert reading is not None
        assert reading.state is TideState.HIGH
        assert reading.height == 0.2

    def test_between_extremes_it_is_rising_or_falling(self) -> None:
        hourly = _hourly("dahab-2026-09-15-marine.json")
        falling = tide(hourly["time"], hourly["sea_level_height_msl"], _at(2026, 9, 15, 9, 30))
        rising = tide(hourly["time"], hourly["sea_level_height_msl"], _at(2026, 9, 15, 15, 0))
        assert falling is not None and falling.state is TideState.FALLING
        assert rising is not None and rising.state is TideState.RISING
        assert falling.height == pytest.approx((0.02 - 0.1) / 2)

    def test_monterey_falls_within_the_hour_of_noaa(self) -> None:
        """NOAA predicts H 07:44, L 12:28, H 17:41 local (PDT) on 2026-10-18; the model's
        extremes sit at 07:00, 10:30 (a plateau) and 16:00."""
        hourly = _hourly("monterey-2026-10-18-marine.json")
        noon = _at(2026, 10, 18, 12, 0, offset_hours=-7)
        reading = tide(hourly["time"], hourly["sea_level_height_msl"], noon)
        assert reading is not None and reading.previous is not None and reading.next is not None
        pdt = timezone(timedelta(hours=-7))
        assert reading.previous.kind is ExtremeKind.LOW
        assert datetime.fromtimestamp(reading.previous.at, pdt).strftime("%H:%M") == "10:30"
        assert reading.next.kind is ExtremeKind.HIGH
        assert datetime.fromtimestamp(reading.next.at, pdt).strftime("%H:%M") == "16:00"
        assert reading.state is TideState.RISING

    def test_an_all_null_series_has_no_tide(self) -> None:
        """2019 is before the marine model's start: every sample is null."""
        hourly = _hourly("dahab-2019-06-10-marine.json")
        assert tide(hourly["time"], hourly["sea_level_height_msl"], _at(2019, 6, 10, 8, 0)) is None

    def test_the_edge_of_a_gap_gives_a_height_and_no_state(self) -> None:
        times = [hour * 3600 for hour in range(8)]
        levels: list[float | None] = [None, None, 0.1, 0.3, 0.5, 0.4, 0.2, 0.1]
        reading = tide(times, levels, 3 * 3600)
        assert reading is not None
        assert reading.height == 0.3
        assert reading.previous is None
        assert reading.state is None

    def test_an_extreme_across_a_gap_is_not_paired(self) -> None:
        times = [hour * 3600 for hour in range(9)]
        levels: list[float | None] = [0.1, 0.5, 0.1, None, 0.2, 0.3, 0.4, 0.3, 0.2]
        reading = tide(times, levels, 5 * 3600)
        assert reading is not None
        assert reading.previous is None
        assert reading.next is not None and reading.next.kind is ExtremeKind.HIGH
        assert reading.state is None
