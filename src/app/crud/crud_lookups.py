"""The query behind every `GET /<plural>/lookup`: a page of one resource's rows, the ones the
caller's dives used most recently first.

Shared because the six lookups differ only in what they select, what they search and which
dive column or join table names the item; the order is one rule, and one spelling of it is
what keeps the six from drifting apart.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, FromClause, Select, Subquery, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from ..models.dive import Dive


def _uses(item: InstrumentedAttribute[Any], *, user_id: int, bound: datetime | None) -> Select[Any]:
    """The start of each of the caller's live dives naming an item, at or before `bound`.

    `item` is the dive's own column (`Dive.trip_id`) or a join table's (`DivePerson.person_id`),
    in which case the join table is read through to its dive.
    """
    statement = select(item.label("item_id"), Dive.start_time.label("start_time"))
    host = item.class_
    if host is not Dive:
        statement = statement.select_from(host).join(Dive, Dive.id == host.dive_id)
    statement = statement.where(Dive.user_id == user_id, Dive.is_deleted.is_(False), item.is_not(None))
    if bound is not None:
        statement = statement.where(Dive.start_time <= bound)
    return statement


def _last_use(items: tuple[InstrumentedAttribute[Any], ...], *, user_id: int, bound: datetime | None) -> Subquery:
    """The latest start of the caller's live dives naming each item through any of `items`,
    at or before `bound`. The shape is the site list's last-dived order, with the bound inside
    so a dive after it counts for nothing."""
    if len(items) == 1:
        uses = _uses(items[0], user_id=user_id, bound=bound).subquery()
    else:
        uses = union_all(*(_uses(item, user_id=user_id, bound=bound) for item in items)).subquery()
    return select(uses.c.item_id, func.max(uses.c.start_time).label("last_start")).group_by(uses.c.item_id).subquery()


async def get_lookup_page(
    db: AsyncSession,
    *,
    model: Any,
    columns: tuple[Any, ...],
    conditions: tuple[ColumnElement[bool], ...],
    used_by: InstrumentedAttribute[Any],
    also_used_by: InstrumentedAttribute[Any] | None = None,
    user_id: int,
    bound: datetime | None,
    offset: int,
    limit: int,
    from_clause: FromClause | None = None,
) -> dict[str, Any]:
    """One page of `columns` over `model`'s rows matching `conditions`, in `get_multi`'s
    `{data, total_count}` shape.

    Ordered by last use at or before `bound`, then never-used newest first, then name, then id:
    an item created a minute ago is what a diver logging their first dive with it reaches for.
    `from_clause` replaces `model` as what the rows are read from, for a search reaching a
    joined table. `also_used_by` is a second column naming the item on a dive, for the one
    resource a dive names two ways - a gear item listed in the gear and linked as a cylinder's
    vessel - so both count as a use.
    """
    source = model if from_clause is None else from_clause
    total_count = await db.scalar(select(func.count()).select_from(source).where(*conditions))

    sources = (used_by,) if also_used_by is None else (used_by, also_used_by)
    used = _last_use(sources, user_id=user_id, bound=bound)
    statement = (
        select(*columns)
        .select_from(source)
        .outerjoin(used, used.c.item_id == model.id)
        .where(*conditions)
        .order_by(used.c.last_start.desc().nulls_last(), model.created_at.desc(), model.name.asc(), model.id.asc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(statement)).mappings().all()
    return {"data": [dict(row) for row in rows], "total_count": total_count or 0}
