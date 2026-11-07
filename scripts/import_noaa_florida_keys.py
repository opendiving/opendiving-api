"""Write the moored sites of the Florida Keys National Marine Sanctuary as a batch for
`build_commons_bundle.py`.

Run by hand and hand the output to the builder, as `CONTRIBUTING.md` says:

    uv run python scripts/import_noaa_florida_keys.py keys.geojson

The register holds a row per buoy, and most of the buoys at a site are mooring buoys. Boundary,
spar and closure buoys mark something else. The file is a working file and is not checked in: the
bundle's diff is the batch.

A US federal work carries no copyright. Candidates are written in the identifier's order.
"""

import argparse
import math
import statistics
import sys
import urllib.error
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from curated_source import candidate, fetch_layer, metres, one_line, position_of, refuse_repeats, slug, write_batch

_LAYER = "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/PubFKNMSBuoy2025/FeatureServer/0"

_TOKEN = "noaa-florida-keys-moorings"
_LICENSE = "LicenseRef-US-Gov-PD"

_NOT_A_MOORING = "not a mooring buoy"
_BY_NAME = "left out by name"

# Sites that should stay out and that no floor says so of, by identifier, each with its reason.
_LEFT_OUT_BY_NAME = {
    "archer-key": "Archer Key, moorings at a key in 2.4 m, which does not read as a dive site",
    "cottrell-key": "Cottrell Key, moorings at a key in 3 to 4.3 m, which does not read as a dive site",
    "grecian": "Grecian, one buoy among the moorings of Grecian Rocks, which is the site",
    "marker-48": "Marker 48, named for a marker in 2.7 to 4.3 m, which does not read as a dive site",
    "mule-key": "Mule Key, a mooring at a key in 2.4 m, which does not read as a dive site",
    "mule-key-2": "Mule Key 2, the next mooring at the same key, 110 m from Mule Key",
}


def _is_a_mooring(attributes: dict[str, Any]) -> bool:
    return (attributes["BuoyType"] or "").strip().startswith("Mooring")


def pin_of(moorings: list[dict[str, Any]]) -> tuple[float, float]:
    """The mooring nearest the median position: always a real buoy, and one misplaced row in the
    register does not move it. Ties go by position, so the order of the rows has no say."""
    positions: list[tuple[float, float]] = [position_of(mooring) for mooring in moorings]
    longitude = statistics.median(position[0] for position in positions)
    latitude = statistics.median(position[1] for position in positions)
    squeeze = math.cos(math.radians(latitude))

    def distance(position: tuple[float, float]) -> float:
        return math.hypot((position[0] - longitude) * squeeze, position[1] - latitude)

    return min(positions, key=lambda position: (distance(position), position))


def _depths_of(moorings: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    feet = [mooring["attributes"]["Depth__ft_"] for mooring in moorings]
    known = [depth for depth in feet if depth is not None]
    return (metres(min(known)), metres(max(known))) if known else (None, None)


def candidates(features: list[dict[str, Any]], retrieved: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    """The sites, in the order of their identifier, and the count left out: rows that are no
    mooring, and sites left out by name."""
    left_out: Counter[str] = Counter()
    sites: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for feature in features:
        if _is_a_mooring(feature["attributes"]):
            sites[one_line(feature["attributes"]["Location"])].append(feature)
        else:
            left_out[_NOT_A_MOORING] += 1

    named = sorted((slug(name), name) for name in sites)
    refuse_repeats(identifier for identifier, _ in named)
    left_out[_BY_NAME] = sum(1 for identifier, _ in named if identifier in _LEFT_OUT_BY_NAME)
    return [
        candidate(
            _TOKEN,
            identifier,
            name,
            pin_of(sites[name]),
            license=_LICENSE,
            retrieved=retrieved,
            depths=_depths_of(sites[name]),
        )
        for identifier, name in named
        if identifier not in _LEFT_OUT_BY_NAME
    ], left_out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the Florida Keys sanctuary's moored sites as a batch.")
    parser.add_argument("output", type=Path, help="Where to write the batch")
    arguments = parser.parse_args(argv)

    try:
        features = fetch_layer(_LAYER)
        kept, left_out = candidates(features, date.today().isoformat())
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"could not read the buoys: {exc}", file=sys.stderr)
        return 1

    write_batch(arguments.output, kept)
    print(f"{len(features)} buoys")
    print(f"  {left_out[_NOT_A_MOORING]:>5} {_NOT_A_MOORING}")
    print(f"  {left_out[_BY_NAME]:>5} {_BY_NAME}")
    for identifier, reason in _LEFT_OUT_BY_NAME.items():
        print(f"        {identifier}: {reason}")
    print(f"{arguments.output}: {len(kept)} candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
