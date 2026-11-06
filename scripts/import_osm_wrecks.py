"""Write OpenStreetMap's named wrecks with a depth as a batch for `build_commons_bundle.py`.

Run by hand, against a clean checkout of the repository, and hand the output to the builder:

    uv run python scripts/import_osm_wrecks.py /path/to/opendiving-commons wrecks.geojson
    uv run python scripts/build_commons_bundle.py /path/to/opendiving-commons --batch wrecks.geojson

Overpass can take a couple of minutes to answer. The file is a working file and is not checked
in: the bundle's diff is the batch.

**Each floor is written to leave a doubtful row out**, because nothing removes a Commons record
once the hosted instance has inserted it. Tags alone do not make a dive site: of the wrecks a
filter on hazard tags keeps, about one in three is a dive, the rest being beached hulks, chart
hazards and deep-ocean loss positions. A tagged depth is the filter whose survivors read as a
dive list.

A wreck already in the pool is untouched, so this adds records and refreshes none. Candidates are
written in the order of their OpenStreetMap entry, which is the order the builder judges them in.
"""

import argparse
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_QUERY_TIMEOUT_SECONDS = 300
_QUERY = f"""[out:json][timeout:{_QUERY_TIMEOUT_SECONDS}];
(
  nwr["historic"="wreck"]["name"];
  nwr["seamark:type"="wreck"]["name"];
);
out tags center;
"""

# Overpass asks for a descriptive agent. It names the project and nobody in it.
_USER_AGENT = "OpenDiving-commons-bundle/1.0 (https://github.com/opendiving/opendiving-api)"

# The repository's `SEED_UUID_PREFIX`: a seed's uuid is a function of this text, so it is fixed
# rather than derived from wherever the repository lives.
_SEED_UUID_PREFIX = "https://github.com/opendiving/opendiving-commons/sites/"

_REGISTRY = "openstreetmap"

# Shallower than this the tagged wrecks read as chart hazards rather than dives.
_MIN_DEPTH_METRES = 3.0
_MAX_DEPTH_METRES = 60.0
_METRES_PER_FOOT = 0.3048

_DEPTH_KEYS = (
    "wreck:depth",
    "depth",
    "seamark:wreck:depth",
    "scuba_diving:depth",
    "scuba_diving:maxdepth",
    "scuba_diving:mindepth",
    "wreck:max_depth",
    "wreck:min_depth",
    "maxdepth",
    "mindepth",
)

_NUMBER = r"\d+(?:\.\d+)?"
_UNIT = r"m|metres?|meters?|ft|feet|foot|'"
# No sign is allowed for: a negative value is a height, and `ele` says the same thing.
_DEPTH = re.compile(
    rf"(?P<first>{_NUMBER})\s*(?P<first_unit>{_UNIT})?"
    rf"(?:\s*[-–—]\s*(?P<second>{_NUMBER})\s*(?P<second_unit>{_UNIT})?)?",
    re.IGNORECASE,
)

_SHOWING = re.compile(r"(hull|mast).*(showing|visible)")
_SUBMERGED = frozenset({"submerged", "always_submerged"})
_CLOSED = frozenset({"no", "private"})

# `graves?` stands alone so that a gravel seabed in a description is not a grave. `raä:ref` is the
# Swedish National Heritage Board's own reference, which says heritage without the word.
_HERITAGE = re.compile(r"heritage|protect|archa?eolog|(?<![a-z])graves?(?![a-z])|^raa:")
_HERITAGE_VALUE_KEYS = ("historic", "seamark:information", "description", "note")

# Wrecks that should stay out and that no tag says so of, by entry, each with its reason.
_LEFT_OUT_BY_NAME = {
    "node/1347780019": "HMAS Perth, a war grave no tag marks",
    "node/6516992724": "Riksäpplet, a 17th-century Swedish warship and a monument no tag marks",
}

_ROMAN_NUMERAL = re.compile(r"[ivx]+")


def _folded(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(character for character in decomposed if not unicodedata.combining(character)).casefold()


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", _folded(text))


# What a name made of nothing else says about the feature rather than which one it is. A floor to
# grow as batches show more: these are the words OpenStreetMap's wreck names actually use.
_NOT_NAMING_WORDS = frozenset(
    _words(
        """
        wreck wrecks wrecked wreckage shipwreck shipwrecks wrak wrack wracks vrak vraket épave épaves
        schiffswrack bootswrack flugzeugwrack autowrack lastwagen naufragio naufrágio naufrage relitto
        relitti pecio restos resti remains rubble debris hulk hull ruin ruins bow stern graveyard
        cemetery cimetière friedhof site area ναυάγιο уламки останки حطام

        ship ships boat boats vessel vessels craft schooner sloop barge barges tug tugs tugboat ferry
        scow lighter dredge dredger trawler freighter steamer steamship tanker yacht sailboat cargo
        fishing landing patrol submarine plane planes airplane aeroplane aircraft bomber fighter
        helicopter car cars auto autos truck bus tank barco barcos bateau bateaux navio nave navire avion avión avião
        aereo aéreo chaland draga balsa vapor lancha acorazado crucero schiff boot flugzeug barka
        ponton pontoon баржа судно шхуна катер корабль سفينة ss ms mv mfv hms uss sms

        the a an of le la les l el los las il lo de del du des der die das den het een ein eine o os
        as en

        big bigger small smaller little large great huge old older new unknown unidentified unnamed
        sunken sunk sinking hundido hundida undido undida naufragado naufragada encallado encalhado
        afundado affondata gesunken versunken coulé abandoned burnt burned crashed broken inverted
        overturned upright concrete wooden iron steel затонувшая затонувший затонувшее
        """
    )
)


def is_a_name(name: str) -> bool:
    """Whether any word of it tells this wreck from another."""
    return any(
        word not in _NOT_NAMING_WORDS and not word.isdecimal() and not _ROMAN_NUMERAL.fullmatch(word)
        for word in _words(name)
    )


def _metres(value: str, unit: str | None) -> float:
    is_feet = unit is not None and unit.lower() in {"ft", "feet", "foot", "'"}
    return float(value) * _METRES_PER_FOOT if is_feet else float(value)


def _parsed_depth(value: str) -> list[float]:
    """The metres a tag says, one value or the two ends of a range, or nothing if it says none.
    An end with no unit takes the other's: `1-25m` is metres at both."""
    match = _DEPTH.fullmatch(value.strip())
    if match is None:
        return []
    first_unit, second_unit = match["first_unit"], match["second_unit"]
    depths = [_metres(match["first"], first_unit or second_unit)]
    if match["second"] is not None:
        depths.append(_metres(match["second"], second_unit or first_unit))
    return depths


def depth_of(tags: Mapping[str, str]) -> tuple[float, float] | None:
    """The shallowest and deepest the tags say, in metres to one decimal, or None."""
    depths = [round(depth, 1) for key in _DEPTH_KEYS if key in tags for depth in _parsed_depth(tags[key])]
    return (min(depths), max(depths)) if depths else None


def _within_reach(tags: Mapping[str, str]) -> bool:
    depths = depth_of(tags)
    return depths is not None and _MIN_DEPTH_METRES <= depths[1] <= _MAX_DEPTH_METRES


def _shows_a_hull_or_mast(tags: Mapping[str, str]) -> bool:
    return _SHOWING.search(tags.get("seamark:wreck:category", "").lower()) is not None


def _breaks_the_surface(tags: Mapping[str, str]) -> bool:
    return tags.get("seamark:wreck:water_level", "submerged") not in _SUBMERGED


def _visible_at_a_tide(tags: Mapping[str, str]) -> bool:
    return any(
        "yes" in tags.get(key, "").split(";") for key in ("wreck:visible_at_low_tide", "wreck:visible_at_high_tide")
    )


def _closed(tags: Mapping[str, str]) -> bool:
    return tags.get("access") in _CLOSED


def _marked_as_heritage(tags: Mapping[str, str]) -> bool:
    """A website's address may hold the word and say nothing about the wreck, so a value counts
    only under the keys that describe it."""
    named = (_folded(key) for key in tags)
    described = (_folded(tags[key]) for key in _HERITAGE_VALUE_KEYS if key in tags)
    return "historic" in tags.get("seamark:status", "").split(";") or any(
        _HERITAGE.search(text) for text in (*named, *described)
    )


def entry_of(element: Mapping[str, Any]) -> str:
    return f"{element['type']}/{element['id']}"


def _floors(pool: set[tuple[str, str]]) -> list[tuple[str, Callable[[Mapping[str, Any]], bool]]]:
    """What leaves an element out, in the order it is asked, each named for the count it reports."""

    def wikidata_entries(tags: Mapping[str, str]) -> set[tuple[str, str]]:
        return {("wikidata", value.strip()) for value in tags.get("wikidata", "").split(";")}

    return [
        ("already in the pool", lambda element: (_REGISTRY, entry_of(element)) in pool),
        ("its Wikidata entry is in the pool", lambda element: bool(wikidata_entries(element["tags"]) & pool)),
        ("not a name", lambda element: not is_a_name(element["tags"]["name"])),
        ("no depth", lambda element: depth_of(element["tags"]) is None),
        ("not at least 3 m and at most 60 m", lambda element: not _within_reach(element["tags"])),
        ("shows a hull or a mast", lambda element: _shows_a_hull_or_mast(element["tags"])),
        ("a water level that is not submerged", lambda element: _breaks_the_surface(element["tags"])),
        ("visible at a tide", lambda element: _visible_at_a_tide(element["tags"])),
        ("closed", lambda element: _closed(element["tags"])),
        ("marked as heritage", lambda element: _marked_as_heritage(element["tags"])),
        ("left out by name", lambda element: entry_of(element) in _LEFT_OUT_BY_NAME),
    ]


def seed_uuid(registry: str, identifier: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_SEED_UUID_PREFIX}{registry}/{identifier}"))


def candidate_of(element: Mapping[str, Any], retrieved: str) -> dict[str, Any]:
    """One element in the repository's record shape, its members in the order it publishes them.

    Six decimals is the most the repository's rules take; OpenStreetMap stores seven.
    """
    tags = element["tags"]
    position = element.get("center", element)
    entry = {"registry": _REGISTRY, "identifier": entry_of(element)}
    name = tags["name"].strip()
    properties: dict[str, Any] = {
        "uuid": seed_uuid(_REGISTRY, entry["identifier"]),
        "name": name,
        "external_ids": [entry],
    }

    name_en = tags.get("name:en", "").strip()
    if name_en and name_en.casefold() != name.casefold():
        properties["other_names"] = [name_en]

    depths = depth_of(tags)
    assert depths is not None
    shallowest, deepest = depths
    if shallowest != deepest:
        properties["depth_from"] = shallowest
    properties["depth_to"] = deepest

    properties["extensions"] = {
        "opendivingcommons": {
            "source": _REGISTRY,
            "license": "ODbL-1.0",
            "retrieved": retrieved,
            "seeded_from": entry,
        }
    }
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(position["lon"], 6), round(position["lat"], 6)]},
        "properties": properties,
    }


def candidates(response: Mapping[str, Any], pool: set[tuple[str, str]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    """The records every floor passes, in the order of their entry, and the count each floor left
    out. An element is counted against the first floor it fails."""
    retrieved = response["osm3s"]["timestamp_osm_base"][:10]
    floors = _floors(pool)
    left_out: Counter[str] = Counter()
    kept = []
    for element in sorted(response["elements"], key=lambda element: (element["type"], element["id"])):
        failed = next((label for label, fails in floors if fails(element)), None)
        if failed is None:
            kept.append(candidate_of(element, retrieved))
        else:
            left_out[failed] += 1
    return kept, left_out


def pool_entries(checkout: Path) -> set[tuple[str, str]]:
    """Every registry entry a record of the checkout lists."""
    entries: set[tuple[str, str]] = set()
    for file in (checkout / "sites").rglob("*.geojson"):
        properties = json.loads(file.read_text(encoding="utf-8"))["properties"]
        entries.update((entry["registry"], entry["identifier"]) for entry in properties.get("external_ids", []))
    return entries


def overpass_answer(body: bytes) -> dict[str, Any]:
    """The parsed response, refusing a partial one: a query that times out answers 200 with what
    it had so far and a `remark` saying so, which would read as a smaller world."""
    response: dict[str, Any] = json.loads(body)
    if remark := response.get("remark"):
        raise ValueError(f"Overpass answered in part: {remark}")
    return response


def _fetch_wrecks() -> dict[str, Any]:
    request = urllib.request.Request(
        _OVERPASS_URL,
        data=urllib.parse.urlencode({"data": _QUERY}).encode(),
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=_QUERY_TIMEOUT_SECONDS + 60) as response:
        return overpass_answer(response.read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write OpenStreetMap's named wrecks with a depth as a batch.")
    parser.add_argument("checkout", type=Path, help="A clean checkout of opendiving/opendiving-commons")
    parser.add_argument("output", type=Path, help="Where to write the batch")
    arguments = parser.parse_args(argv)

    try:
        pool = pool_entries(arguments.checkout)
        print(f"Asking {_OVERPASS_URL} ...", file=sys.stderr)
        response = _fetch_wrecks()
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"could not read the wrecks: {exc}", file=sys.stderr)
        return 1

    kept, left_out = candidates(response, pool)
    arguments.output.write_text(
        json.dumps({"type": "FeatureCollection", "features": kept}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )

    print(f"{len(response['elements'])} named wrecks at {response['osm3s']['timestamp_osm_base']}")
    for label, _ in _floors(pool):
        print(f"  {left_out[label]:>5} {label}")
    for entry, reason in _LEFT_OUT_BY_NAME.items():
        print(f"        {entry}: {reason}")
    print(f"{arguments.output}: {len(kept)} candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
