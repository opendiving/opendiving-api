import math
import uuid as uuid_pkg
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, NamedTuple

from fastcrud import FastCRUD
from sqlalchemy import ColumnElement, Select, case, delete, func, inspect, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_dive_start_time
from ..core.utils.search import LIKE_ESCAPE_CHAR, escape_like, search_clause
from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_site import DiveSite
from ..models.dive_site_tag import DiveSiteTag
from ..models.dive_species import DiveSpecies
from ..models.saved_site import SavedSite
from ..models.tag import Tag
from ..schemas.dive_site import (
    DiveSiteCreateInternal,
    DiveSiteListSort,
    DiveSiteReadInternal,
    DiveSiteSummary,
    DiveSiteUpdate,
    DiveSiteUpdateInternal,
)
from .crud_dives import date_only_of_the, offset_of_the
from .unique_keys import unique_key_taken

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

_SAVED_SITE_KEY = "ux_saved_site_user_id_dive_site_id"


def saved_by(user_id: int) -> ColumnElement[bool]:
    """The sites on this diver's list: their own, and the Commons records they have saved."""
    return DiveSite.id.in_(select(SavedSite.dive_site_id).where(SavedSite.user_id == user_id))


def in_the_commons() -> ColumnElement[bool]:
    """The rows the Commons owns, which every account can read."""
    return DiveSite.user_id.is_(None)


def readable_by(user_id: int) -> ColumnElement[bool]:
    """What this diver may name wherever a site is named: their list, and any Commons record."""
    return or_(in_the_commons(), saved_by(user_id))


def _select_sites(user_id: int, *, on_the_list_only: bool) -> Select[Any]:
    """Every mapped column of a site, with what this diver's saved row says of it, if they have one.

    `notes` and `created_at` are the saved row's. A Commons record the diver has not saved has
    no such row, so it reads with no notes and the date it became a Commons record.
    """
    site_columns = [column for column in inspect(DiveSite).columns if column.key != "created_at"]
    return select(
        *site_columns,
        func.coalesce(SavedSite.notes, "").label("notes"),
        func.coalesce(SavedSite.created_at, DiveSite.created_at).label("created_at"),
        SavedSite.id.is_not(None).label("saved"),
    ).join_from(
        DiveSite,
        SavedSite,
        (SavedSite.dive_site_id == DiveSite.id) & (SavedSite.user_id == user_id),
        isouter=not on_the_list_only,
    )


async def get_readable_dive_site(db: AsyncSession, *, uuid: uuid_pkg.UUID, user_id: int) -> dict[str, Any] | None:
    """One site in `_select_sites`' shape, or `None` if this diver may not read it."""
    statement = _select_sites(user_id, on_the_list_only=False).where(DiveSite.uuid == uuid, readable_by(user_id))
    row = (await db.execute(statement)).mappings().one_or_none()
    return None if row is None else dict(row)


def add_saved_site(db: AsyncSession, *, user_id: int, dive_site_id: int, notes: str, created_at: datetime) -> None:
    """Put a new site on its owner's list, in the transaction that creates it. Stamped with the
    site's own `created_at`, so a read answers the instant the create did."""
    db.add(SavedSite(user_id=user_id, dive_site_id=dive_site_id, notes=notes, created_at=created_at))


async def is_saved(db: AsyncSession, *, user_id: int, dive_site_id: int) -> bool:
    saved = await db.scalar(
        select(SavedSite.id).where(SavedSite.user_id == user_id, SavedSite.dive_site_id == dive_site_id)
    )
    return saved is not None


async def save_commons_site(db: AsyncSession, *, user_id: int, dive_site_id: int, notes: str) -> bool:
    """Whether this put the record on the diver's list, which it does not where it already is:
    a retry changes nothing, and two at once leave one row. Does not commit."""
    written = await db.scalar(
        insert(SavedSite)
        .values(user_id=user_id, dive_site_id=dive_site_id, notes=notes, created_at=datetime.now(UTC))
        .on_conflict_do_nothing(constraint=_SAVED_SITE_KEY)
        .returning(SavedSite.id)
    )
    return written is not None


async def save_named_commons_sites(db: AsyncSession, *, user_id: int, dive_site_ids: Sequence[int]) -> None:
    """Put on the diver's list every Commons record among these sites, as naming one on a dive
    does. Their own sites and records already saved are left as they are. Does not commit."""
    if not dive_site_ids:
        return
    await db.execute(
        insert(SavedSite)
        .from_select(
            ["user_id", "dive_site_id", "notes", "created_at"],
            select(literal(user_id), DiveSite.id, literal(""), literal(datetime.now(UTC))).where(
                DiveSite.id.in_(set(dive_site_ids)), in_the_commons()
            ),
        )
        .on_conflict_do_nothing(constraint=_SAVED_SITE_KEY)
    )


async def remove_saved_commons_site(db: AsyncSession, *, user_id: int, dive_site_id: int) -> None:
    """Take a Commons record off the diver's list: their saved row, their tags on it and their
    dives' links to it, which is what deleting their own site takes through its cascades. Every
    other saver's rows on the record stay. Does not commit."""
    await db.execute(
        delete(DiveDiveSite).where(
            DiveDiveSite.dive_site_id == dive_site_id,
            DiveDiveSite.dive_id.in_(select(Dive.id).where(Dive.user_id == user_id)),
        )
    )
    await db.execute(
        delete(DiveSiteTag).where(
            DiveSiteTag.dive_site_id == dive_site_id,
            DiveSiteTag.tag_id.in_(select(Tag.id).where(Tag.user_id == user_id)),
        )
    )
    await db.execute(delete(SavedSite).where(SavedSite.user_id == user_id, SavedSite.dive_site_id == dive_site_id))


async def replace_saved_site_notes(db: AsyncSession, *, user_id: int, dive_site_id: int, notes: str) -> bool:
    """Whether the diver had this site on their list to write the notes to. Does not commit."""
    written = await db.scalar(
        update(SavedSite)
        .where(SavedSite.user_id == user_id, SavedSite.dive_site_id == dive_site_id)
        .values(notes=notes, updated_at=datetime.now(UTC))
        .returning(SavedSite.id)
    )
    return written is not None


def search_condition(term: str) -> ColumnElement[bool]:
    """A case-insensitive substring of the name, the locality or any other name.

    The other names are matched element by element through `json_array_elements_text`, which
    hands back each as the text it is. Matching the column's own text instead would miss
    every name outside ASCII: the JSON is stored with ASCII escapes, so `砂辺` sits in it as
    `\\u7802\\u8fba` and no pattern of the name finds it.
    """
    return or_(search_clause(DiveSite, DIVE_SITE_SEARCH_COLUMNS, term), _any_other_name_ilike(f"%{escape_like(term)}%"))


def _any_other_name_ilike(pattern: str) -> ColumnElement[bool]:
    other_name = func.json_array_elements_text(DiveSite.other_names).table_valued("value")
    return (
        select(literal(1))
        .select_from(other_name)
        .where(other_name.c.value.ilike(pattern, escape=LIKE_ESCAPE_CHAR))
        .exists()
    )


def _name_ilike(pattern: str) -> ColumnElement[bool]:
    return or_(DiveSite.name.ilike(pattern, escape=LIKE_ESCAPE_CHAR), _any_other_name_ilike(pattern))


def _distance_from(latitude: float, longitude: float) -> ColumnElement[float]:
    """Great-circle distance by the haversine formula, in radians of arc: this orders rows and
    never leaves the query. Trigonometry rather than a planar difference, because a search from
    Egypt matches sites in Indonesia and a flat approximation reorders those."""
    pin_latitude, pin_longitude = func.radians(DiveSite.latitude), func.radians(DiveSite.longitude)
    from_latitude, from_longitude = math.radians(latitude), math.radians(longitude)
    half_chord = func.power(func.sin((pin_latitude - from_latitude) / 2), 2) + math.cos(from_latitude) * func.cos(
        pin_latitude
    ) * func.power(func.sin((pin_longitude - from_longitude) / 2), 2)
    return func.asin(func.least(1.0, func.sqrt(half_chord)))


async def get_unsaved_commons_page(
    db: AsyncSession,
    *,
    columns: tuple[Any, ...],
    user_id: int,
    search: str,
    uuids: Sequence[uuid_pkg.UUID] | None,
    position: tuple[float, float] | None,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """The Commons records this diver has not saved whose name or other names match, in
    `get_multi`'s `{data, total_count}` shape: exact names before prefixes before substrings,
    nearer first where a position is given, then by name.

    The locality is not searched: it is what tells a diver's own sites apart, and across the
    world's sites a town's name matches everything near it. Nearness is computed over the rows
    the name already narrowed, so no spatial index is asked of the table.
    """
    escaped = escape_like(search)
    conditions: list[ColumnElement[bool]] = [in_the_commons(), ~saved_by(user_id), _name_ilike(f"%{escaped}%")]
    if uuids:
        conditions.append(DiveSite.uuid.in_(uuids))
    total_count = await db.scalar(select(func.count()).select_from(DiveSite).where(*conditions))
    if not limit:
        return {"data": [], "total_count": total_count or 0}

    match = case((_name_ilike(escaped), 0), (_name_ilike(f"{escaped}%"), 1), else_=2)
    nearer = () if position is None else (_distance_from(*position).asc().nulls_last(),)
    statement = (
        select(*columns)
        .where(*conditions)
        .order_by(match, *nearer, DiveSite.name.asc(), DiveSite.id.asc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(statement)).mappings().all()
    return {"data": [dict(row) for row in rows], "total_count": total_count or 0}


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
    Rows are plain dicts, `_select_sites`' shape.
    """
    conditions: list[ColumnElement[bool]] = []
    if search:
        conditions.append(search_condition(search))
    if tag_id is not None:
        conditions.append(carrying_tag(tag_id))

    total_count = await db.scalar(select(func.count()).select_from(DiveSite).where(saved_by(user_id), *conditions))

    statement = _select_sites(user_id, on_the_list_only=True).where(*conditions)
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
        .where(saved_by(user_id), func.json_array_length(DiveSite.external_ids) > 0)
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
    """Resolve dive site public `uuid`s to their internal `id`s, scoped to the sites the given
    user may name: their list, and any Commons record.

    Returns `None` if any given uuid doesn't resolve to one of those (used to prevent a user
    from linking another user's dive site(s) to their own dive).
    """
    unique_uuids = set(dive_site_uuids)
    if not unique_uuids:
        return {}

    stmt = select(DiveSite.uuid, DiveSite.id).where(
        DiveSite.uuid.in_(unique_uuids),
        readable_by(user_id),
    )
    result = await db.execute(stmt)
    mapping = {row.uuid: row.id for row in result}
    if mapping.keys() != unique_uuids:
        return None
    return mapping


DIVE_SITE_NAME_KEY = "ux_dive_site_user_id_name_location_lower"


async def dive_site_name_exists(
    db: AsyncSession, user_id: int, name: str, location_name: str | None = None, exclude_id: int | None = None
) -> bool:
    """Asked on `ux_dive_site_user_id_name_location_lower`'s own expressions, which key on the
    locality's *name* and none of its other members: two sites with no locality and the same
    name are duplicates."""
    return await unique_key_taken(
        db,
        DiveSite,
        DIVE_SITE_NAME_KEY,
        {"user_id": user_id, "name": name, "location_name": location_name},
        exclude_id=exclude_id,
    )
