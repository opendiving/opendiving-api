"""Every species one diver has ever logged, with their own history of each.

**The first hand-written aggregate in this API**, and it is hand-written for the reason the
house rule gives: FastCRUD serves the plain-row path, and anything with a join, an aggregate
or a non-trivial ordering is a `select()` returning `{"data": ..., "total_count": ...}` so
`paginated_response` still works. A grep for `group_by` across `src/app/` found one hit before
this, and it was a search-ranking collapse rather than a statistics query.

It sits beside `services/dive_gas.py` and `services/dive_activity.py`: one module per derived
view of a diver's own log, the route above it doing nothing but authorize and cache. Unlike
those two it is paginated, because this one is a browsable collection rather than a series to
plot - which makes it the first paginated route in the `/user/` family, a cost recorded rather
than rediscovered.

**Why it is not under `/species/`.** That router is the one thing in `api/v1` that belongs to
nobody, stated as such in its own docstring and in DECISIONS.md, and a user-scoped route there
would make it the first mixed-ownership namespace in the API. A life list is "the caller's own
log as a whole", which is exactly what `/user/dive-stats`, `/user/gas-use-history` and
`/user/dive-activity` are. Preserving a documented invariant beats matching the pagination
family's path shape.
"""

import uuid as uuid_pkg
from collections.abc import Awaitable, Callable, Collection
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_dive_start_time
from ..core.utils.search import LIKE_ESCAPE_CHAR, escape_like
from ..crud.crud_dives import at_dive_site, date_only_of_the, offset_of_the
from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_species import DiveSpecies
from ..models.species import Species
from ..models.species_name import SpeciesName
from ..schemas.species import (
    SpeciesLifeListDetail,
    SpeciesLifeListEntry,
    SpeciesSuggestion,
    SpeciesSuggestResponse,
)
from .species_service import _MAX_RESULTS, _WORMS_ATTRIBUTION, search_species


def _sighting_join(statement: Any, *, user_id: int) -> Any:
    """Narrow a `species`-rooted statement to the taxa this diver has actually logged.

    **`dive_species` carries no liveness of its own**, and its `dive_id` cascade is *dormant*:
    dives are soft-deleted, so no `DELETE FROM dive` is ever issued and a deleted dive's join
    rows outlive it. Every read here therefore has to reach through to `Dive.is_deleted`,
    exactly as `recalculate_dive_stats` does. The failure direction is what makes it worth a
    shared helper rather than two copies - forgetting it shows a diver dives they deleted, and
    it does so quietly.
    """
    return statement.join(DiveSpecies, DiveSpecies.species_id == Species.id).join(
        Dive, (Dive.id == DiveSpecies.dive_id) & (Dive.user_id == user_id) & (Dive.is_deleted.is_(False))
    )


def _search_clause(search: str) -> Any:
    """Match a species by the same names the catalog search already matches on.

    An `EXISTS` over `species_name` rather than a join, so a species with three matching
    aliases stays one row and the aggregates below are not multiplied by the alias count -
    which a join would do silently, turning `dive_count` into `dives x aliases`.
    `scientific_name` is matched alongside it for the reason `_local_search` gives: a row whose
    `species_name` rows failed to write is still findable by its own name.
    """
    pattern = f"%{escape_like(search)}%"
    matching_alias = (
        select(SpeciesName.id)
        .where(SpeciesName.species_id == Species.id, SpeciesName.name.ilike(pattern, escape=LIKE_ESCAPE_CHAR))
        .exists()
    )
    return or_(matching_alias, Species.scientific_name.ilike(pattern, escape=LIKE_ESCAPE_CHAR))


_LAST_SEEN = func.max(Dive.start_time).label("last_seen")


def _history_columns() -> tuple[Any, ...]:
    """The taxon and the diver's history with it - one list entry's columns, shared with the
    single-species read so the two can never count a species' dives differently."""
    return (
        Species.uuid,
        Species.scientific_name,
        Species.common_name,
        Species.rank,
        Species.photo_sha256,
        # `DISTINCT` because a dive can only carry a species once - the unique constraint on
        # `dive_species` says so - but the count has to survive anything a join adds here, as
        # the single-species read's join to `dive_dive_site` does, rather than depending on that.
        func.count(func.distinct(Dive.id)).label("dive_count"),
        func.min(Dive.start_time).label("first_seen"),
        _LAST_SEEN,
        offset_of_the(Dive.start_time.asc()).label("first_offset"),
        offset_of_the(Dive.start_time.desc()).label("last_offset"),
        date_only_of_the(Dive.start_time.asc()).label("first_date_only"),
        date_only_of_the(Dive.start_time.desc()).label("last_date_only"),
    )


def _history_fields(row: Any) -> dict[str, Any]:
    return {
        "uuid": row.uuid,
        "scientific_name": row.scientific_name,
        "common_name": row.common_name,
        "rank": row.rank,
        "photo_sha256": row.photo_sha256,
        "dive_count": row.dive_count,
        "first_seen": combine_dive_start_time(row.first_seen, row.first_offset, row.first_date_only),
        "last_seen": combine_dive_start_time(row.last_seen, row.last_offset, row.last_date_only),
    }


async def species_life_list(
    db: AsyncSession,
    *,
    user_id: int,
    offset: int,
    limit: int,
    search: str | None = None,
    dive_site_id: int | None = None,
) -> dict[str, Any]:
    """One page of the diver's life list, plus the total, in `get_multi`'s shape.

    Ordered most-recently-seen first: a life list is read to see what you saw lately, and the
    newest sighting is the one a diver is looking for. `Species.id` breaks ties so two species
    last seen on the same dive keep a stable order between two identical requests.

    **The total is `COUNT(DISTINCT species)` over the same join, which is what makes it equal
    the dashboard's `species_seen` tile** - that stat is the same count computed on the write
    path by `recalculate_dive_stats`. The two agreeing for any diver is the cheapest
    end-to-end check this feature has, and it only holds because both reach through to
    `Dive.is_deleted`.

    **The two dates come back in the offset the diver logged those dives in**, which takes more
    than a `min()`/`max()` - see `offset_of_the`. Ordering is by the UTC instant either way:
    "first seen" means the earliest dive chronologically, whatever local time it read as.
    """
    conditions = [] if search is None else [_search_clause(search)]
    if dive_site_id is not None:
        # A dive naming the site at any position, as the site's summary counts it - so a
        # site page's species list is as long as its species count.
        conditions.append(at_dive_site(dive_site_id))

    total_count = await db.scalar(
        _sighting_join(select(func.count(func.distinct(Species.id))), user_id=user_id).where(*conditions)
    )

    rows = (
        await db.execute(
            _sighting_join(select(*_history_columns()), user_id=user_id)
            .where(*conditions)
            # Grouped by the primary key alone: every other selected `species` column is
            # functionally dependent on it, which Postgres understands.
            .group_by(Species.id)
            .order_by(_LAST_SEEN.desc(), Species.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()

    return {
        "data": [SpeciesLifeListEntry(**_history_fields(row)).model_dump() for row in rows],
        "total_count": total_count or 0,
    }


async def species_life_list_detail(
    db: AsyncSession, *, user_id: int, species_uuid: uuid_pkg.UUID
) -> SpeciesLifeListDetail | None:
    """One species' life-list entry, plus how many of the diver's sites those dives name.

    `None` when no live dive of theirs records it - the same condition that keeps it off the
    list - whether or not the catalog holds the species.

    Sites are counted as the site summary counts dives: a dive naming a site at any position
    counts for it. The outer join keeps a species sighted only on site-less dives, at a site
    count of 0; the duplicate rows it makes for a multi-site dive are why every aggregate
    here is distinct or order-picked.
    """
    row = (
        await db.execute(
            _sighting_join(
                select(
                    *_history_columns(),
                    func.count(func.distinct(DiveDiveSite.dive_site_id)).label("dive_site_count"),
                ),
                user_id=user_id,
            )
            .outerjoin(DiveDiveSite, DiveDiveSite.dive_id == Dive.id)
            .where(Species.uuid == species_uuid)
            .group_by(Species.id)
        )
    ).one_or_none()
    if row is None:
        return None
    return SpeciesLifeListDetail(**_history_fields(row), dive_site_count=row.dive_site_count)


async def suggest_species(
    db: AsyncSession,
    *,
    user_id: int,
    query: str,
    dive_site_ids: Collection[int],
    excluded_uuids: Collection[uuid_pkg.UUID],
    before_search: Callable[[], Awaitable[None]],
) -> SpeciesSuggestResponse:
    """The dive form's species menu: the caller's own species first, then the catalog search.

    The own rows are one ordering, `dive_count_at_sites DESC, last_seen DESC`, which is the
    three tiers' order in one key - every species logged at the form's sites precedes every
    one that was not, and the rest fall back on recency. They match `query` by the names a
    diver can read and never by a stored alias, so a foreign vernacular cannot fill the page
    ahead of the search; that species still arrives from the search tier, hinted.

    The search tier is asked only for two characters or more and only when the own rows leave
    room, and `before_search` - the caller's rate limit - runs exactly then, so opening the
    menu or typing one letter never spends the register budget.
    """
    normalized = " ".join(query.split()).casefold()
    count_at_sites = (
        func.count(func.distinct(Dive.id))
        .filter(Dive.id.in_(select(DiveDiveSite.dive_id).where(DiveDiveSite.dive_site_id.in_(dive_site_ids))))
        .label("dive_count_at_sites")
    )
    conditions: list[Any] = []
    if normalized:
        pattern = f"%{escape_like(normalized)}%"
        conditions.append(
            or_(
                Species.scientific_name.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
                Species.common_name.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
            )
        )
    if excluded_uuids:
        conditions.append(Species.uuid.not_in(excluded_uuids))

    rows = (
        await db.execute(
            _sighting_join(
                select(
                    Species.uuid,
                    Species.aphia_id,
                    Species.scientific_name,
                    Species.common_name,
                    Species.rank,
                    Species.status,
                    count_at_sites,
                    _LAST_SEEN,
                    offset_of_the(Dive.start_time.desc()).label("last_offset"),
                    date_only_of_the(Dive.start_time.desc()).label("last_date_only"),
                ),
                user_id=user_id,
            )
            .where(*conditions)
            .group_by(Species.id)
            .order_by(count_at_sites.desc(), _LAST_SEEN.desc(), Species.id)
            # One past the page, to tell a full page from a cut one.
            .limit(_MAX_RESULTS + 1)
        )
    ).all()
    # Built from columns before `search_species` releases the read transaction, so nothing
    # held here is an ORM object that release would expire.
    own = [
        SpeciesSuggestion(
            aphia_id=row.aphia_id,
            uuid=row.uuid,
            scientific_name=row.scientific_name,
            common_name=row.common_name,
            rank=row.rank,
            status=row.status,
            # A visible name explains every own match, so there is never a hidden one to name.
            matched_name=None,
            source="catalog",
            attribution=_WORMS_ATTRIBUTION,
            dive_count_at_sites=row.dive_count_at_sites,
            last_seen=combine_dive_start_time(row.last_seen, row.last_offset, row.last_date_only),
        )
        for row in rows
    ]

    if len(own) >= _MAX_RESULTS:
        # Cut, or full with the search never asked: either way there is more behind the page,
        # except under an empty query, which has no search behind it.
        return SpeciesSuggestResponse(results=own[:_MAX_RESULTS], has_more=len(own) > _MAX_RESULTS or bool(normalized))
    if len(normalized) < 2:
        # One letter is the caller's own species only; the catalog was not asked, so a short
        # list must not read as every species carrying that letter.
        return SpeciesSuggestResponse(results=own, has_more=bool(normalized))

    await before_search()
    searched = await search_species(db=db, query=normalized)
    shown = {suggestion.aphia_id for suggestion in own}
    excluded = set(excluded_uuids)
    appended = [
        SpeciesSuggestion(**result.model_dump())
        for result in searched.results
        if result.aphia_id not in shown and (result.uuid is None or result.uuid not in excluded)
    ]
    room = _MAX_RESULTS - len(own)
    return SpeciesSuggestResponse(results=own + appended[:room], has_more=searched.has_more or len(appended) > room)
