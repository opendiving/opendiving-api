"""Dive-computer files written inline, for tests about recordings rather than about a format.

A Suunto app JSON export, because it is the one format the reader converts that states a
start with its offset, a serial and a device's CNS clock in a few lines of text - which is
what keeps a test about where a file lands, or what fills what, from being a test of a
format. What a real file of any format reads as is `tests/fixtures/dive_files/`'s job.
"""

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

# The Suunto app's activity type for a dive; anything else is an activity the reader skips.
DIVE_ACTIVITY = 51


def suunto_json(
    *,
    start: str = "2026-09-08T15:17:38.670+03:00",
    duration: int = 1800,
    max_depth: float = 19.04,
    serial: str = "253810000400",
    name: str = "Suunto Ocean",
    cns_end: float | None = None,
    gases: Sequence[dict[str, Any]] = (),
    samples: Sequence[tuple[float, float] | tuple[float, float, int, int]] = (),
    activity: int = DIVE_ACTIVITY,
) -> bytes:
    """A minimal Suunto app JSON export of one dive.

    `gases` are `Header.Diving.Gases[]` entries in the file's own SI units - an `Oxygen`
    fraction, pressures in Pascal. `samples` are `(seconds from the start, depth in metres)`,
    or `(seconds, depth, gas number, pressure in Pascal)` for a sample carrying a
    transmitter's reading - which, with no `gases`, is the Ocean's shape: the reader lists a
    cylinder per transmitting slot and labels each by its position from 0. `cns_end` is a
    percentage, written as the fraction the export states.
    """
    origin = datetime.fromisoformat(start)
    diving: dict[str, Any] = {}
    if cns_end is not None:
        diving["EndTissue"] = {"CNS": cns_end / 100}
    if gases:
        diving["Gases"] = list(gases)
    header: dict[str, Any] = {
        "ActivityType": activity,
        "DateTime": start,
        "Depth": {"Max": max_depth},
        "Device": {"Name": name, "SerialNumber": serial},
        "Duration": duration,
    }
    if diving:
        header["Diving"] = diving
    return json.dumps(
        {
            "DeviceLog": {
                "Header": header,
                "Samples": [_sample(origin, sample) for sample in samples],
            }
        }
    ).encode()


def _sample(origin: datetime, sample: tuple[float, float] | tuple[float, float, int, int]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "Depth": sample[1],
        "TimeISO8601": (origin + timedelta(seconds=sample[0])).isoformat(timespec="milliseconds"),
    }
    if len(sample) == 4:
        entry["Cylinders"] = [{"GasNumber": sample[2], "Pressure": sample[3]}]
    return entry
