"""Build `src/app/data/commons_dive_sites.geojson` from a checkout of the OpenDiving Commons.

Run by hand, not by anything automated, from a clean checkout at the commit to ship - the
repository's `main` before every release, which is how a self-hosted copy is refreshed on
upgrade:

    uv run python scripts/build_commons_bundle.py /path/to/opendiving-commons

A second or two when every record names its locality; a couple of minutes, most of it two
Natural Earth downloads (~54 MB together), when some do not. Nothing is cached between runs.

**The output is an ODbL Derivative Database, and the notice block inside it is a licence
compliance artifact rather than documentation.** ODbL 4.2 requires the notice to travel "within
the data or metadata"; 4.6 requires recipients to be able to obtain the database or the method
of making it, which is why this script is checked in beside its output. Do not remove the
`sources` block: the API credits every record through it, and drops one whose source it does not
name.

The script reads a record in either of the two shapes the repository has held. An OpenStreetMap
seed's English name sits in `extensions.opendivingcommons.name_en`, or in `other_names`; a seed's
registry entry is read off its path, or from `seeded_from` in its provenance. What it writes is
always the second shape, one feature per line so that a refresh reads as a diff of the records it
moved.

Natural Earth supplies a record's locality where the record names none: neither OpenStreetMap nor
Wikidata carries a country or a region. It is public domain, consumed here and never shipped, so
it adds no licence obligation, and it is named in the notice block anyway because that block's
job is to say where every field came from.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

_OUTPUT = Path(__file__).resolve().parent.parent / "src" / "app" / "data" / "commons_dive_sites.geojson"

_REPOSITORY = "https://github.com/opendiving/opendiving-commons"

# The pool's own registry token, and the extension key its provenance sits under.
_REGISTRY = "opendivingcommons"

_NATURAL_EARTH_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
ADMIN_0_URL = f"{_NATURAL_EARTH_BASE}/ne_10m_admin_0_countries.geojson"
ADMIN_1_URL = f"{_NATURAL_EARTH_BASE}/ne_10m_admin_1_states_provinces.geojson"

# GitHub asks for a descriptive agent. It names the project and nobody in it.
_USER_AGENT = "OpenDiving-commons-bundle/1.0 (https://github.com/opendiving/opendiving-api)"

_DOWNLOAD_TIMEOUT_SECONDS = 300

# How far off a boundary a record may sit and still take its country from it. Dive sites are
# in water and admin polygons are land: only 44.5% of the records fall inside any admin-0
# polygon, so containment alone would leave the country empty for the majority of rows while
# every test still passed. Of an offshore sample the median distance to land is ~3 km and 98%
# are within 50 km, so this recovers roughly all of them.
_NEAREST_LIMIT_KM = 50.0

# Degrees per kilometre, for the local planar approximation in `_distance_km`.
_KM_PER_DEGREE_LATITUDE = 110.574
_KM_PER_DEGREE_LONGITUDE = 111.320

# Per-source credits, in the `[label](href)` wire format the clients parse. The
# OpenStreetMap string is **byte-identical** to `geocoding_service._DEFAULT_ATTRIBUTION` on
# purpose: the site form renders Commons rows and geocoder rows in one list under one credit
# line that collapses repeats by exact string, so a second wording for the same licence would
# show a diver the same credit twice. Natural Earth is not here because it supplies no record,
# only the locality on one, and asks for nothing.
_ATTRIBUTION = {
    "openstreetmap": "[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)",
    "wikidata": "[Data from Wikidata, CC0 1.0.](https://www.wikidata.org/wiki/Wikidata:Licensing)",
    "contribution": f"[Data from OpenDiving Commons contributors, CC0 1.0.]({_REPOSITORY})",
}

_ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
_CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/"

Ring = tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class _Boundary:
    """One polygon of one admin area, with its bounding box for a cheap first test."""

    code: str | None
    name: str
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    rings: tuple[Ring, ...]

    @property
    def box_area(self) -> float:
        return (self.max_lon - self.min_lon) * (self.max_lat - self.min_lat)


@dataclass(slots=True)
class _Place:
    """A record with no locality of its own, on its way to having one."""

    latitude: float
    longitude: float
    country_code: str | None = None
    country: str | None = None
    region: str | None = None


def _fetch(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


# -------------- the repository --------------


def _folded(value: str) -> str:
    return value.strip().casefold()


def _seed_entry(path: str) -> dict[str, str]:
    """The registry entry a seed's path names: `sites/<registry>/<identifier>.geojson`."""
    registry, identifier = path.removeprefix("sites/").removesuffix(".geojson").split("/", 1)
    return {"registry": registry, "identifier": identifier}


def bundled(path: str, feature: dict[str, Any]) -> dict[str, Any]:
    """One repository file as the bundle holds it, in the shape the repository publishes.

    An English name that repeats the name, or another name, once trimmed and case-folded is
    dropped rather than moved: another name may not say what a name already says.
    """
    properties = dict(feature["properties"])
    provenance = dict(properties["extensions"][_REGISTRY])

    other_names = list(properties.get("other_names", []))
    name_en = provenance.pop("name_en", None)
    if name_en and _folded(name_en) not in {_folded(name) for name in (properties["name"], *other_names)}:
        other_names.append(name_en)
    properties.pop("other_names", None)
    if other_names:
        properties["other_names"] = other_names

    if provenance["source"] != "contribution" and "seeded_from" not in provenance:
        provenance["seeded_from"] = _seed_entry(path)
    properties["extensions"] = {_REGISTRY: provenance}

    if location := properties.get("location"):
        properties["location"] = {"name": location["name"]}
    return {"type": "Feature", "geometry": feature["geometry"], "properties": properties}


def _read_records(checkout: Path) -> list[dict[str, Any]]:
    sites = checkout / "sites"
    records = []
    for file in sorted(sites.rglob("*.geojson")):
        path = file.relative_to(checkout).as_posix()
        records.append(bundled(path, json.loads(file.read_text(encoding="utf-8"))))
    return records


def _commit_of(checkout: Path) -> str:
    """The commit the checkout stands at, refusing a tree that says something else: the file
    names its commit, and a record edited by hand would make that a false statement."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(checkout), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    if git("status", "--porcelain", "--", "sites"):
        raise ValueError(f"{checkout} has uncommitted changes under sites/")
    return git("rev-parse", "HEAD")


# -------------- Natural Earth --------------


def _distance_km(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    """Local planar approximation, not a haversine, and that is a deliberate choice here.

    Every comparison this makes is against `_NEAREST_LIMIT_KM`, where the equirectangular
    error is under a tenth of a percent, and it runs tens of millions of times against
    polygon vertices.
    """
    mean_latitude = math.radians((latitude_a + latitude_b) / 2)
    north = (latitude_a - latitude_b) * _KM_PER_DEGREE_LATITUDE
    east = (longitude_a - longitude_b) * _KM_PER_DEGREE_LONGITUDE * math.cos(mean_latitude)
    return math.hypot(north, east)


def _ring(coordinates: list[Any]) -> Ring:
    return tuple((float(point[0]), float(point[1])) for point in coordinates)


def _boundaries_of(code: str | None, name: str, geometry: dict[str, Any]) -> Iterator[_Boundary]:
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    for polygon in polygons:
        rings = tuple(ring for ring in (_ring(ring) for ring in polygon) if ring)
        if not rings:
            continue
        longitudes = [longitude for longitude, _ in rings[0]]
        latitudes = [latitude for _, latitude in rings[0]]
        yield _Boundary(
            code=code,
            name=name,
            min_lon=min(longitudes),
            min_lat=min(latitudes),
            max_lon=max(longitudes),
            max_lat=max(latitudes),
            rings=rings,
        )


def _smallest_first(boundaries: list[_Boundary]) -> list[_Boundary]:
    """Ordered so that the first part containing a point is the most specific one holding it.

    An enclave has to beat the country around it - Lesotho over South Africa, Vatican City
    over Italy - and `_resolve` returns on the first containment rather than testing the rest,
    so the ordering *is* the rule. Bounding-box area rather than true polygon area, for the
    reason `services.marine_areas` gives: it needs no geometry library and is not a close call
    for any pair this has to separate.
    """
    return sorted(boundaries, key=lambda boundary: boundary.box_area)


def _iso_a2(value: Any) -> str | None:
    """Natural Earth writes the *string* `-99` where it has no code.

    Read `ISO_A2_EH` and only that. `ISO_A2` is `-99` for 22 of the 258 admin-0 features and
    `ISO_A2_EH` for 13, and those 13 are a strict subset - so falling back from one to the
    other can never recover a code, while reading `ISO_A2` instead silently strips the
    country from every French and Norwegian dive site.
    """
    return value if isinstance(value, str) and value and value != "-99" else None


def _admin_0_boundaries(document: dict[str, Any]) -> list[_Boundary]:
    boundaries: list[_Boundary] = []
    for feature in document["features"]:
        properties = feature["properties"]
        name = properties.get("NAME_EN")
        if not name:
            continue
        boundaries.extend(_boundaries_of(_iso_a2(properties.get("ISO_A2_EH")), name, feature["geometry"]))
    return _smallest_first(boundaries)


def _admin_1_boundaries(document: dict[str, Any]) -> list[_Boundary]:
    boundaries: list[_Boundary] = []
    for feature in document["features"]:
        properties = feature["properties"]
        # `name_en`, never `name`. They differ on 1,265 of the 4,596 features: the unit
        # nearest `SS Thistlegorm` is `Janub Sina'` by `name` and `South Sinai` by `name_en`,
        # and shipping the first reproduces on the region half exactly the local-language
        # defect a locality in English avoids on the country half. The 7 features with no
        # `name_en` have no `name` either, so there is nothing to fall back to.
        name = properties.get("name_en")
        code = _iso_a2(properties.get("iso_a2"))
        if not name or code is None:
            continue
        boundaries.extend(_boundaries_of(code, name, feature["geometry"]))
    return _smallest_first(boundaries)


def _ring_contains(ring: Ring, longitude: float, latitude: float) -> bool:
    """Ray casting - an odd number of crossings east of the point means inside."""
    inside = False
    previous_longitude, previous_latitude = ring[-1]
    for point_longitude, point_latitude in ring:
        if (point_latitude > latitude) != (previous_latitude > latitude):
            crossing = (previous_longitude - point_longitude) * (latitude - point_latitude) / (
                previous_latitude - point_latitude
            ) + point_longitude
            if longitude < crossing:
                inside = not inside
        previous_longitude, previous_latitude = point_longitude, point_latitude
    return inside


def _contains(boundary: _Boundary, longitude: float, latitude: float) -> bool:
    if not (boundary.min_lon <= longitude <= boundary.max_lon and boundary.min_lat <= latitude <= boundary.max_lat):
        return False
    if not _ring_contains(boundary.rings[0], longitude, latitude):
        return False
    return not any(_ring_contains(hole, longitude, latitude) for hole in boundary.rings[1:])


def _nearest_within(boundaries: list[_Boundary], longitude: float, latitude: float) -> _Boundary | None:
    """The closest boundary within `_NEAREST_LIMIT_KM`, measured vertex-to-point.

    Vertex-to-point over-states the true distance to a polygon - a point off a long straight
    coast is nearer the edge than any vertex on it - so this is conservative in the direction
    that matters: a record it resolves really is that close.
    """
    latitude_margin = _NEAREST_LIMIT_KM / _KM_PER_DEGREE_LATITUDE
    # cos() shrinks a degree of longitude towards the poles, so the margin has to grow to
    # match. Clamped because it goes to infinity at the pole itself.
    longitude_margin = latitude_margin / max(math.cos(math.radians(latitude)), 0.01)

    best: _Boundary | None = None
    best_distance = _NEAREST_LIMIT_KM
    for boundary in boundaries:
        if not (
            boundary.min_lon - longitude_margin <= longitude <= boundary.max_lon + longitude_margin
            and boundary.min_lat - latitude_margin <= latitude <= boundary.max_lat + latitude_margin
        ):
            continue
        for ring in boundary.rings:
            for vertex_longitude, vertex_latitude in ring:
                distance = _distance_km(latitude, longitude, vertex_latitude, vertex_longitude)
                if distance < best_distance:
                    best_distance = distance
                    best = boundary
    return best


def _resolve(boundaries: list[_Boundary], longitude: float, latitude: float) -> _Boundary | None:
    """Containment first, then nearest within 50 km, then nothing.

    The second rule is not an optimisation. Dive sites are in water and admin polygons are
    land: only 44.5% of the records fall inside any admin-0 polygon, so a generator written
    to containment alone ships an empty country for more than half its rows - and does it
    while every unit test passes, because each one names a site that happens to be inshore.
    """
    for boundary in boundaries:
        if _contains(boundary, longitude, latitude):
            return boundary
    return _nearest_within(boundaries, longitude, latitude)


def _resolve_places(records: list[_Place], admin_0: list[_Boundary], admin_1: list[_Boundary]) -> None:
    """Fill `country_code`, `country` and `region` in place. Any of the three may stay null.

    **Admin-0 is resolved first and admin-1 is then restricted to that country.** The two
    layers are separately generalised outlines and most records resolve by *nearest* rather
    than containment, so run independently they disagree: in the Gulf of Aqaba, Egypt,
    Israel, Jordan and Saudi Arabia are all within 20 km of each other, and a record could
    take its country from one and its region from another's province - shipping
    `Tabuk, Egypt`. Where the restricted set has nothing within 50 km the region is null
    rather than a neighbour's.

    A country Natural Earth has no ISO code for - Somaliland, Northern Cyprus and eleven
    others - therefore resolves to a country name and no region, since there is no code to
    restrict admin-1 by. That is the conservative answer and the hint reads fine without it.
    """
    by_country: dict[str, list[_Boundary]] = {}
    for boundary in admin_1:
        if boundary.code is not None:
            by_country.setdefault(boundary.code, []).append(boundary)

    for record in records:
        country = _resolve(admin_0, record.longitude, record.latitude)
        if country is None:
            continue
        record.country_code = country.code
        record.country = country.name
        if country.code is None:
            continue
        region = _resolve(by_country.get(country.code, []), record.longitude, record.latitude)
        record.region = region.name if region is not None else None


# -------------- output --------------


def locality_name(place: _Place) -> str | None:
    """`region, country`, in English, and never an ISO code: this is a place's own name."""
    return ", ".join(part for part in (place.region, place.country) if part) or None


def _place_records(records: list[dict[str, Any]], admin_0: list[_Boundary], admin_1: list[_Boundary]) -> None:
    """Give every record that names no locality the one Natural Earth resolves, if it does."""
    unplaced = [record for record in records if "location" not in record["properties"]]
    places = [_Place(record["geometry"]["coordinates"][1], record["geometry"]["coordinates"][0]) for record in unplaced]
    _resolve_places(places, admin_0, admin_1)
    for record, place in zip(unplaced, places, strict=True):
        if (name := locality_name(place)) is not None:
            record["properties"]["location"] = {"name": name}


def notice_block(commit: str, today: str, count: int) -> dict[str, Any]:
    return {
        "generated_by": "scripts/build_commons_bundle.py",
        "generated": today,
        "repository": _REPOSITORY,
        "commit": commit,
        # The file as a whole, whatever the repository around it is licensed as. This is a
        # deliberate carve-out from the AGPL in `LICENSE`, which is a software copyleft and
        # says nothing about a vendored database.
        "licence": "ODbL 1.0",
        "licence_url": _ODBL_URL,
        "notice": (
            "OpenDiving Commons is a Derivative Database of OpenStreetMap under the Open Database "
            "License (ODbL) 1.0. © OpenDiving Commons contributors, "
            f"{_REPOSITORY}. © OpenStreetMap contributors, https://osm.org/copyright. It also "
            "contains public-domain (CC0) records from Wikidata and from divers' contributions. "
            "Locality names are from Natural Earth, public domain."
        ),
        "sources": [
            {
                "name": "OpenStreetMap",
                # `source` is the link the service resolves a record's credit through: it is
                # the value that record's own provenance carries. Written explicitly rather
                # than left to be inferred from `name`, because getting it wrong ships ODbL
                # data with no notice attached to it.
                "source": "openstreetmap",
                "licence": "ODbL 1.0",
                "licence_url": _ODBL_URL,
                "supplies": "the records seeded from it",
                "attribution": _ATTRIBUTION["openstreetmap"],
            },
            {
                "name": "Wikidata",
                "source": "wikidata",
                "licence": "CC0 1.0",
                "licence_url": _CC0_URL,
                "supplies": "the records seeded from it",
                "attribution": _ATTRIBUTION["wikidata"],
            },
            {
                "name": "OpenDiving Commons contributors",
                "source": "contribution",
                "licence": "CC0 1.0",
                "licence_url": _CC0_URL,
                "supplies": "the records divers contributed",
                "attribution": _ATTRIBUTION["contribution"],
            },
            {
                "name": "Natural Earth",
                # No `source` and no `attribution`. Natural Earth asks for no credit, and it
                # supplies no record of its own for a per-record one to hang on - only the
                # locality on records that came from somewhere else.
                "url": f"{ADMIN_0_URL} and {ADMIN_1_URL}",
                "licence": "public domain",
                "licence_url": "https://www.naturalearthdata.com/about/terms-of-use/",
                "supplies": "the locality on a record that names none",
            },
        ],
        "count": count,
    }


def serialised(notice: dict[str, Any], records: list[dict[str, Any]]) -> str:
    """The notice first, where a reader meets it, then one feature per line."""
    compact = {"ensure_ascii": False, "separators": (",", ":")}
    head = json.dumps({"type": "FeatureCollection", **notice}, **compact)  # type: ignore[arg-type]
    features = ",\n".join(json.dumps(record, **compact) for record in records)  # type: ignore[arg-type]
    return f'{head[:-1]},"features":[\n{features}\n]}}\n'


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Commons bundle from a checkout of the repository.")
    parser.add_argument("checkout", type=Path, help="A clean checkout of opendiving/opendiving-commons")
    checkout = parser.parse_args().checkout.resolve()

    try:
        commit = _commit_of(checkout)
        records = _read_records(checkout)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"could not read {checkout}: {exc}", file=sys.stderr)
        return 1
    if not records:
        print("no records found; refusing to write an empty bundle", file=sys.stderr)
        return 1

    if any("location" not in record["properties"] for record in records):
        try:
            print(f"Fetching {ADMIN_0_URL} ...", file=sys.stderr)
            admin_0 = _admin_0_boundaries(_fetch(ADMIN_0_URL))
            print(f"Fetching {ADMIN_1_URL} ...", file=sys.stderr)
            admin_1 = _admin_1_boundaries(_fetch(ADMIN_1_URL))
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print(f"upstream fetch failed: {exc}", file=sys.stderr)
            return 1
        print("Resolving localities ...", file=sys.stderr)
        _place_records(records, admin_0, admin_1)

    # Staged and moved into place, because `docker-compose.yml` bind-mounts `./src/app`
    # straight into the running container: a plain in-place write means a process starting
    # mid-build reads a half-written file. `os.replace` is atomic within a filesystem.
    staged = _OUTPUT.with_suffix(".geojson.tmp")
    staged.write_text(
        serialised(notice_block(commit, date.today().isoformat(), len(records)), records), encoding="utf-8"
    )
    os.replace(staged, _OUTPUT)

    placed = sum(1 for record in records if "location" in record["properties"])
    print(f"{_OUTPUT}: {len(records)} records from {commit[:12]}, {placed} with a locality")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
