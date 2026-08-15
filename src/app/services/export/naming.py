"""The labels an export has to spell out that nothing in the database stores.

Three of them, and they exist because the export is the first server-side consumer of
strings the web client has always derived for itself: a gas needs a name in UDDF's
`<mix>` (the element is `namedType`, so the name is not optional), a trip's places have to
collapse onto one line wherever the format has a single location slot, and the download
itself needs a filename. Paths *inside* the archive are `paths.py`.
"""

import re
from collections.abc import Iterable
from datetime import date

from ...schemas.trip import TripLocationRead

# Air is 20.9 % oxygen, devices variously record 20.9, 20.99 or 21, and divers call all
# of them air. Mirrors `AIR_OXYGEN_MIN`/`AIR_OXYGEN_MAX`/`OXYGEN_MIN` in the web client's
# `lib/dive-mixtures.ts` - the two have to agree, because a diver comparing the app to
# their own export should not find the same cylinder named two ways.
_AIR_OXYGEN_MIN = 20.5
_AIR_OXYGEN_MAX = 21.4
_OXYGEN_MIN = 99.5


def gas_name(oxygen: float, helium: float) -> str:
    """What a diver would call this gas: `Air`, `Oxygen`, `EAN32`, or `21/35` for trimix.

    Rounds to whole percent because the shorthand *is* integer shorthand - a 32.4 % fill
    is an EAN32 on every cylinder sticker - and because this is only ever a label: the
    exact fractions travel unrounded in `export.json` and in the CSV alongside it, and
    UDDF carries them in `<o2>`/`<he>` next to this name.

    A mixture the constraints should have rejected (oxygen and helium summing past 100,
    or no oxygen at all) is spelled out rather than named, so an impossible gas cannot
    pass for a real one in a file someone imports elsewhere.
    """
    if not (oxygen > 0 and helium >= 0 and oxygen + helium <= 100):
        return f"O2 {oxygen:g}% / He {helium:g}%"
    if helium > 0:
        return f"{round(oxygen)}/{round(helium)}"
    if oxygen >= _OXYGEN_MIN:
        return "Oxygen"
    if _AIR_OXYGEN_MIN <= oxygen <= _AIR_OXYGEN_MAX:
        return "Air"
    return f"EAN{round(oxygen)}"


def trip_location_names(locations: Iterable[TripLocationRead]) -> str:
    """A trip's places on one line: `Moalboal, Bohol`.

    Only the flat formats need this. `export.json` carries the locations structured, but
    UDDF's `<geography><location>` is a single string and `trips.csv` has a single
    `location` cell, so both have to render the list the way the app does - and rendering
    it twice is how the two would end up disagreeing.

    Comma-joined rather than the `;` the CSV uses for its lists (dive sites, cylinders):
    this is one prose location line, not a set of records folded into a cell, and it is
    the string a reader would expect to see in a "where did you go" column.
    """
    return ", ".join(location.name for location in locations)


def export_filename(username: str, exported_on: date, extension: str) -> str:
    """`opendiving-<username>-<YYYYMMDD>.<ext>`.

    `username` is already constrained to `^[a-z0-9]+$` by `UserUpdate`, so this cannot
    normally produce anything a header or a filesystem would object to - but the
    admin panel writes the column too, so the value is scrubbed rather than trusted.
    """
    safe = re.sub(r"[^a-z0-9]+", "-", username.lower()).strip("-") or "export"
    return f"opendiving-{safe}-{exported_on:%Y%m%d}.{extension}"
