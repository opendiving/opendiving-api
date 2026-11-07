"""Write the moored sites of the Thunder Bay National Marine Sanctuary as a batch for
`build_commons_bundle.py`.

Run by hand and hand the output to the builder, as `CONTRIBUTING.md` says:

    uv run python scripts/import_noaa_thunder_bay.py thunder-bay.geojson

The layer is the sanctuary's buoy status map, so a row's `Name` says whether the buoy is in the
water this season and its `Icon_Title` is the site. The depth sits in the HTML of its
`Description`, and a row with none is the one about the map. No depth floor: the sanctuary moors
these sites for divers, the shallow ones included.

A US federal work carries no copyright. Candidates are written in the identifier's order.
"""

import argparse
import re
import sys
import urllib.error
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from curated_source import candidate, fetch_layer, metres, one_line, position_of, refuse_repeats, slug, write_batch

_LAYER = (
    "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/"
    "arcgisOnline_BuoysOnly_Update20230703/FeatureServer/0"
)

_TOKEN = "noaa-thunder-bay-moorings"
_LICENSE = "LicenseRef-US-Gov-PD"

_NO_DEPTH = "no depth in its description"

_TAG = re.compile(r"<[^>]+>")
_DEPTH = re.compile(r"Depth:\s*(\d+(?:\.\d+)?)\s*Feet", re.IGNORECASE)


def depth_of(description: str | None) -> float | None:
    """The metres a row's description says, or None."""
    match = _DEPTH.search(_TAG.sub(" ", description or ""))
    return None if match is None else metres(float(match[1]))


def candidates(features: list[dict[str, Any]], retrieved: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    """The sites, in the order of their identifier, and the count of rows left out."""
    left_out: Counter[str] = Counter()
    sites = []
    for feature in features:
        attributes = feature["attributes"]
        depth = depth_of(attributes["Description"])
        if depth is None:
            left_out[_NO_DEPTH] += 1
            continue
        name = one_line(attributes["Icon_Title"])
        sites.append((slug(name), name, position_of(feature), depth))

    sites.sort(key=lambda site: site[0])
    refuse_repeats(identifier for identifier, *_ in sites)
    return [
        candidate(
            _TOKEN,
            identifier,
            name,
            position,
            license=_LICENSE,
            retrieved=retrieved,
            depths=(None, depth),
            water_type="fresh",
        )
        for identifier, name, position, depth in sites
    ], left_out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the Thunder Bay sanctuary's moored sites as a batch.")
    parser.add_argument("output", type=Path, help="Where to write the batch")
    arguments = parser.parse_args(argv)

    try:
        features = fetch_layer(_LAYER)
        kept, left_out = candidates(features, date.today().isoformat())
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"could not read the buoys: {exc}", file=sys.stderr)
        return 1

    write_batch(arguments.output, kept)
    print(f"{len(features)} rows")
    print(f"  {left_out[_NO_DEPTH]:>5} {_NO_DEPTH}")
    print(f"{arguments.output}: {len(kept)} candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
