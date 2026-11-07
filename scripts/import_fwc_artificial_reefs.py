"""Write the sunk vessels of Florida's artificial reef register as a batch for
`build_commons_bundle.py`.

Run by hand and hand the output to the builder, as `CONTRIBUTING.md` says:

    uv run python scripts/import_fwc_artificial_reefs.py vessels.geojson

The register holds a row per deployment, and no grouping of its rubble and pipe turns them into
sites a diver picks, so a vessel is what enters. A barge is left out for the same reason: most are
a flat deck among other material. `Depth` and `Relief` are in feet, as the layer's aliases say.

The register places itself in the public domain, and its metadata travels with the repository's
copy of the data, which the credit links.

Candidates are written oldest first, so where two vessels at a site share a name the builder keeps
the first sunk. `DDate` is `YYYYMMDD` as a number, and the register writes 0 for a date it does not know.
"""

import argparse
import re
import sys
import urllib.error
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from curated_source import candidate, fetch_layer, metres, one_line, position_of, write_batch

_LAYER = "https://gis.myfwc.com/mapping/rest/services/Open_Data/Artificial_Reef_Locations_in_Florida/MapServer/12"
# Narrowed at the service because the whole register is past the layer's page.
_VESSELS = "MatCat='Vessel'"

_TOKEN = "fwc-artificial-reefs"
_LICENSE = "LicenseRef-FWC-PD"

_MAX_DEPTH_METRES = 60.0

_NOT_A_VESSEL = "not a vessel"
_A_BARGE = "a barge"
_NO_DEPTH = "no depth above 0"
_TOO_DEEP = "deeper than 60 m"
_NOT_A_NAME = "a permit or site code"
_BY_NAME = "left out by name"
_FLOORS = (_NOT_A_VESSEL, _A_BARGE, _NO_DEPTH, _TOO_DEEP, _NOT_A_NAME, _BY_NAME)

# Vessels that should stay out and that no floor says so of, by deployment, each with its reason.
# The builder's rule misses these: the names differ, and the pins lie too far apart for it to look.
_LEFT_OUT_BY_NAME = {
    "DU0071": "OSJ Site - Drydocks, the 615 ft drydock SJ0016 holds, 1 km away and dated later",
    "MO0024": "Cayman Salvage Master, the wreck the Florida Keys register holds as Cayman Salvor",
    "MO0062": "Hoyt Vandenberg, the pool's USNS General Hoyt S. Vandenberg, 1.1 km from this pin",
}

# A permit's letters and number, `MB 218`, or the county's numbered site, `Site #3_1995`.
_CODE = re.compile(r"[A-Z]+[ -]?\d+|Site #\d+(_\d+)?")


def is_a_name(attributes: dict[str, Any]) -> bool:
    """A hull number reads as a code and is still the vessel's name, which the register then quotes."""
    name = one_line(attributes["Name"] or "")
    if not name:
        return False
    return _CODE.fullmatch(name) is None or f'"{name}"' in (attributes["Description"] or "")


def _floor_of(attributes: dict[str, Any]) -> str | None:
    """The first thing that leaves a row out, or None."""
    if attributes["MatCat"] != "Vessel":
        return _NOT_A_VESSEL
    if "barge" in (attributes["MatDescrip"] or "").casefold():
        return _A_BARGE
    if not attributes["Depth"] or attributes["Depth"] <= 0:
        return _NO_DEPTH
    if metres(attributes["Depth"]) > _MAX_DEPTH_METRES:
        return _TOO_DEEP
    if not is_a_name(attributes):
        return _NOT_A_NAME
    return _BY_NAME if attributes["DeployID"] in _LEFT_OUT_BY_NAME else None


def _oldest_first(feature: dict[str, Any]) -> tuple[bool, int, str]:
    attributes = feature["attributes"]
    deployed = attributes["DDate"] or 0
    return deployed == 0, deployed, attributes["DeployID"]


def _depths_of(attributes: dict[str, Any]) -> tuple[float | None, float]:
    deepest = metres(attributes["Depth"])
    top = deepest - metres(attributes["Relief"] or 0)
    return (top if round(top, 1) > 0 else None), deepest


def candidates(features: list[dict[str, Any]], retrieved: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    """The vessels, oldest first, and the count each floor left out. A row is counted against the
    first floor it fails."""
    left_out: Counter[str] = Counter()
    kept = []
    for feature in sorted(features, key=_oldest_first):
        attributes = feature["attributes"]
        if (floor := _floor_of(attributes)) is not None:
            left_out[floor] += 1
            continue
        kept.append(
            candidate(
                _TOKEN,
                attributes["DeployID"],
                one_line(attributes["Name"]),
                position_of(feature),
                license=_LICENSE,
                retrieved=retrieved,
                depths=_depths_of(attributes),
            )
        )
    return kept, left_out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write Florida's sunk vessels as a batch.")
    parser.add_argument("output", type=Path, help="Where to write the batch")
    arguments = parser.parse_args(argv)

    try:
        features = fetch_layer(_LAYER, _VESSELS)
        kept, left_out = candidates(features, date.today().isoformat())
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"could not read the register: {exc}", file=sys.stderr)
        return 1

    write_batch(arguments.output, kept)
    print(f"{len(features)} vessels")
    for floor in _FLOORS:
        print(f"  {left_out[floor]:>5} {floor}")
    for deployment, reason in _LEFT_OUT_BY_NAME.items():
        print(f"        {deployment}: {reason}")
    print(f"{arguments.output}: {len(kept)} candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
