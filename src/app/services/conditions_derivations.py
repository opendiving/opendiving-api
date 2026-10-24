"""What a dive's surface conditions are, read off a provider's hourly series.

Pure functions over parsed series, so each rule is tested on recorded answers without a
provider. Everything here is versioned together by `DERIVATION_VERSION`: a change to the WMO
table, the wave thresholds or the tide rules bumps it, and every row written under the old
one is refilled from its answer (`services.dive_conditions`) rather than having its marks
cleared by a sweep that re-derives.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..schemas.dive import Waves, Weather

DERIVATION_VERSION = 1

# Open-Meteo's `weather_code`, WMO 4677 as its documentation publishes it, onto the dive's
# `weather` vocabulary. 1 ("mainly clear") is `clear` because that is what a diver calls a
# sky with a cloud or two. 97 is in the documentation and not in the API's own enum, and the
# hail codes come only from models that forecast hail, so 95 to 99 are one band. A code not
# listed fills nothing.
_WEATHER_BY_WMO_CODE: dict[int, Weather] = {
    0: Weather.CLEAR,
    1: Weather.CLEAR,
    2: Weather.PARTLY_CLOUDY,
    3: Weather.OVERCAST,
    45: Weather.FOG,
    48: Weather.FOG,
    **dict.fromkeys((51, 53, 55, 56, 57), Weather.RAIN),
    **dict.fromkeys((61, 63, 65, 66, 67), Weather.RAIN),
    **dict.fromkeys((80, 81, 82), Weather.RAIN),
    **dict.fromkeys((71, 73, 75, 77), Weather.SNOW),
    **dict.fromkeys((85, 86), Weather.SNOW),
    **dict.fromkeys((95, 96, 97, 99), Weather.STORM),
}

# The Douglas sea scale on significant wave height, in metres, each the lower bound of a band.
# Below 0.5 is degrees 0 to 2 (glassy, rippled, smooth); 3 is slight, 4 moderate, and 5 and
# up all land on `rough`, the dive vocabulary's top value.
_DOUGLAS_SLIGHT = 0.5
_DOUGLAS_MODERATE = 1.25
_DOUGLAS_ROUGH = 2.5

# How close a dive's instant has to be to a high or low, inclusive, to be called that rather
# than rising or falling.
TIDE_SLACK_SECONDS = 30 * 60


class TideState(StrEnum):
    HIGH = "high"
    LOW = "low"
    RISING = "rising"
    FALLING = "falling"


class ExtremeKind(StrEnum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Extreme:
    kind: ExtremeKind
    # Unix seconds. The midpoint of a plateau can fall on a half hour.
    at: int
    height: float


@dataclass(frozen=True, slots=True)
class Tide:
    height: float | None
    state: TideState | None
    previous: Extreme | None
    next: Extreme | None


def weather_from_code(code: int | None) -> Weather | None:
    return None if code is None else _WEATHER_BY_WMO_CODE.get(code)


def waves_from_height(height: float | None) -> Waves | None:
    if height is None:
        return None
    if height < _DOUGLAS_SLIGHT:
        return Waves.CALM
    if height < _DOUGLAS_MODERATE:
        return Waves.SLIGHT
    if height < _DOUGLAS_ROUGH:
        return Waves.MODERATE
    return Waves.ROUGH


def nearest_sample(times: Sequence[int], instant: int) -> int | None:
    """The index of the sample nearest `instant`, the earlier of two equally near, or `None`
    for an empty series. Open-Meteo's hourly values are instantaneous for temperature, wind,
    pressure and cloud, and the preceding hour's sum for precipitation."""
    if not times:
        return None
    return min(range(len(times)), key=lambda index: (abs(times[index] - instant), times[index]))


def _segments(times: Sequence[int], levels: Sequence[float | None]) -> list[list[tuple[int, float]]]:
    """The series split at its gaps: each run of consecutive samples that all carry a value."""
    segments: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for at, level in zip(times, levels, strict=True):
        if level is None:
            if current:
                segments.append(current)
            current = []
        else:
            current.append((at, level))
    if current:
        segments.append(current)
    return segments


def _extremes(segment: list[tuple[int, float]]) -> list[Extreme]:
    """Every high and low inside one gapless segment.

    A run of one or more equal samples higher than both its neighbours is a high, lower than
    both a low, timed at the run's midpoint - a strict neighbour test would lose a plateaued
    high, and hourly samples straddling a turn produce them. A run touching either end of the
    segment has an unknown neighbour and is no extreme: the edge of a gap says nothing.
    """
    extremes: list[Extreme] = []
    start = 0
    while start < len(segment):
        end = start
        while end + 1 < len(segment) and segment[end + 1][1] == segment[start][1]:
            end += 1
        if start > 0 and end < len(segment) - 1:
            level = segment[start][1]
            before, after = segment[start - 1][1], segment[end + 1][1]
            at = (segment[start][0] + segment[end][0]) // 2
            if level > before and level > after:
                extremes.append(Extreme(ExtremeKind.HIGH, at, level))
            elif level < before and level < after:
                extremes.append(Extreme(ExtremeKind.LOW, at, level))
        start = end + 1
    return extremes


def _interpolate(segment: list[tuple[int, float]], instant: int) -> float | None:
    """The level at `instant`: a sample's own where one falls on it, else linear between the two
    around it."""
    for at, level in segment:
        if at == instant:
            return level
    for (t0, v0), (t1, v1) in zip(segment, segment[1:], strict=False):
        if t0 < instant < t1:
            return v0 + (v1 - v0) * (instant - t0) / (t1 - t0)
    return None


def tide(times: Sequence[int], levels: Sequence[float | None], instant: int) -> Tide | None:
    """The tide at `instant` from an hourly sea-level series, or `None` where the series has no
    value around it - an uncovered cell, a day before the model's start, one of its gaps.

    Only the gapless stretch holding the instant is read, so an extreme on the far side of a
    gap is never paired with one on this side. The height is interpolated linearly; the state
    is `high` or `low` within `TIDE_SLACK_SECONDS` of an extreme, inclusive, else `rising`
    toward a high and `falling` toward a low. A stretch without an extreme on both sides of
    the instant yields the height and no state.
    """
    segment = next((one for one in _segments(times, levels) if one[0][0] <= instant <= one[-1][0]), None)
    if segment is None:
        return None
    extremes = _extremes(segment)
    previous = next((one for one in reversed(extremes) if one.at <= instant), None)
    following = next((one for one in extremes if one.at > instant), None)
    state: TideState | None = None
    if previous is not None and following is not None:
        nearest = min((previous, following), key=lambda one: abs(one.at - instant))
        if abs(nearest.at - instant) <= TIDE_SLACK_SECONDS:
            state = TideState(nearest.kind.value)
        else:
            state = TideState.RISING if following.kind is ExtremeKind.HIGH else TideState.FALLING
    return Tide(height=_interpolate(segment, instant), state=state, previous=previous, next=following)
