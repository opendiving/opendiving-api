"""Regenerate `src/app/data/dive_site_catalog.json` from OpenStreetMap, Wikidata and Natural Earth.

Run by hand, not by anything automated - the vendored file is the artefact under version
control, and the point of checking it in is that a self-hoster needs no network, no key and
no account for the dive-site form to suggest "SS Thistlegorm" (see
`services.dive_site_catalog`).

    uv run python scripts/build_dive_site_catalog.py

Takes a couple of minutes, most of it the two Natural Earth downloads (~54 MB together) and
the place resolution over them. Nothing here is cached between runs.

**The output is an ODbL Derivative Database, and the provenance block inside it is a licence
compliance artifact rather than documentation.** ODbL 4.4(b) makes extraction of a
substantial part of OSM's contents into a new database a Derivative Database; 4.2 requires
the licence notice to travel "within the data or metadata"; 4.6 requires recipients to be
able to obtain the derivative database or the method of making it, which is why this script
is checked in beside its output rather than run from a gist. Do not remove the `sources`
block, and do not add a record from a source whose terms are NC or ND - the merged file is
ODbL as a whole and cannot carry them.

Three upstreams, and they do different jobs:

- **OpenStreetMap**, via Overpass, is the catalog. See `_is_business` and `_is_indoor` for
  what gets dropped and why the exclusion sets are floors rather than enumerations.
- **Wikidata** is a regional patch, not a second global source. OSM has exactly three named
  `sport=scuba_diving` features in the whole of South Africa; 302 of Wikidata's 345
  recreational dive sites are South African. Deduped against OSM, it fires on a couple of
  dozen records precisely because the two sets barely intersect.
- **Natural Earth** supplies country and region. Neither of the other two carries them: of
  3,560 named non-business OSM features, 36 have any country-ish tag and 9 any admin tag.
  It is public domain, consumed here and never shipped, so it adds no licence obligation -
  but it is named in the provenance block anyway, because that block's job is to say where
  every field came from.
"""

import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

_OUTPUT = Path(__file__).resolve().parent.parent / "src" / "app" / "data" / "dive_site_catalog.json"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# `out center;`, never bare `out;`. A way or relation has no position of its own, and the
# bare form ships them with no coordinates at all - which is how Submersion's bundled
# catalog came to have 356 of its 3,612 records unusable, a number exactly equal to its
# ways plus its relations.
OVERPASS_QUERY = (
    '[out:json][timeout:280];(nwr["sport"="scuba_diving"];nwr["scuba_diving:divespot"="yes"];);out tags center;'
)

WIKIDATA_URL = "https://query.wikidata.org/sparql"

# Q2141554 is "recreational dive site" and has no subclasses, so `wdt:P31` and the
# transitive form return the same set. Coordinates are required rather than optional: a
# record with no position cannot prefill a dive site.
WIKIDATA_QUERY = """
SELECT ?site ?siteLabel ?coordinate WHERE {
  ?site wdt:P31 wd:Q2141554 ; wdt:P625 ?coordinate .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul". }
}
"""

_NATURAL_EARTH_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
ADMIN_0_URL = f"{_NATURAL_EARTH_BASE}/ne_10m_admin_0_countries.geojson"
ADMIN_1_URL = f"{_NATURAL_EARTH_BASE}/ne_10m_admin_1_states_provinces.geojson"

# Wikidata blocks the default urllib/curl user agent outright. Overpass and GitHub do not,
# but both ask for a descriptive one, so it goes on every request rather than only the one
# that fails without it.
_USER_AGENT = "OpenDiving-dive-site-catalog/1.0 (https://github.com/opendiving/opendiving-api)"

_DOWNLOAD_TIMEOUT_SECONDS = 300

# ~11 cm. These are point features surveyed by hand, not generalised polygons, so the
# rounding here is only about not shipping float noise - unlike `build_marine_areas.py`,
# where it is what takes a third off the file.
_PRECISION = 6

# How far off a boundary a record may sit and still take its country from it. Dive sites are
# in water and admin polygons are land: only 44.5% of the catalog falls inside any admin-0
# polygon, so containment alone would leave the country empty for the majority of rows while
# every test still passed. Of an offshore sample the median distance to land is ~3 km and 98%
# are within 50 km, so this recovers roughly the whole catalog.
_NEAREST_LIMIT_KM = 50.0

# Wikidata records this close to an OSM record are the same site. 200 m is tight on purpose:
# these are point features, and the two sets barely overlap, so a loose radius costs real
# South African coverage to remove duplicates that are not there.
_DEDUPE_METRES = 200.0

# The looser radius, used only when the normalised names also match.
_NAMED_DEDUPE_METRES = 1000.0

# Degrees per kilometre, for the local planar approximation in `_distance_km`.
_KM_PER_DEGREE_LATITUDE = 110.574
_KM_PER_DEGREE_LONGITUDE = 111.320

# Per-source credits, in the `[label](href)` wire format the clients parse. The
# OpenStreetMap string is **byte-identical** to `geocoding_service._DEFAULT_ATTRIBUTION` on
# purpose: the site form renders catalog hits and geocoder hits in one list under one credit
# line that collapses repeats by exact string, so a second wording for the same licence
# would show a diver the same credit twice. Natural Earth is not here because it supplies no
# record - only the country and region on one - and asks for nothing; it is named in the
# provenance block instead.
_ATTRIBUTION = {
    "osm": "[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)",
    "wikidata": "[Data from Wikidata, CC0 1.0.](https://www.wikidata.org/wiki/Wikidata:Licensing)",
}

# A business, not a dive site. Submersion's bundled catalog skipped this step and ships
# `Dive Otago` and `Go Dive Pacific` as places to dive.
#
# **A floor, not an enumeration.** Re-derive it before a refresh by dumping the tag set of
# the non-business features and reading the value distributions of `leisure`, `building` and
# `amenity`; the tagging vocabulary moves. `amenity=scuba_diving` is deliberately *not* here
# - 23 uses globally and no wiki page, so there is no evidence it means "business" rather
# than "dive site", and guessing costs a real record.
_BUSINESS_AMENITIES = frozenset({"dive_centre"})
_BUSINESS_CLUBS = frozenset({"scuba_diving"})
_BUSINESS_KEYS = ("shop", "office")

# An indoor training facility, not a dive site. Same floor caveat as above; `fitness_centre`
# and `sports_hall` were added on the same evidence `sports_centre` and `swimming_pool` rest
# on - the same family of value, and not one of the named records carries a scuba attribute.
#
# **`leisure=pitch` and `leisure=water_park` are deliberately absent, and this is the trap
# in the whole selection rule.** `pitch` is the Dutch convention for a dive site in Zeeland
# (`Flauwers West`, `Goese Sas`, `Noordbout`) and 35 of its 43 named features carry scuba
# attributes; `water_park` is the same story at 6 of 7. A filter that excluded
# sports-shaped `leisure` values wholesale would delete them.
_INDOOR_LEISURE = frozenset({"sports_centre", "swimming_pool", "fitness_centre", "sports_hall"})
_INDOOR_AMENITIES = frozenset({"school", "community_centre", "sport_school", "public_bath"})

Ring = tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class _Boundary:
    """One contiguous administrative polygon, not one country.

    Split per part for the reason `services.marine_areas` splits its seas: a feature-level
    bounding box for a country with overseas territories spans most of the globe and rejects
    nothing, which matters twice as much here because the bounding box is the only thing
    standing between the nearest-boundary search and a vertex-by-vertex scan of the planet.
    """

    code: str | None
    name: str
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    # Outer ring first, holes after.
    rings: tuple[Ring, ...]

    @property
    def box_area(self) -> float:
        return (self.max_lon - self.min_lon) * (self.max_lat - self.min_lat)


@dataclass(slots=True)
class _Record:
    """One catalog row, before its place fields are resolved."""

    name: str
    name_en: str | None
    latitude: float
    longitude: float
    source: str
    source_id: str
    country_code: str | None = None
    country: str | None = None
    region: str | None = None


def _fetch(url: str, *, data: bytes | None = None) -> Any:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


# -------------- OpenStreetMap --------------


def _values(tags: dict[str, str], key: str) -> list[str]:
    """A tag's values, split on OSM's `;` multi-value convention.

    Without this `amenity=restaurant;dive_centre` reads as neither a restaurant nor a dive
    centre and the feature ships as a dive site - which it is not.
    """
    raw = tags.get(key)
    return [value.strip() for value in raw.split(";")] if raw else []


def _has_scuba_attributes(tags: dict[str, str]) -> bool:
    """Whether the feature is described as somewhere you dive rather than merely tagged with
    the sport - `scuba_diving:maxdepth`, `scuba_diving:entry`, `scuba_diving:divespot`."""
    return any(key.startswith("scuba_diving:") for key in tags)


def _is_business(tags: dict[str, str]) -> bool:
    return (
        any(value in _BUSINESS_AMENITIES for value in _values(tags, "amenity"))
        or any(value in _BUSINESS_CLUBS for value in _values(tags, "club"))
        or any(key in tags for key in _BUSINESS_KEYS)
    )


def _is_indoor(tags: dict[str, str]) -> bool:
    """An indoor facility - a pool, a training centre, a building.

    `scuba_diving:divespot=yes` rescues one regardless: a flooded quarry tagged
    `leisure=sports_centre` because a club runs it is still somewhere people dive, and the
    mapper saying so explicitly is better evidence than the `leisure` value.
    """
    if tags.get("scuba_diving:divespot") == "yes":
        return False
    return (
        any(value in _INDOOR_LEISURE for value in _values(tags, "leisure"))
        or any(value in _INDOOR_AMENITIES for value in _values(tags, "amenity"))
        or "building" in tags
    )


def _position(element: dict[str, Any]) -> tuple[float, float] | None:
    """A node's own position, or the centre Overpass computed for a way or relation."""
    if element.get("lat") is not None and element.get("lon") is not None:
        return float(element["lat"]), float(element["lon"])
    centre = element.get("center")
    if isinstance(centre, dict) and centre.get("lat") is not None and centre.get("lon") is not None:
        return float(centre["lat"]), float(centre["lon"])
    return None


def _osm_records(elements: Iterable[dict[str, Any]]) -> list[_Record]:
    records: list[_Record] = []
    for element in elements:
        tags = element.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name or _is_business(tags) or _is_indoor(tags):
            continue
        position = _position(element)
        if position is None:
            continue
        latitude, longitude = position

        # Only where it says something the `name` does not. `name:en` equal to `name` is the
        # common case for anywhere already named in Latin script, and carrying it would
        # double the size of the field for no reachable search term.
        name_en = (tags.get("name:en") or "").strip() or None
        if name_en == name:
            name_en = None

        records.append(
            _Record(
                name=name,
                name_en=name_en,
                latitude=round(latitude, _PRECISION),
                longitude=round(longitude, _PRECISION),
                source="osm",
                source_id=f"{element['type']}/{element['id']}",
            )
        )
    return records


# -------------- Wikidata --------------


def _wikidata_records() -> list[_Record]:
    query = urllib.parse.urlencode({"query": WIKIDATA_QUERY, "format": "json"}).encode()
    payload = _fetch(WIKIDATA_URL, data=query)

    records: list[_Record] = []
    for binding in payload["results"]["bindings"]:
        qid = binding["site"]["value"].rsplit("/", 1)[-1]
        label = binding.get("siteLabel", {}).get("value", "").strip()
        # An item with no label answers with its own QID, which is not a name a diver can
        # read. Three of the 345 are in that state.
        if not label or label == qid:
            continue
        point = binding["coordinate"]["value"]
        if not point.startswith("Point(") or not point.endswith(")"):
            continue
        longitude_text, _, latitude_text = point[len("Point(") : -1].partition(" ")
        try:
            longitude, latitude = float(longitude_text), float(latitude_text)
        except ValueError:
            continue

        records.append(
            _Record(
                name=label,
                # The label already is the English name, so a separate `name_en` would only
                # repeat it. Unlike OSM, where `name` is whatever the site is called locally.
                name_en=None,
                latitude=round(latitude, _PRECISION),
                longitude=round(longitude, _PRECISION),
                source="wikidata",
                source_id=qid,
            )
        )
    return records


def _normalised(name: str) -> str:
    return "".join(character for character in name.casefold() if character.isalnum())


def _deduped(wikidata: list[_Record], osm: list[_Record], osm_qids: set[str]) -> list[_Record]:
    """Wikidata records that are not already in the OSM set.

    Three signals, in decreasing confidence: the OSM feature names the QID in its own
    `wikidata` tag; the two positions are within `_DEDUPE_METRES`; or they are within
    `_NAMED_DEDUPE_METRES` *and* the normalised names match. The last one is deliberately not
    proximity alone at a kilometre - two genuinely different sites on one reef are routinely
    that close, and the point of this source is the coverage it adds.
    """
    kept: list[_Record] = []
    for candidate in wikidata:
        if candidate.source_id in osm_qids:
            continue
        duplicate = False
        for existing in osm:
            metres = _distance_km(candidate.latitude, candidate.longitude, existing.latitude, existing.longitude) * 1000
            if metres <= _DEDUPE_METRES or (
                metres <= _NAMED_DEDUPE_METRES and _normalised(candidate.name) == _normalised(existing.name)
            ):
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
    return kept


# -------------- Natural Earth --------------


def _distance_km(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    """Local planar approximation, not a haversine, and that is a deliberate choice here.

    Every comparison this makes is against `_NEAREST_LIMIT_KM`, where the equirectangular
    error is under a tenth of a percent, and it runs tens of millions of times against
    polygon vertices. `services.dive_site_catalog` ranks over global distances and uses a
    real haversine for exactly that reason.
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
        # defect this catalog exists to avoid on the country half. The 7 features with no
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
    land: only 44.5% of the catalog falls inside any admin-0 polygon, so a generator written
    to containment alone ships an empty country for more than half its rows - and does it
    while every unit test passes, because each one names a site that happens to be inshore.
    """
    for boundary in boundaries:
        if _contains(boundary, longitude, latitude):
            return boundary
    return _nearest_within(boundaries, longitude, latitude)


def _resolve_places(records: list[_Record], admin_0: list[_Boundary], admin_1: list[_Boundary]) -> None:
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


def _document(records: list[_Record], today: str) -> dict[str, Any]:
    return {
        "generated_by": "scripts/build_dive_site_catalog.py",
        "generated": today,
        # The file as a whole, whatever the repository around it is licensed as. This is a
        # deliberate carve-out from the AGPL in `LICENSE`, which is a software copyleft and
        # says nothing about a vendored database.
        "licence": "ODbL 1.0",
        "licence_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "notice": (
            "This file is a Derivative Database of OpenStreetMap under the Open Database "
            "License (ODbL) 1.0. © OpenStreetMap contributors, https://osm.org/copyright. "
            "It also contains public-domain (CC0) data from Wikidata. Country and region "
            "names are from Natural Earth, public domain. Regenerate it with "
            "scripts/build_dive_site_catalog.py."
        ),
        "sources": [
            {
                "name": "OpenStreetMap",
                # `source` is the link the service resolves a record's credit through: it is
                # the value that record's own `source` field carries. Written explicitly
                # rather than left to be inferred from `name`, because getting it wrong ships
                # ODbL data with no notice attached to it.
                "source": "osm",
                "url": OVERPASS_URL,
                "query": OVERPASS_QUERY,
                "retrieved": today,
                "licence": "ODbL 1.0",
                "licence_url": "https://opendatacommons.org/licenses/odbl/1-0/",
                "supplies": "the records themselves",
                "attribution": _ATTRIBUTION["osm"],
            },
            {
                "name": "Wikidata",
                "source": "wikidata",
                "url": WIKIDATA_URL,
                "query": " ".join(WIKIDATA_QUERY.split()),
                "retrieved": today,
                "licence": "CC0 1.0",
                "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "supplies": "the records themselves",
                "attribution": _ATTRIBUTION["wikidata"],
            },
            {
                "name": "Natural Earth",
                # No `source` and no `attribution`. Natural Earth asks for no credit, and it
                # supplies no record of its own for a per-result one to hang on - only the
                # country and region on records that came from somewhere else.
                # `services.dive_site_catalog` credits each row through this block, so a
                # record naming a source that has no `attribution` here is dropped at load
                # rather than served uncredited.
                "url": f"{ADMIN_0_URL} and {ADMIN_1_URL}",
                "retrieved": today,
                "licence": "public domain",
                "licence_url": "https://www.naturalearthdata.com/about/terms-of-use/",
                "supplies": "the country_code, country and region on every record",
            },
        ],
        "count": len(records),
        "records": [
            {
                "name": record.name,
                **({"name_en": record.name_en} if record.name_en else {}),
                "latitude": record.latitude,
                "longitude": record.longitude,
                **({"country_code": record.country_code} if record.country_code else {}),
                **({"country": record.country} if record.country else {}),
                **({"region": record.region} if record.region else {}),
                "source": record.source,
                "source_id": record.source_id,
            }
            for record in records
        ],
    }


def main() -> int:
    try:
        print(f"Fetching {OVERPASS_URL} ...", file=sys.stderr)
        elements = _fetch(OVERPASS_URL, data=urllib.parse.urlencode({"data": OVERPASS_QUERY}).encode())["elements"]
        print(f"Fetching {WIKIDATA_URL} ...", file=sys.stderr)
        wikidata = _wikidata_records()
        print(f"Fetching {ADMIN_0_URL} ...", file=sys.stderr)
        admin_0 = _admin_0_boundaries(_fetch(ADMIN_0_URL))
        print(f"Fetching {ADMIN_1_URL} ...", file=sys.stderr)
        admin_1 = _admin_1_boundaries(_fetch(ADMIN_1_URL))
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"upstream fetch failed: {exc}", file=sys.stderr)
        return 1

    osm = _osm_records(elements)
    # An OSM feature naming a QID in its own `wikidata` tag *is* that Wikidata item, which is
    # the one dedupe signal that needs no distance at all.
    osm_qids = {qid for element in elements if (qid := (element.get("tags") or {}).get("wikidata"))}
    patch = _deduped(wikidata, osm, osm_qids)
    records = osm + patch
    print(
        f"{len(elements)} OSM elements -> {len(osm)} sites; "
        f"{len(wikidata)} Wikidata sites -> {len(patch)} after dedupe",
        file=sys.stderr,
    )

    print("Resolving country and region ...", file=sys.stderr)
    _resolve_places(records, admin_0, admin_1)

    if not records:
        print("no records selected; refusing to write an empty catalog", file=sys.stderr)
        return 1

    with_country = sum(1 for record in records if record.country)
    with_region = sum(1 for record in records if record.region)

    # Staged and moved into place, because `docker-compose.yml` bind-mounts `./src/app`
    # straight into the running container: a plain in-place write means a request arriving
    # mid-regeneration reads a half-written file. `os.replace` is atomic within a filesystem.
    staged = _OUTPUT.with_suffix(".json.tmp")
    document = _document(records, date.today().isoformat())
    staged.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(staged, _OUTPUT)

    print(
        f"{_OUTPUT}: {len(records)} records, {_OUTPUT.stat().st_size / 1024:.0f} KiB "
        f"({with_country} with a country, {with_region} with a region)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
