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

from typing import Any

from sqlalchemy import ARRAY, Integer, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_start_time
from ..core.utils.search import LIKE_ESCAPE_CHAR, escape_like
from ..models.dive import Dive
from ..models.dive_species import DiveSpecies
from ..models.species import Species
from ..models.species_name import SpeciesName
from ..schemas.species import SpeciesLifeListEntry


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


def _offset_of_the(order: Any) -> Any:
    """The `utc_offset_minutes` of the first dive in the group under `order`.

    **A dive displays in the timezone it was logged in**, which this endpoint has to honour like
    every other dive-derived surface - `_to_public_start_time`, `dive_neighbors`,
    `dive_activity` and `gas_use_history` all reconstruct it, and `DECISIONS.md` states it as the
    API contract. A `timestamptz` stores only an absolute instant, so a dive logged at 09:00 in
    Bangkok comes back as 02:00 UTC and would read as the wrong local time - and, for an evening
    dive, the wrong day.

    That is harder here than anywhere else in the app, because `first_seen` and `last_seen` are
    *aggregates*: the offset wanted is the one belonging to the single dive that produced the
    `min()` or the `max()`, and no aggregate over the offset column can say which that was.
    Ordering `array_agg` and taking its first element is what pairs the two, in one pass over the
    group that Postgres is already making. `Dive.id` breaks a tie between two dives at the same
    instant, so a diver with two logs at one timestamp does not get a different offset run to
    run.

    The conversion itself still happens in Python, through `combine_start_time`. Deliberately, and
    the same call `dive_activity` explains: `core/utils/datetime_offset.py` is documented as the
    single place that conversion happens, and the failure mode of a second copy of it in SQL is a
    list that quietly disagrees with the dive pages it was built from.
    """
    return func.array_agg(aggregate_order_by(Dive.utc_offset_minutes, order, Dive.id.asc()), type_=ARRAY(Integer))[1]


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


async def species_life_list(
    db: AsyncSession, *, user_id: int, offset: int, limit: int, search: str | None = None
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
    than a `min()`/`max()` - see `_offset_of_the`. Ordering is by the UTC instant either way:
    "first seen" means the earliest dive chronologically, whatever local time it read as.
    """
    conditions = [] if search is None else [_search_clause(search)]

    total_count = await db.scalar(
        _sighting_join(select(func.count(func.distinct(Species.id))), user_id=user_id).where(*conditions)
    )

    last_seen = func.max(Dive.start_time).label("last_seen")
    rows = (
        await db.execute(
            _sighting_join(
                select(
                    Species.uuid,
                    Species.scientific_name,
                    Species.common_name,
                    Species.rank,
                    Species.photo_sha256,
                    # `DISTINCT` because a dive can only carry a species once - the unique
                    # constraint on `dive_species` says so - but the count has to survive
                    # anything a future join adds here rather than depending on that.
                    func.count(func.distinct(Dive.id)).label("dive_count"),
                    func.min(Dive.start_time).label("first_seen"),
                    last_seen,
                    _offset_of_the(Dive.start_time.asc()).label("first_offset"),
                    _offset_of_the(Dive.start_time.desc()).label("last_offset"),
                ),
                user_id=user_id,
            )
            .where(*conditions)
            # Grouped by the primary key alone: every other selected `species` column is
            # functionally dependent on it, which Postgres understands.
            .group_by(Species.id)
            .order_by(last_seen.desc(), Species.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()

    return {
        "data": [
            SpeciesLifeListEntry(
                uuid=row.uuid,
                scientific_name=row.scientific_name,
                common_name=row.common_name,
                rank=row.rank,
                photo_sha256=row.photo_sha256,
                dive_count=row.dive_count,
                first_seen=combine_start_time(row.first_seen, row.first_offset),
                last_seen=combine_start_time(row.last_seen, row.last_offset),
            ).model_dump()
            for row in rows
        ],
        "total_count": total_count or 0,
    }
