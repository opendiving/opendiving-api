"""Regenerate `src/app/data/marine_areas.geojson` from Natural Earth.

Run by hand, not by anything automated - the vendored file is the artefact under version
control, and the point of checking it in is that a self-hoster needs no network, no key and
no spatial database to have an offshore pin answer "Red Sea" (see `services.marine_areas`).

    uv run python scripts/build_marine_areas.py

What it strips, and why the stripping is safe: the source carries 30-odd translated `name_*`
columns, `wikidataid`, label placement hints and full cartographic precision, none of which
this app reads. Coordinates are rounded to `_PRECISION` decimals because these are
generalised polygons used as a *coarse* fallback - a sea boundary is a cartographer's
convention, not a survey line, so a metre either way is noise, and the rounding is what
takes the file from 1.7 MB to 1.1 MB.

The source is public domain (Natural Earth), which is the reason it can be vendored at all.
"""

import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

_SOURCE_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
SOURCE_URL = f"{_SOURCE_BASE}/ne_10m_geography_marine_polys.geojson"

_OUTPUT = Path(__file__).resolve().parent.parent / "src" / "app" / "data" / "marine_areas.geojson"

# ~11 m at the equator. Far finer than the data deserves, and still takes about a third
# off the file. Three decimals saves a further 110 KiB and collapses one small feature out
# of existence entirely, which is a bad trade for a tenth of a megabyte.
_PRECISION = 4

_DOWNLOAD_TIMEOUT_SECONDS = 60


def _name(properties: dict[str, Any]) -> str | None:
    """Natural Earth shouts two of the ocean names - `SOUTHERN OCEAN`, `INDIAN OCEAN` - while
    every other row is title case. This value is headed for `dive_site.location`, so it is
    normalized here rather than left for a client to guess at."""
    name = properties.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    return name.title() if name.isupper() else name


def _ring(coordinates: list[Any]) -> list[list[float]] | None:
    """A rounded ring, or `None` if rounding collapsed it into something that is no longer a
    polygon. Consecutive duplicates are dropped because at four decimals a handful of vertices
    in the densest coastlines round onto each other, and a ring carrying its own vertices
    twice is just a bigger file - the crossing test ignores a zero-length edge either way,
    since both its endpoints sit on the same side of any ray."""
    rounded: list[list[float]] = []
    for point in coordinates:
        vertex = [round(float(point[0]), _PRECISION), round(float(point[1]), _PRECISION)]
        if not rounded or rounded[-1] != vertex:
            rounded.append(vertex)

    if not rounded:
        return None
    if rounded[0] != rounded[-1]:
        rounded.append(rounded[0])
    return rounded if len(rounded) >= 4 else None


def _geometry(geometry: dict[str, Any]) -> dict[str, Any] | None:
    parts = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]

    kept: list[list[list[list[float]]]] = []
    for part in parts:
        # The outer ring is taken separately rather than filtered alongside the holes: a
        # collapsed hole is a rounding artefact of an island a few metres across, and
        # dropping it only means the island reads as sea - but a collapsed *outer* ring
        # would promote the first surviving hole into its place, and `marine_areas` would
        # read that hole as the sea's own boundary.
        outer = _ring(part[0])
        if outer is None:
            continue
        holes = [hole for hole in (_ring(ring) for ring in part[1:]) if hole is not None]
        kept.append([outer, *holes])

    if not kept:
        return None
    if len(kept) == 1:
        return {"type": "Polygon", "coordinates": kept[0]}
    return {"type": "MultiPolygon", "coordinates": kept}


def _antimeridian_is_already_handled(features: list[dict[str, Any]]) -> bool:
    """Checked here so `services.marine_areas` can stay flat-plane arithmetic.

    Every sea that genuinely straddles ±180 - the Bering, Chukchi, Ross - arrives from the
    source already **split into separate parts** at the meridian, so each part is an ordinary
    lon/lat rectangle and ray casting needs no wrap-around case. The two exceptions are
    circumpolar and only look like wrapping: the Arctic and Southern Oceans are drawn as
    single rings that run the full 360°, which is a correct plate carrée rendering of a
    region that really does encircle a pole.

    So the thing that must not appear is an *edge* jumping the meridian anywhere but along
    the top or bottom of the map, which is what a ring stitched across the seam would look
    like - and what would let one sea's polygon swallow half a hemisphere.
    """
    for feature in features:
        geometry = feature["geometry"]
        parts = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        for ring in (ring for part in parts for ring in part):
            for (lon, lat), (next_lon, next_lat) in zip(ring, ring[1:], strict=False):
                if abs(next_lon - lon) > 180 and not (abs(lat) == 90 and abs(next_lat) == 90):
                    name = feature["properties"]["name"]
                    print(f"{name}: an edge crosses the antimeridian at {lat}, {next_lat}", file=sys.stderr)
                    return False
    return True


def main() -> int:
    with urllib.request.urlopen(SOURCE_URL, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        source = json.loads(response.read())

    features = []
    for feature in source["features"]:
        name = _name(feature["properties"])
        geometry = _geometry(feature["geometry"])
        if name is None or geometry is None:
            continue
        features.append({"type": "Feature", "properties": {"name": name}, "geometry": geometry})

    if not _antimeridian_is_already_handled(features):
        return 1

    document = {
        "type": "FeatureCollection",
        "source": SOURCE_URL,
        "retrieved": date.today().isoformat(),
        "licence": "Natural Earth, public domain. https://www.naturalearthdata.com/about/terms-of-use/",
        "generated_by": "scripts/build_marine_areas.py",
        "features": features,
    }
    # Written beside the target and moved into place, because `docker-compose.yml` bind-mounts
    # `./src/app` straight into the running container: a plain in-place write means a request
    # arriving mid-regeneration reads a half-written file. `os.replace` is atomic within a
    # filesystem, so a reader sees either the old file or the new one.
    staged = _OUTPUT.with_suffix(".geojson.tmp")
    staged.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(staged, _OUTPUT)
    print(f"{_OUTPUT}: {len(features)} features, {_OUTPUT.stat().st_size / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
