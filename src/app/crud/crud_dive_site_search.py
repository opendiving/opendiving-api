"""The queries behind `GET /dive-sites/search`: one page of site rows, and the count of sites
near a place.

Every name condition reads `public.dive_site_search_text`, the revision's fold of a site's name
and other names into one case- and accent-free text with a unit separator around each, which the
trigram index `ix_dive_site_search_text_trgm` holds. A pattern anchored on the separator is a
whole name or its start, so the exact, prefix and substring tiers are three `LIKE`s over one
indexed expression.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Text, and_, case, false, func, literal, null, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.search import LIKE_ESCAPE_CHAR, escape_like
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_site import DiveSite
from ..models.saved_site import SavedSite
from ..schemas.dive_site import DiveSiteListSort
from ..schemas.dive_site_search import DiveSiteSearchMatch
from ..schemas.location import DIVE_SITE_LOCATION_PREFIX, LOCATION_FIELDS
from .crud_dive_sites import (
    EARTH_RADIUS_M,
    carrying_tag,
    has_commons_match,
    in_list_order,
    in_the_commons,
    metres_from,
    readable_by,
    where_seen,
)
from .crud_lookups import get_lookup_page

_SEPARATOR = "\x1f"

# The least `word_similarity` the typo tier admits. A one-letter typo in a five-letter word keeps
# three of its six trigrams against nine in the union, a third, which is the worst case for any
# word of five letters or more: a longer word keeps more.
FUZZY_SIMILARITY = 0.3

# How near a place's point a site is, for a place the geocoder gave no box.
NEARBY_RADIUS_M = 25_000.0

TIER_OWN = 1
TIER_COMMONS = 2
TIER_FUZZY = 4


@dataclass(frozen=True)
class Box:
    """A view, as the api spells boxes throughout: `west > east` crosses the antimeridian."""

    south: float
    north: float
    west: float
    east: float

    def holds(self) -> ColumnElement[bool]:
        """The sites whose pin lies inside; a site with no pin is never inside a box."""
        latitude = DiveSite.latitude.between(self.south, self.north)
        if self.west <= self.east:
            return and_(latitude, DiveSite.longitude.between(self.west, self.east))
        return and_(latitude, or_(DiveSite.longitude >= self.west, DiveSite.longitude <= self.east))


def box_around(latitude: float, longitude: float, radius_m: float) -> Box:
    """The smallest box holding every point within `radius_m` of this one, to narrow a distance
    condition to what the index over the pin can find. Past a pole it spans every longitude."""
    span = math.degrees(radius_m / EARTH_RADIUS_M)
    south, north = max(-90.0, latitude - span), min(90.0, latitude + span)
    if south <= -90.0 or north >= 90.0:
        return Box(south, north, -180.0, 180.0)
    width = span / math.cos(math.radians(max(abs(south), abs(north))))
    if width >= 180.0:
        return Box(south, north, -180.0, 180.0)
    west, east = longitude - width, longitude + width
    return Box(south, north, west + 360.0 if west < -180.0 else west, east - 360.0 if east > 180.0 else east)


def fold(value: str) -> ColumnElement[str]:
    """`value` as the search text holds a name: lower case, accents taken off."""
    return func.public.opendiving_fold(literal(value, Text), type_=Text)


def search_text() -> ColumnElement[str]:
    return func.public.dive_site_search_text(DiveSite.name, DiveSite.other_names, type_=Text)


def _pattern(before: str, term: str, after: str) -> ColumnElement[str]:
    return literal(before, Text) + fold(escape_like(term)) + literal(after, Text)


def name_contains(term: str) -> ColumnElement[bool]:
    """A name or other name holds `term`, case and accents folded."""
    return search_text().like(_pattern("%", term, "%"), escape=LIKE_ESCAPE_CHAR)


def _name_is(term: str) -> ColumnElement[bool]:
    return search_text().like(_pattern(f"%{_SEPARATOR}", term, f"{_SEPARATOR}%"), escape=LIKE_ESCAPE_CHAR)


def _name_starts(term: str) -> ColumnElement[bool]:
    return search_text().like(_pattern(f"%{_SEPARATOR}", term, "%"), escape=LIKE_ESCAPE_CHAR)


def _locality_contains(term: str) -> ColumnElement[bool]:
    return func.public.opendiving_fold(DiveSite.location_name, type_=Text).like(
        _pattern("%", term, "%"), escape=LIKE_ESCAPE_CHAR
    )


_ON_THE_LIST = SavedSite.id.is_not(None)

_COLUMNS = (
    DiveSite.id,
    DiveSite.uuid,
    DiveSite.user_id,
    DiveSite.commons_source,
    DiveSite.name,
    DiveSite.other_names,
    DiveSite.latitude,
    DiveSite.longitude,
    *(getattr(DiveSite, f"{DIVE_SITE_LOCATION_PREFIX}{field}") for field in LOCATION_FIELDS),
    DiveSite.external_ids,
    DiveSite.water_type,
    DiveSite.altitude,
    DiveSite.entry_types,
    DiveSite.depth_from,
    DiveSite.depth_to,
    _ON_THE_LIST.label("saved"),
)


def _with_the_list(user_id: int) -> Any:
    """`dive_site` beside this diver's saved row, where they have one."""
    return DiveSite.__table__.outerjoin(
        SavedSite.__table__, (SavedSite.dive_site_id == DiveSite.id) & (SavedSite.user_id == user_id)
    )


@dataclass(frozen=True)
class SiteSearchPage:
    rows: list[dict[str, Any]]
    # Every site row the request admits, on any page.
    total_count: int
    # Of those, the rows of the tiers before the places: every row but the typo tier's.
    matched_count: int


async def get_site_search_page(
    db: AsyncSession,
    *,
    user_id: int,
    term: str | None,
    camera: tuple[float, float] | None,
    box: Box | None,
    my: bool,
    until: datetime | None,
    tag_id: int | None,
    commons_match: bool,
    species_id: int | None,
    sort: DiveSiteListSort | None,
    fuzzy: bool,
    offset: int,
    limit: int,
) -> SiteSearchPage:
    """One page of site rows, each with `distance_m` from the camera and `match`.

    With `term`, the tiers: the diver's own and saved sites whose name matches - and under
    `my` those whose locality alone does, after them - then the Commons records they have not
    saved whose name matches, then, where `fuzzy`, the sites only a typo away. Within the first
    two an exact name, then a prefix, then a substring, nearer first within each; within the
    last the closer resemblance first. `sort` replaces that order with the list's. The box
    narrows every tier.

    Without `term`: under `my` the diver's sites in the list's order, every one of them or,
    where a box is sent, the pinned ones inside it; with a camera the pinned sites they can see,
    nearest first, inside the box where one is sent; and with neither their sites by last use at
    or before `until`, as their lookup orders them.
    """
    distance: ColumnElement[Any] = null() if camera is None else metres_from(*camera)
    scope: list[ColumnElement[bool]] = [_ON_THE_LIST if my else or_(_ON_THE_LIST, in_the_commons())]
    if tag_id is not None:
        scope.append(carrying_tag(tag_id))
    if commons_match:
        scope.append(has_commons_match(user_id))
    if species_id is not None:
        scope.append(where_seen(species_id, user_id=user_id))

    if term is None and not my and camera is None:
        data = await get_lookup_page(
            db,
            model=DiveSite,
            columns=(*_COLUMNS, null().label("distance_m"), null().label("match"), literal(TIER_OWN).label("tier")),
            conditions=(_ON_THE_LIST,),
            used_by=DiveDiveSite.dive_site_id,
            user_id=user_id,
            bound=until,
            offset=offset,
            limit=limit,
            from_clause=_with_the_list(user_id),
            created_at=func.coalesce(SavedSite.created_at, DiveSite.created_at),
        )
        return SiteSearchPage(rows=data["data"], total_count=data["total_count"], matched_count=data["total_count"])

    if term is None:
        conditions = list(scope)
        if not my:
            conditions.append(DiveSite.latitude.is_not(None))
        if box is not None:
            conditions.append(box.holds())
        tier: ColumnElement[Any] = literal(TIER_OWN)
        match: ColumnElement[Any] = null()
        statement = select(*_COLUMNS, distance.label("distance_m"), match.label("match"), tier.label("tier"))
        statement = statement.select_from(_with_the_list(user_id)).where(*conditions)
        if my:
            statement = in_list_order(statement, sort or DiveSiteListSort.LAST_DIVED_ON, user_id=user_id)
        else:
            statement = statement.order_by(distance.asc(), DiveSite.name.asc(), DiveSite.id.asc())
        return await _page(db, statement, conditions, user_id=user_id, offset=offset, limit=limit)

    name_match = name_contains(term)
    locality_match = _locality_contains(term) if my else false()
    typo = (fold(term).op("<%")(search_text())) if fuzzy else false()
    if fuzzy:
        # `<%` reads its threshold from this setting, local to the transaction the page runs in.
        await db.execute(select(func.set_config("pg_trgm.word_similarity_threshold", str(FUZZY_SIMILARITY), True)))

    conditions = [*scope, or_(name_match, locality_match, typo)]
    if box is not None:
        conditions.append(box.holds())
    rank = case((_name_is(term), 0), (_name_starts(term), 1), (name_match, 2), else_=3)
    tier = case(
        (and_(name_match, _ON_THE_LIST), TIER_OWN),
        (name_match, TIER_COMMONS),
        (locality_match, TIER_OWN),
        else_=TIER_FUZZY,
    )
    match = case(
        (_name_is(term), DiveSiteSearchMatch.EXACT.value),
        (_name_starts(term), DiveSiteSearchMatch.PREFIX.value),
        (name_match, DiveSiteSearchMatch.CONTAINS.value),
        (locality_match, null()),
        else_=DiveSiteSearchMatch.FUZZY.value,
    )
    statement = (
        select(*_COLUMNS, distance.label("distance_m"), match.label("match"), tier.label("tier"))
        .select_from(_with_the_list(user_id))
        .where(*conditions)
    )
    if sort is not None:
        statement = in_list_order(statement, sort, user_id=user_id)
    else:
        resemblance = case((tier == TIER_FUZZY, func.word_similarity(fold(term), search_text())), else_=literal(0.0))
        nearer = () if camera is None else (distance.asc().nulls_last(),)
        statement = statement.order_by(tier, rank, resemblance.desc(), *nearer, DiveSite.name.asc(), DiveSite.id.asc())
    return await _page(
        db, statement, conditions, user_id=user_id, offset=offset, limit=limit, matched=or_(name_match, locality_match)
    )


async def _page(
    db: AsyncSession,
    statement: Any,
    conditions: list[ColumnElement[bool]],
    *,
    user_id: int,
    offset: int,
    limit: int,
    matched: ColumnElement[bool] | None = None,
) -> SiteSearchPage:
    counted = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(true() if matched is None else matched),
            )
            .select_from(_with_the_list(user_id))
            .where(*conditions)
        )
    ).one()
    rows = (await db.execute(statement.offset(offset).limit(limit))).mappings().all()
    return SiteSearchPage(rows=[dict(row) for row in rows], total_count=counted[0], matched_count=counted[1])


async def count_sites_near(
    db: AsyncSession,
    *,
    user_id: int,
    latitude: float,
    longitude: float,
    box: Box | None,
) -> int:
    """The sites this diver can see near a place: inside its box, or within `NEARBY_RADIUS_M`
    of its point where it has none."""
    if box is not None:
        near = box.holds()
    else:
        near = and_(
            box_around(latitude, longitude, NEARBY_RADIUS_M).holds(),
            metres_from(latitude, longitude) <= NEARBY_RADIUS_M,
        )
    count = await db.scalar(select(func.count()).select_from(DiveSite).where(readable_by(user_id), near))
    return count or 0
