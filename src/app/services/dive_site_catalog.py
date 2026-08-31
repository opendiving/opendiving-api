"""Suggest dive sites by name, from a catalog vendored in the repo.

This exists because the geocoder knows where Dahab is and not where the Blue Hole's north
entry is. A place search answers with towns, headlands and postal addresses; a diver filling
in a dive site is looking for a named dive site, and no geocoder has one.

**3,702 records**, extracted from OpenStreetMap and Wikidata by
`scripts/build_dive_site_catalog.py`, which records the source URLs, the queries, the
retrieval date and the licence inside the file itself - where, as with
`marine_areas.geojson`, they cannot drift away from the data. Here that block is also the
**licence compliance artifact**: the file is an ODbL Derivative Database and ODbL 4.2
requires the notice to travel within the data or its metadata. It is the block that credits
each row, too - a record whose `source` is not named there is dropped at load rather than
served with no attribution, because serving it uncredited is the breach the block exists to
prevent.

**Why a vendored file rather than a live query.** A self-hoster should not acquire an
Overpass dependency, an outbound host and a second failure mode so that a form can offer a
suggestion. This is the call `services.marine_areas` already makes against the same kind of
data, and the same one that kept dive-site coordinates in two plain `Float` columns rather
than adding PostGIS.

**This is not the "sites near me" that would reopen PostGIS.** `DECISIONS.md` rejects PostGIS
with "Revisit if 'sites near me' ever ships", and `search_sites` takes a latitude and a
longitude, which looks like exactly that condition being met and ignored. It is not. What
happens here is an in-memory scan over a few thousand frozen value objects loaded from a file
that ships in the image: no table, no index, no extension, nothing persisted, and no query
the database ever sees. The position is a tie-break over an answer already selected by name,
not a spatial predicate. The revisit condition is about the *user's own* dive sites becoming
searchable by distance in Postgres, and nothing here moves towards it.

**Why no geometry library, and why the haversine is local.** There is no distance helper
anywhere in this repo - `core/utils/` has no home for one and the only trigonometry present
converts a dive computer's radian coordinates. One private haversine with exactly one
consumer is the right scope, and `marine_areas` sets the precedent by keeping its own
point-in-polygon local rather than promoting it to a shared util.

**Refresh policy: by hand, in the PR that changes the file's meaning** - the same as every
other generated artifact in this repo, and `CONTRIBUTING.md` says how. Note that this file
deliberately does **not** borrow `marine_areas`' excuse for having no policy, which is that
its polygons "do not change". OSM dive sites do change: divers add them, rename them and
move them, and the upstream this reads is alive. What makes a stale catalog acceptable is
that dive sites change *slowly* - a site that existed last year still exists, and the worst a
stale file does is fail to suggest a new one, which is exactly the state the form was in
before this shipped. The tripwire is the count assertion in `tests/test_dive_site_catalog.py`:
a refresh that moves the number fails there, so it becomes a decision somebody takes rather
than a diff somebody accepts.
"""

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "dive_site_catalog.json"

# What one search may return. Neither of the two house numbers nearby transfers: the
# geocoder's 5 is sized for a provider that charges per call, and species' 25 for a menu that
# shows nothing else. This one shares its menu with up to 5 geocoder rows, so 10 keeps the
# merged list around 15 - shorter than the species picker already renders, and short enough
# that the geocoder rows below stay reachable without much scrolling.
SUGGESTION_LIMIT = 10

_EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True, slots=True)
class CatalogSite:
    """One suggestion, as it sits in memory.

    `name` is what the site is called where it is; `name_en` exists so that a Latin keyboard
    reaches 砂辺 by typing "Sunabe", and is null wherever the two would be the same string.
    All three place fields are optional and all three are genuinely absent for some records -
    a few dozen sit far enough offshore that no administrative boundary is within 50 km, and
    the generator ships them anyway, because a site with no country is still a site.

    `country_code` never leaves this module. It is the stable key the file is built on; what
    a client gets is the English display name. Writing `EG` into a Location field whose own
    schema example is `Koh Tao, Thailand` is the mistake it exists to make impossible.
    """

    name: str
    name_en: str | None
    latitude: float
    longitude: float
    country_code: str | None
    country: str | None
    region: str | None
    source: str
    source_id: str
    attribution: str
    # Both names casefolded once at load, because the alternative is casefolding every name
    # in the file on every keystroke of every diver's search.
    _folded: tuple[str, ...]


# Set by `_sites` on the first successful read, and only then - see its docstring.
_loaded: tuple[CatalogSite, ...] | None = None

# Whether a failed read has already been reported at WARNING - see `_load`.
_warned = False


def _attributions(document: dict[str, Any]) -> dict[str, str]:
    """Credit per record source, read out of the file's own provenance block.

    Read from the data rather than hardcoded here for the reason the block exists at all: the
    licence a record ships under is a property of where it came from, and a constant in this
    module is a second place for that to be stated and a first place for it to be wrong.
    """
    return {
        source["source"]: source["attribution"]
        for source in document["sources"]
        if source.get("source") and source.get("attribution")
    }


def _load() -> tuple[CatalogSite, ...] | None:
    """Every catalog record, or `None` if the file could not be read.

    Reading it is the only step in this module that touches the world, and the dive-site
    form promises to degrade to "no suggestion" rather than raise: a truncated data file must
    not turn a diver's form into a 500 over a convenience they can type past.
    """
    try:
        document = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        attributions = _attributions(document)
        sites: list[CatalogSite] = []
        for record in document["records"]:
            attribution = attributions.get(record["source"])
            if attribution is None:
                # Not a parse failure, so it cannot be caught below: a record whose source
                # the provenance block does not name has no licence notice to travel with
                # it. Dropping it is the only safe answer - serving it would be the ODbL 4.2
                # breach the block exists to prevent - and it is silent because a file with
                # a source missing is a broken build, not a runtime condition.
                continue
            name = record["name"]
            name_en = record.get("name_en")
            sites.append(
                CatalogSite(
                    name=name,
                    name_en=name_en,
                    latitude=float(record["latitude"]),
                    longitude=float(record["longitude"]),
                    country_code=record.get("country_code"),
                    country=record.get("country"),
                    region=record.get("region"),
                    source=record["source"],
                    source_id=record["source_id"],
                    attribution=attribution,
                    _folded=tuple(value.casefold() for value in (name, name_en) if value),
                )
            )
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        # Loud once, then quiet. `_sites` retries on every search, so a genuinely broken
        # deploy would otherwise write this line for every keystroke of every dive-site form
        # for as long as it stayed broken.
        global _warned
        logger.log(
            logging.DEBUG if _warned else logging.WARNING,
            "Could not read %s (%s); the dive-site form will suggest nothing.",
            _DATA_PATH.name,
            exc,
        )
        _warned = True
        return None

    logger.debug("Loaded %d dive sites from %s.", len(sites), _DATA_PATH.name)
    return tuple(sites)


def _sites() -> tuple[CatalogSite, ...]:
    """The loaded catalog, reading the file the first time it is asked for.

    Not at import: the arq worker imports this package and never suggests anything, and
    neither does most of the test suite. The cost is one JSON parse per process, spent
    synchronously inside whichever request happens to be first.

    **Only a successful read is remembered**, exactly as in `services.marine_areas` and for
    the same reason: memoizing the failure is cheaper and is the wrong trade, since it turns
    one bad read into a picker that is dead for the life of the process, recoverable only by
    restarting, on evidence no stronger than a single `OSError`. The one routine thing that
    could produce that is closed off rather than tolerated - `docker-compose.yml` bind-mounts
    `./src/app` into the running container, so `scripts/build_dive_site_catalog.py` stages
    its output and `os.replace`s it, and a regeneration is never read half-written.
    """
    global _loaded
    if _loaded is None:
        _loaded = _load()
    return _loaded if _loaded is not None else ()


def _distance_km(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    """Great-circle distance, by the haversine formula.

    A haversine rather than the planar approximation the generator uses for its 50 km
    boundary test, because these distances are unbounded: a diver searching from Egypt gets
    matches in Indonesia, and a flat-earth approximation is wrong by hundreds of kilometres
    at that range and wrong in a way that reorders results.
    """
    latitude_a_rad, latitude_b_rad = math.radians(latitude_a), math.radians(latitude_b)
    delta_latitude = latitude_b_rad - latitude_a_rad
    delta_longitude = math.radians(longitude_b - longitude_a)
    a = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude_a_rad) * math.cos(latitude_b_rad) * math.sin(delta_longitude / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _match_rank(site: CatalogSite, folded_query: str) -> int | None:
    """How well a site answers the query - lower is better, `None` means it does not.

    Both names are searched, so 砂辺 is reachable by typing "Sunabe" and Sunabe is reachable
    by typing 砂辺. Substring rather than prefix, because a diver looking for the Thistlegorm
    types "thistlegorm" and the record is called "SS Thistlegorm".
    """
    best: int | None = None
    for folded in site._folded:
        if folded == folded_query:
            return 0
        if folded.startswith(folded_query):
            best = 1
        elif best is None and folded_query in folded:
            best = 2
    return best


def search_sites(
    query: str,
    latitude: float | None = None,
    longitude: float | None = None,
) -> tuple[list[CatalogSite], bool]:
    """Catalog sites matching `query`, best first, and whether the answer was cut.

    A linear scan over a few thousand frozen value objects, which is microseconds - the same
    reasoning `marine_areas` uses for its ray casting. Sync CPU in the handler, one sub-MB
    copy per worker.

    **When a position is given, distance decides.** That is the ranking, not a tie-break
    applied after match quality: a diver who dropped a pin before searching is asking about
    where they are, and 111 of this file's names are carried by more than one record - five
    `Shark Point`s in four countries, seven `Diving Spot`s within four kilometres of each
    other - so distance is the only thing that separates a same-name cluster at all.
    With no position there is nothing to rank by but the match, so an exact name beats a
    prefix and a prefix beats a substring, and the name breaks the tie so that the same query
    always returns the same order.
    """
    folded_query = query.strip().casefold()
    if not folded_query:
        return [], False

    position = (latitude, longitude) if latitude is not None and longitude is not None else None

    matches: list[tuple[float, float, str, CatalogSite]] = []
    for site in _sites():
        rank = _match_rank(site, folded_query)
        if rank is None:
            continue
        distance = _distance_km(position[0], position[1], site.latitude, site.longitude) if position else 0.0
        # The tuple is the sort key, and the two orderings differ only in which of the first
        # two slots leads. Built here rather than in a `key=` callable so the distance is
        # computed once per match instead of once per comparison.
        primary, secondary = (distance, rank) if position else (rank, distance)
        matches.append((primary, secondary, site.name, site))

    # `match[:3]` rather than the whole tuple: `CatalogSite` is not orderable, so rows tying
    # on all three - the seven identically named, identically placed `Diving Spot` records
    # searched without a position - would otherwise be compared against each other and raise.
    # Python's sort is stable, so they keep the file's order instead.
    matches.sort(key=lambda match: match[:3])
    return [site for *_, site in matches[:SUGGESTION_LIMIT]], len(matches) > SUGGESTION_LIMIT
