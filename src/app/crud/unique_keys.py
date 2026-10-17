from collections.abc import Mapping
from typing import Any

from sqlalchemy import Column, ColumnElement, Table, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.visitors import replacement_traverse

from ..core.db.database import Base


def _table(model: type[Base]) -> Table:
    table = model.__table__
    assert isinstance(table, Table)
    return table


def unique_key_matches(model: type[Base], index_name: str, values: Mapping[str, Any]) -> list[ColumnElement[bool]]:
    """`WHERE` clauses matching the row that holds the key `values` would take in the named
    unique index, `values` mapping each column the index covers to the value about to be
    written.

    Each side is the index's own expression - over the stored column on one, over a bound
    value on the other - so Postgres computes both and the answer is the one the index would
    give. Normalising in Python instead (a `.strip()` the index does not do, an `IS NULL`
    where it coalesces) passes values the index then refuses, as a 500.
    """
    table = _table(model)
    (index,) = (index for index in table.indexes if index.name == index_name)

    def bind(element: Any, **_: Any) -> ColumnElement[Any] | None:
        if isinstance(element, Column) and element.table is table:
            return literal(values[element.name], element.type)
        return None

    clauses = []
    for expression in index.expressions:
        assert isinstance(expression, ColumnElement)
        clauses.append(expression == replacement_traverse(expression, {}, bind))
    return clauses


async def unique_key_taken(
    db: AsyncSession, model: type[Base], index_name: str, values: Mapping[str, Any], exclude_id: int | None = None
) -> bool:
    """Whether another row already holds the key, so a route can answer a duplicate with a
    422 before the index answers it with an `IntegrityError` - see *"Case-insensitive
    per-user uniqueness"* in DECISIONS.md."""
    table = _table(model)
    stmt = select(table.c.id).where(*unique_key_matches(model, index_name, values))
    if exclude_id is not None:
        stmt = stmt.where(table.c.id != exclude_id)
    return (await db.execute(stmt.limit(1))).first() is not None
