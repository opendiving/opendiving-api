"""What the importers of a curated source share: the layer each reads and the record each writes.

A curated source is a public dataset the repository knows by a token, which is the record's
`source`, its `seeded_from.registry` and its directory there. Its identifier is provenance, so a
record lists no `external_ids`. Nothing here imports the app or the builder: a batch is a file the
builder reads, whatever wrote it.
"""

import json
import re
import unicodedata
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# The publishers' services ask for nothing, but it names the project and nobody in it all the same.
_USER_AGENT = "OpenDiving-commons-bundle/1.0 (https://github.com/opendiving/opendiving-api)"

_QUERY_TIMEOUT_SECONDS = 120

# The repository's `SEED_UUID_PREFIX`: a seed's uuid is a function of this text, so it is fixed
# rather than derived from wherever the repository lives.
_SEED_UUID_PREFIX = "https://github.com/opendiving/opendiving-commons/sites/"

_METRES_PER_FOOT = 0.3048

_COMMONS = "opendivingcommons"


def metres(feet: float) -> float:
    return feet * _METRES_PER_FOOT


def one_line(text: str) -> str:
    """A publisher's text with the runs of spaces its rows carry closed up."""
    return " ".join(text.split())


def slug(name: str) -> str:
    """The identifier of a source that has none of its own. A seed's uuid comes from it, so it
    outlives any later change to this rule: an apostrophe joins, anything else separates."""
    decomposed = unicodedata.normalize("NFKD", name)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    joined = re.sub(r"['’]", "", plain.casefold())
    return re.sub(r"[^a-z0-9]+", "-", joined).strip("-")


def refuse_repeats(identifiers: Iterable[str]) -> None:
    """Two sites on one identifier would be one uuid, and the second would vanish in the builder."""
    seen: set[str] = set()
    for identifier in identifiers:
        if identifier in seen:
            raise ValueError(f"two sites share the identifier {identifier}")
        seen.add(identifier)


def candidate(
    token: str,
    identifier: str,
    name: str,
    position: tuple[float, float],
    *,
    license: str,
    retrieved: str,
    depths: tuple[float | None, float | None] = (None, None),
    water_type: str | None = None,
) -> dict[str, Any]:
    """One site in the repository's record shape. `position` is longitude and latitude, and
    `depths` the two ends in metres: an end that says what the other does is written once, as
    `depth_to`, the form a single depth has across the pool."""
    entry = {"registry": token, "identifier": identifier}
    properties: dict[str, Any] = {
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_SEED_UUID_PREFIX}{token}/{identifier}")),
        "name": name,
    }
    shallowest, deepest = (None if depth is None else round(depth, 1) for depth in depths)
    if shallowest is not None and shallowest != deepest:
        properties["depth_from"] = shallowest
    if deepest is not None:
        properties["depth_to"] = deepest
    if water_type is not None:
        properties["water_type"] = water_type
    properties["extensions"] = {
        _COMMONS: {"source": token, "license": license, "retrieved": retrieved, "seeded_from": entry}
    }
    longitude, latitude = position
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(longitude, 6), round(latitude, 6)]},
        "properties": properties,
    }


def position_of(feature: dict[str, Any]) -> tuple[float, float]:
    geometry = feature["geometry"]
    return geometry["x"], geometry["y"]


def layer_answer(body: bytes) -> list[dict[str, Any]]:
    """The features of a query's answer. A failed query answers 200 with an `error` member and a
    long one 200 with a flag saying it stopped, and either would read as a smaller register."""
    answer: dict[str, Any] = json.loads(body)
    if error := answer.get("error"):
        raise ValueError(f"the layer refused the query: {error}")
    if answer.get("exceededTransferLimit"):
        raise ValueError("the layer answered in part")
    features: list[dict[str, Any]] = answer["features"]
    return features


def fetch_layer(layer: str, where: str = "1=1") -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {"where": where, "outFields": "*", "returnGeometry": "true", "outSR": 4326, "f": "json"}
    )
    request = urllib.request.Request(
        f"{layer}/query?{query}", headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=_QUERY_TIMEOUT_SECONDS) as response:
        return layer_answer(response.read())


def write_batch(output: Path, kept: list[dict[str, Any]]) -> None:
    output.write_text(
        json.dumps({"type": "FeatureCollection", "features": kept}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
