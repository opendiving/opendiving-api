"""Where the diver got in and where they got out, from a file's satellite fixes.

Two formats carry GPS - FIT on its `record` messages, the 2026 Suunto Ocean JSON on its
samples - and both discarded it until now, because there was nowhere to persist it. The
extraction lives here rather than in either parser so the rule that decides *which end of
the dive* a fix belongs to is written once: a third format that logs positions inherits
it instead of re-deriving it.

**A fix is never taken underwater.** GPS does not reach a wrist through seawater, so
every position in a dive log was recorded at the surface, and the only question worth
asking of one is which surface interval it belongs to - the one before the descent or the
one after the ascent. `entry_and_exit` answers that by splitting the fixes on the dive's
own deepest sample: everything before it is on the way in, everything after it is on the
way out.

The corpus is emphatic about why that split matters rather than "first fix, last fix".
Across the 19 Suunto Ocean exports that carry GPS at all, **every single fix falls after
the diver surfaced** - the earliest one on any of them lands at 96 % of the dive's
duration, in the logging tail past `Header.DiveTime`. A parser taking "the first fix" as
the entry point would have written the *exit* position into the entry columns on all 19,
and nothing downstream could have told. So an absent entry is the normal answer for this
device, and saying so is the point.
"""

import math
from dataclasses import dataclass
from typing import NamedTuple

from ...schemas.parsed_dive import LATITUDE_LIMIT, LONGITUDE_LIMIT

# FIT stores an angle as a *semicircle*: a signed 32-bit count of 180/2^31 degrees, so a
# full circle is 2^32. The profile declares no scale factor for `position_lat`/
# `position_long`, and `fitdecode` therefore hands the raw count back untouched.
_DEGREES_PER_SEMICIRCLE = 180 / 2**31

# Decimal places kept on a stored coordinate. Six is ~11 cm at the equator, well inside
# any consumer GPS's error, and it is chosen for a sharper reason than tidiness: the same
# dive exported as FIT and as Suunto JSON reaches this module through two different
# conversions - a semicircle count and a radian float - and the two agree exactly at six
# places (28.437455, 34.458997) while disagreeing in the digits below. Rounding here is
# what stops one dive imported twice from looking like two positions.
_COORDINATE_PLACES = 6


@dataclass(frozen=True, slots=True)
class GeoFix:
    """One satellite fix: decimal degrees, plus whatever the file orders its samples by.

    `at` is only ever compared against other numbers from the same file, so its epoch
    doesn't matter - the FIT parser passes POSIX seconds off a `datetime`, the JSON
    parser the same off its own ISO timestamps.
    """

    at: float
    latitude: float
    longitude: float


class EntryExit(NamedTuple):
    """The two fixes a dive gets to keep, either of which may be absent."""

    entry: GeoFix | None
    exit: GeoFix | None


NO_POSITIONS = EntryExit(entry=None, exit=None)


def degrees_from_semicircles(value: object) -> float | None:
    """A FIT angle in decimal degrees, or `None` where the field held no fix.

    `fitdecode` already resolves the format's own absent-marker: `position_lat` is a
    `sint32`, whose invalid sentinel is 0x7FFFFFFF, and the base type's parser returns
    `None` for it rather than the 180.000000 degrees the arithmetic would produce. That
    matters most for longitude, where 180 is a real, in-range value that no range check
    would ever catch.
    """
    return value * _DEGREES_PER_SEMICIRCLE if isinstance(value, int) and not isinstance(value, bool) else None


def degrees_from_radians(value: object) -> float | None:
    """A Suunto JSON angle in decimal degrees.

    The export writes `Latitude`/`Longitude` in **radians**, which is invisible from one
    file - 0.496 and 0.601 are perfectly plausible coordinates off the Gulf of Guinea -
    and settled by the corpus: `69cfaef7` exists as both a FIT and a JSON export of one
    dive, and the radians convert to exactly the degrees the FIT semicircles do (28.4374,
    34.4590, in the Gulf of Aqaba). Read as degrees they would pin a Dahab dive into the
    Atlantic, 3 000 km away, which is the same failure Null Island is guarded against
    below.
    """
    return math.degrees(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def geo_fix(at: float, latitude: float | None, longitude: float | None) -> GeoFix | None:
    """One fix, or `None` when the file recorded nothing usable at this sample.

    Filtered here as well as on `ParsedDiveSchema`, and the two are doing different jobs.
    The schema is the backstop for what reaches the columns; this is what stops a junk
    fix from *displacing a real one* - `entry_and_exit` picks the fix closest to the
    dive, so a `0.0, 0.0` sample logged while the receiver was still cold would be chosen
    over the good fix beside it and then nulled by the schema, costing a position the
    file actually recorded.

    **Exactly `0.0, 0.0` is not a position.** Recorded already in DECISIONS.md from the
    divelogs.de importer: Null Island is a place, and an importer that trusts it pins a
    Red Sea wreck into the Atlantic. A receiver with no lock reports the origin, and both
    formats can express it - FIT as two literal zero semicircles, JSON as two zero
    radians.
    """
    if latitude is None or longitude is None:
        return None
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None
    if abs(latitude) > LATITUDE_LIMIT or abs(longitude) > LONGITUDE_LIMIT:
        return None
    if latitude == 0.0 and longitude == 0.0:
        return None
    return GeoFix(
        at=at,
        latitude=round(latitude, _COORDINATE_PLACES),
        longitude=round(longitude, _COORDINATE_PLACES),
    )


def entry_and_exit(fixes: list[GeoFix], depths: list[tuple[float, float]]) -> EntryExit:
    """Split fixes on the deepest sample: the last one before it, the first one after.

    "Last before" and "first after" rather than "first" and "last" because the fix that
    describes where a diver got in is the one taken just before they descended, not the
    one from when the boat left the jetty. The same argument, mirrored, for the exit.

    The deepest sample is the pivot in preference to an in-water *window*, which would
    need a depth threshold this module would have to invent - `FitParser._tank_pressures`
    declines to invent one for the same reason, and FIT has no in-water time to read
    instead (`total_elapsed_time` covers the surface tail too). The deepest point needs
    no threshold at all: it is wherever the depth channel peaks, and "before the deepest
    point" is a plain reading of "on the way in".

    **Neither the fixes nor the depths may be assumed sorted.** A Suunto export's sample
    timestamps are not monotonic across channels - adjacent entries go backwards by up to
    0.7 s, because the separate sensor streams are appended out of order - so this scans
    for its extremes rather than indexing the ends of the lists.

    With no depth channel there is no pivot and so no answer: a file that recorded
    positions but never a depth cannot say which of them is the entry, and inventing an
    order would be guessing. Returns nothing, rather than the half-truth.
    """
    if not fixes or not depths:
        return NO_POSITIONS

    # `max` keeps the first of equal values, so a flat-bottomed profile pivots on the
    # moment the diver first reached the deepest reading rather than the last.
    deepest_at = max(depths, key=lambda point: point[1])[0]

    before = [fix for fix in fixes if fix.at < deepest_at]
    after = [fix for fix in fixes if fix.at >= deepest_at]
    return EntryExit(
        entry=max(before, key=lambda fix: fix.at) if before else None,
        exit=min(after, key=lambda fix: fix.at) if after else None,
    )
