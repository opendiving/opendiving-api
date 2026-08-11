"""Substring search over a user's own rows, for the dive form's pickers.

The dive form's trip/dive site/gear comboboxes used to fetch the user's entire
catalogue and filter it in the browser. They now narrow server-side as you type, which
is what these helpers back - see DECISIONS.md.

Deliberately *not* expressed as FastCRUD `get_multi` filter kwargs: those are AND'd
together, and its `__or` operator groups operators on a single column, so matching a
term against *either* a name or a location/brand can't be written that way.
"""

from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

LIKE_ESCAPE_CHAR = "\\"


def escape_like(term: str) -> str:
    """Escapes the `LIKE`/`ILIKE` wildcards in a user-supplied search term, so a gear item
    named "50% off" is searchable and typing "%" alone doesn't match everything.

    Pairs with `.ilike(pattern, escape=LIKE_ESCAPE_CHAR)`. The escape character itself has
    to be doubled first, or escaping the wildcards would produce new unescaped ones.
    """
    for char in (LIKE_ESCAPE_CHAR, "%", "_"):
        term = term.replace(char, LIKE_ESCAPE_CHAR + char)
    return term


def search_clause(model: Any, columns: tuple[str, ...], term: str) -> ColumnElement[bool]:
    """Matches `term` case-insensitively as a substring of *any* of `columns`."""
    pattern = f"%{escape_like(term)}%"
    return or_(*(getattr(model, column).ilike(pattern, escape=LIKE_ESCAPE_CHAR) for column in columns))


async def search_multi(
    *,
    db: AsyncSession,
    model: Any,
    conditions: tuple[ColumnElement[bool], ...],
    sort_column: str,
    sort_order: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """The search-filtered counterpart to `crud.get_multi`, returning the same
    `{"data": [...], "total_count": n}` shape it does.

    Rows come back as plain dicts of every table column - matching `get_multi` called
    without a `schema_to_select`, so callers can hand them to the same public-shape
    conversion either way (and gear can still read the internal `id` it needs to batch
    its service-schedule lookup).
    """
    total_count = await db.scalar(select(func.count()).select_from(model).where(*conditions))

    order_by = getattr(model, sort_column)
    statement = (
        select(*model.__table__.columns)
        .where(*conditions)
        .order_by(order_by.desc() if sort_order == "desc" else order_by.asc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(statement)).mappings().all()

    return {"data": [dict(row) for row in rows], "total_count": total_count or 0}
