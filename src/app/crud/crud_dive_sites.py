import uuid as uuid_pkg
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, NamedTuple

from fastcrud import FastCRUD
from sqlalchemy import ColumnElement, func, inspect, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_dive_start_time
from ..core.utils.search import LIKE_ESCAPE_CHAR, escape_like, search_clause
from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_site import DiveSite
from ..models.dive_site_tag import DiveSiteTag
from ..models.dive_species import DiveSpecies
from ..schemas.dive_site import (
    DiveSiteCreateInternal,
    DiveSiteListSort,
    DiveSiteReadInternal,
    DiveSiteSummary,
    DiveSiteUpdate,
    DiveSiteUpdateInternal,
)
from .crud_dives import date_only_of_the, offset_of_the

CRUDDiveSite = FastCRUD[
    DiveSite, DiveSiteCreateInternal, DiveSiteUpdate, DiveSiteUpdateInternal, DiveSiteUpdate, DiveSiteReadInternal
]
crud_dive_sites = CRUDDiveSite(DiveSite)

# The columns `GET /dive-sites?search=` matches as they are: the name, and the locality's,
# because that is how people remember sites they haven't dived in a while ("that wall in
# Dahab") - see DECISIONS.md. The other names are the third thing it matches, through
# `search_condition`, being list elements rather than a column. Named rather than inlined,
# on `COURSE_SEARCH_COLUMNS`' precedent: `test_picker_search.py` asserts these exist.
DIVE_SITE_SEARCH_COLUMNS = ("name", "location_name")


def search_condition(term: str) -> ColumnElement[bool]:
    """A case-insensitive substring of the name, the locality or any other name.

    The other names are matched element by element through `json_array_elements_text`, which
    hands back each as the text it is. Matching the column's own text instead would miss
    every name outside ASCII: the JSON is stored with ASCII escapes, so `砂辺` sits in it as
    `\\u7802\\u8fba` and no pattern of the name finds it.
    """
    other_name = func.json_array_elements_text(DiveSite.other_names).table_valued("value")
    pattern = f"%{escape_like(term)}%"
    return or_(
        search_clause(DiveSite, DIVE_SITE_SEARCH_COLUMNS, term),
        select(literal(1))
        .select_from(other_name)
        .where(other_name.c.value.ilike(pattern, escape=LIKE_ESCAPE_CHAR))
        .exists(),
    )


def carrying_tag(tag_id: int) -> ColumnElement[bool]:
    """The sites carrying this tag - what the tag's `site_count` counts."""
    return DiveSite.id.in_(select(DiveSiteTag.dive_site_id).where(DiveSiteTag.tag_id == tag_id))


def _live_dives_at_sites(columns: Sequence[Any], *, user_id: int) -> Any:
    """`dive_dive_site` narrowed to the diver's live dives, one row per site and dive.

    A dive names a site at most once (`ux_dive_dive_site_dive_id_dive_site_id`), so a count
    over these rows is a count of dives, and every position counts: the second site of a
    drift dive was dived as much as the first. `dive_dive_site` carries no liveness of its
    own, and a soft-deleted dive keeps its rows, so `is_deleted` is read through the join -
    as a trip's counts read it.
    """
    return (
        select(*columns)
        .select_from(DiveDiveSite)
        .join(
            Dive,
            (Dive.id == DiveDiveSite.dive_id) & (Dive.user_id == user_id) & Dive.is_deleted.is_(False),
        )
        .group_by(DiveDiveSite.dive_site_id)
    )


async def get_summaries_for_dive_sites(
    db: AsyncSession, *, dive_site_ids: Sequence[int], user_id: int
) -> dict[int, DiveSiteSummary]:
    """What the diver's live dives say of each of these sites, in two grouped queries.

    Two because the species fan each dive out into a row per sighting: a mean rating over
    that join would weigh a dive by how many species it saw. Every requested id gets an
    entry, an empty summary for a site no live dive names.

    `last_dived_on` is the latest dive's own local date, as the life list dates a sighting:
    its stored offset re-attached through `combine_dive_start_time`, or the bare date where
    only the day was recorded.
    """
    if not dive_site_ids:
        return {}
    wanted = DiveDiveSite.dive_site_id.in_(set(dive_site_ids))
    figures = await db.execute(
        _live_dives_at_sites(
            (
                DiveDiveSite.dive_site_id,
                func.count(Dive.id).label("dive_count"),
                func.max(Dive.start_time).label("last_start"),
                offset_of_the(Dive.start_time.desc()).label("last_offset"),
                date_only_of_the(Dive.start_time.desc()).label("last_date_only"),
                func.max(Dive.max_depth).label("max_dive_depth"),
                func.avg(Dive.rating).label("average_rating"),
            ),
            user_id=user_id,
        ).where(wanted)
    )
    species = await db.execute(
        _live_dives_at_sites(
            (DiveDiveSite.dive_site_id, func.count(func.distinct(DiveSpecies.species_id)).label("species_count")),
            user_id=user_id,
        )
        .join(DiveSpecies, DiveSpecies.dive_id == Dive.id)
        .where(wanted)
    )
    species_by_site = {row.dive_site_id: row.species_count for row in species}

    summaries = {site_id: DiveSiteSummary() for site_id in dive_site_ids}
    for row in figures:
        summaries[row.dive_site_id] = DiveSiteSummary(
            dive_count=row.dive_count,
            last_dived_on=_local_date(combine_dive_start_time(row.last_start, row.last_offset, row.last_date_only)),
            max_dive_depth=row.max_dive_depth,
            species_count=species_by_site.get(row.dive_site_id, 0),
            average_rating=None if row.average_rating is None else float(row.average_rating),
        )
    return summaries


def _local_date(start: datetime | date) -> date:
    return start.date() if isinstance(start, datetime) else start


async def get_dive_sites_page(
    db: AsyncSession,
    *,
    user_id: int,
    offset: int,
    limit: int,
    search: str | None,
    tag_id: int | None,
    sort: DiveSiteListSort,
) -> dict[str, Any]:
    """One page of a diver's sites, in `get_multi`'s `{data, total_count}` shape.

    Hand-written for the two orders on the summary, which are aggregates over the dives
    rather than columns of the site: the most dived first, and the most recently dived first
    with a site no live dive names after every one that has. Ties go by name either way.
    Rows are plain dicts of every mapped column, as `search_multi` returns them.
    """
    conditions: list[ColumnElement[bool]] = [DiveSite.user_id == user_id]
    if search:
        conditions.append(search_condition(search))
    if tag_id is not None:
        conditions.append(carrying_tag(tag_id))

    total_count = await db.scalar(select(func.count()).select_from(DiveSite).where(*conditions))

    statement = select(*inspect(DiveSite).columns).where(*conditions)
    by_name = (DiveSite.name.asc(), DiveSite.id.asc())
    if sort is DiveSiteListSort.NAME:
        statement = statement.order_by(*by_name)
    else:
        dived = (
            _live_dives_at_sites(
                (
                    DiveDiveSite.dive_site_id,
                    func.count(Dive.id).label("dive_count"),
                    func.max(Dive.start_time).label("last_start"),
                ),
                user_id=user_id,
            )
        ).subquery()
        statement = statement.outerjoin(dived, dived.c.dive_site_id == DiveSite.id)
        first = (
            func.coalesce(dived.c.dive_count, 0).desc()
            if sort is DiveSiteListSort.DIVE_COUNT
            else dived.c.last_start.desc().nulls_last()
        )
        statement = statement.order_by(first, *by_name)

    rows = (await db.execute(statement.offset(offset).limit(limit))).mappings().all()
    return {"data": [dict(row) for row in rows], "total_count": total_count or 0}


class HeldSite(NamedTuple):
    id: int
    uuid: uuid_pkg.UUID
    name: str


async def sites_by_external_id(db: AsyncSession, *, user_id: int) -> dict[tuple[str, str], list[HeldSite]]:
    """Every registry entry the diver's sites carry, with the sites carrying it, by name.

    One query over the diver's own sites with any entry at all - a few hundred rows at the
    outside, the profile of `GET /dives`. Matched in Python rather than in SQL because the
    column is JSON, which has no containment operator, and the answer is wanted for a list
    of pairs at once.
    """
    rows = await db.execute(
        select(DiveSite.id, DiveSite.uuid, DiveSite.name, DiveSite.external_ids)
        .where(DiveSite.user_id == user_id, func.json_array_length(DiveSite.external_ids) > 0)
        .order_by(DiveSite.name, DiveSite.id)
    )
    held: dict[tuple[str, str], list[HeldSite]] = {}
    for row in rows:
        site = HeldSite(id=row.id, uuid=row.uuid, name=row.name)
        for entry in row.external_ids:
            held.setdefault((entry["registry"], entry["identifier"]), []).append(site)
    return held


async def resolve_dive_site_ids_for_user(
    db: AsyncSession, dive_site_uuids: list[uuid_pkg.UUID], user_id: int
) -> dict[uuid_pkg.UUID, int] | None:
    """Resolve dive site public `uuid`s to their internal `id`s, scoped to dive sites
    belonging to the given user.

    Returns `None` if any given uuid doesn't resolve to a dive site owned by the user
    (used to prevent a user from linking another user's dive site(s) to their own dive).
    """
    unique_uuids = set(dive_site_uuids)
    if not unique_uuids:
        return {}

    stmt = select(DiveSite.uuid, DiveSite.id).where(
        DiveSite.uuid.in_(unique_uuids),
        DiveSite.user_id == user_id,
    )
    result = await db.execute(stmt)
    mapping = {row.uuid: row.id for row in result}
    if mapping.keys() != unique_uuids:
        return None
    return mapping


async def dive_site_name_exists(
    db: AsyncSession, user_id: int, name: str, location_name: str | None = None, exclude_id: int | None = None
) -> bool:
    """Case-insensitive check for whether a dive site with the same (name, locality name)
    already exists for the user.

    Mirrors the `ux_dive_site_user_id_name_location_lower` unique index, which keys on the
    locality's *name* and none of its other members. Two sites with no locality and the same
    name are treated as duplicates.
    """
    stmt = select(DiveSite.id).where(
        DiveSite.user_id == user_id,
        func.lower(DiveSite.name) == name.strip().lower(),
    )
    if location_name is None:
        stmt = stmt.where(DiveSite.location_name.is_(None))
    else:
        stmt = stmt.where(func.lower(DiveSite.location_name) == location_name.strip().lower())
    if exclude_id is not None:
        stmt = stmt.where(DiveSite.id != exclude_id)

    result = await db.execute(stmt.limit(1))
    return result.first() is not None
