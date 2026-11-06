"""Which Commons records a site may be: the rule a diver and a reviewer both read.

Proposals are reviewed on this instance, so the rule runs here. *Identity* comes first: the two
share a registry entry, which makes a diver's site a copy and is the one rule that refuses a
proposal. The others only ever offer, since two records they flag are often two real sites - a
lake's sunken platform and its sunken boat - and two they miss can be one. Their distances are
floors calibrated on the catalogue the pool was seeded from:

- **Within 20 m, whatever the names.** Further out, most close pairs are differently named
  features of one dive lake or one reef, and flagging them would bury the real hits.
- **Within 200 m, when the names are similar**: identical once normalised, one containing the
  other (three characters or more), half the words of the shorter one shared (three letters or
  more, outside a small stop-list), or a `difflib` ratio of 0.8. Every name a site carries is
  compared with every name of the other.
- **Within 5 km, when a name is identical once normalised** and is not a generic one: `Shark
  Point` exists in many countries, and seven `Diving Spot` placeholders sit in one archipelago.
"""

import difflib
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.crud_dive_sites import commons_records_carrying, in_the_commons
from ..models.dive_site import DiveSite
from ..schemas.dive_site import CommonsMatchRule, DiveSiteCommonsMatchRead
from .commons_records import attribution_for

NEAR_METRES = 20.0
SIMILAR_METRES = 200.0
SAME_NAME_METRES = 5000.0

_EARTH_RADIUS_METRES = 6_371_008.8
# A degree of latitude is never shorter than this, so a pair further apart in latitude than
# the widest rule allows cannot meet it.
_SHORTEST_DEGREE_METRES = 110_500

_MIN_SUBSTRING = 3
_MIN_TOKEN = 3
_RATIO = 0.8

# Words two names can share without being similar. Small on purpose, and only words of
# three letters or more, since nothing shorter is compared.
STOP_WORDS = frozenset({"the", "and", "und", "von", "der", "die", "das", "den", "del", "des", "les", "van", "het"})

# A name that is not a name: two records sharing one say nothing about being one site. A
# floor to grow as proposals show more; the seven `Diving Spot` placeholders of Kepulauan
# Seribu are why it exists.
GENERIC_NAMES = frozenset(
    {
        "dive site", "dive spot", "diving site", "diving spot", "divespot", "snorkeling spot",
        "snorkelling spot", "house reef", "housereef", "hausriff", "coral garden",
        "coral gardens", "the wall", "wall", "cave", "platform", "plattform", "einstieg",
        "entry point", "terumbu karang",
    }
)  # fmt: skip

# What tells two siblings apart and nothing else: `Housereef West` and `Housereef East`,
# `instap 1` and `instap 2`.
_QUALIFIERS = frozenset(
    {
        "north", "south", "east", "west", "nord", "sud", "süd", "ost", "oost", "noord", "zuid",
        "norte", "sur", "este", "oeste", "n", "s", "e", "w", "ne", "nw", "se", "sw", "inner",
        "outer", "upper", "lower", "left", "right", "big", "small", "little", "great", "grand",
        "petit", "klein", "groot", "gross", "i", "ii", "iii", "iv", "v", "a", "b", "c", "d",
        "mini", "deep", "shallow", "new", "old",
    }
)  # fmt: skip

_STRENGTH = {rule: strength for strength, rule in enumerate(CommonsMatchRule)}


def normalise(name: str) -> str:
    """NFKD, combining marks dropped, case-folded, every run of non-word characters a space.

    Not an ASCII strip: `[^a-z0-9]` empties every name with no Latin letter, and seventy-odd
    seed names would then all be "the same name".
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"\W+", " ", stripped.casefold()).strip()


def _tokens(normalised: str) -> set[str]:
    return {token for token in normalised.split() if len(token) >= _MIN_TOKEN and token not in STOP_WORDS}


def one_contains_the_other(a: str, b: str) -> bool:
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= _MIN_SUBSTRING and shorter in longer


def names_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if one_contains_the_other(a, b):
        return True
    tokens_a, tokens_b = _tokens(a), _tokens(b)
    if tokens_a and tokens_b and 2 * len(tokens_a & tokens_b) >= min(len(tokens_a), len(tokens_b)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= _RATIO


def _qualifier_siblings(a: str, b: str) -> bool:
    difference = set(a.split()) ^ set(b.split())
    return bool(difference) and all(token in _QUALIFIERS or token.isdigit() for token in difference)


def metres_between(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    """Haversine, on a sphere of the mean Earth radius."""
    phi_a, phi_b = math.radians(latitude_a), math.radians(latitude_b)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(longitude_b - longitude_a)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_METRES * math.asin(min(1.0, math.sqrt(h)))


def normalised_names(names: Iterable[str]) -> list[str]:
    return [name for name in dict.fromkeys(normalise(name) for name in names) if name]


def judge(names_a: list[str], names_b: list[str], metres: float) -> tuple[CommonsMatchRule, bool] | None:
    """The first distance rule a pair meets and whether the names differ only by a qualifier, or
    None. The names come through `normalised_names`."""
    if metres <= NEAR_METRES:
        return CommonsMatchRule.WITHIN_20_M, False
    if metres <= SIMILAR_METRES:
        similar = [(x, y) for x in names_a for y in names_b if names_similar(x, y)]
        if similar:
            siblings = all(_qualifier_siblings(x, y) for x, y in similar)
            return CommonsMatchRule.SIMILAR_NAME_WITHIN_200_M, siblings
    if metres <= SAME_NAME_METRES:
        shared = set(names_a) & set(names_b)
        if any(name not in GENERIC_NAMES for name in shared):
            return CommonsMatchRule.SAME_NAME_WITHIN_5_KM, False
    return None


def longitude_ranges(latitude: float, longitude: float, metres: float) -> list[tuple[float, float]] | None:
    """The longitudes a point within `metres` of this one can have, as one range or, across the
    antimeridian, two. None when it can have any, which is what being near a pole comes to.

    From the haversine: `sin(d/2R)` bounds `sqrt(cos a cos b) sin(dLon/2)`, and the point furthest
    from the equator that is still in reach gives the widest longitude.
    """
    furthest = min(90.0, abs(latitude) + metres / _SHORTEST_DEGREE_METRES)
    product = math.cos(math.radians(latitude)) * math.cos(math.radians(furthest))
    if product <= 0:
        return None
    reach = math.sin(metres / (2 * _EARTH_RADIUS_METRES)) / math.sqrt(product)
    if reach >= 1:
        return None
    degrees = math.degrees(2 * math.asin(reach))
    west, east = longitude - degrees, longitude + degrees
    if west < -180:
        return [(west + 360, 180.0), (-180.0, east)]
    if east > 180:
        return [(west, 180.0), (-180.0, east - 360)]
    return [(west, east)]


def _within_reach(latitude: float, longitude: float) -> ColumnElement[bool]:
    """A range over the two float columns: every pin the widest rule could reach, and some it
    cannot, which the rule then drops."""
    degrees = SAME_NAME_METRES / _SHORTEST_DEGREE_METRES
    near = DiveSite.latitude.between(latitude - degrees, latitude + degrees)
    ranges = longitude_ranges(latitude, longitude, SAME_NAME_METRES)
    if ranges is None:
        return near
    return and_(near, or_(*(DiveSite.longitude.between(west, east) for west, east in ranges)))


async def find_commons_matches(
    db: AsyncSession, members: Mapping[str, Any], *, excluding_id: int | None = None
) -> list[DiveSiteCommonsMatchRead]:
    """The Commons records a site with these shared members may be, strongest first and nearest
    first within a rule. `excluding_id` leaves out the record being changed."""
    latitude, longitude = members["latitude"], members["longitude"]
    entries = {(entry["registry"], entry["identifier"]) for entry in members["external_ids"]}

    reachable: list[ColumnElement[bool]] = []
    if entries:
        reachable.append(DiveSite.id.in_(commons_records_carrying(entries)))
    if latitude is not None and longitude is not None:
        reachable.append(_within_reach(latitude, longitude))
    if not reachable:
        return []

    conditions = [in_the_commons(), or_(*reachable)]
    if excluding_id is not None:
        conditions.append(DiveSite.id != excluding_id)
    rows = await db.execute(
        select(
            DiveSite.uuid,
            DiveSite.name,
            DiveSite.other_names,
            DiveSite.latitude,
            DiveSite.longitude,
            DiveSite.location_name,
            DiveSite.external_ids,
            DiveSite.commons_source,
        ).where(*conditions)
    )

    names = normalised_names([members["name"], *members["other_names"]])
    matches = []
    for row in rows:
        metres = (
            None
            if latitude is None or longitude is None or row.latitude is None or row.longitude is None
            else metres_between(latitude, longitude, row.latitude, row.longitude)
        )
        carried = {(entry["registry"], entry["identifier"]) for entry in row.external_ids}
        met: tuple[CommonsMatchRule, bool] | None = (CommonsMatchRule.IDENTITY, False) if entries & carried else None
        if met is None and metres is not None:
            met = judge(names, normalised_names([row.name, *row.other_names]), metres)
        if met is None:
            continue
        matches.append(
            DiveSiteCommonsMatchRead(
                uuid=row.uuid,
                name=row.name,
                other_names=row.other_names,
                latitude=row.latitude,
                longitude=row.longitude,
                location_name=row.location_name,
                external_ids=row.external_ids,
                attribution=attribution_for(row.commons_source),
                rule=met[0],
                distance_metres=None if metres is None else round(metres, 1),
                qualifier_siblings=met[1],
            )
        )
    return sorted(
        matches,
        key=lambda match: (
            _STRENGTH[match.rule],
            math.inf if match.distance_metres is None else match.distance_metres,
            match.name,
        ),
    )
