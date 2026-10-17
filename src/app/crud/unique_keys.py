from collections.abc import Awaitable, Mapping
from typing import Any

from sqlalchemy import Column, ColumnElement, Table, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.visitors import replacement_traverse

from ..core.db.database import Base
from ..core.exceptions.http_exceptions import DuplicateValueException


def _table(model: type[Base]) -> Table:
    table = model.__table__
    assert isinstance(table, Table)
    return table


def unique_key_expressions(model: type[Base], index_name: str) -> list[ColumnElement[Any]]:
    """The named unique index's own expressions - what a probe compares on, and what an
    `ON CONFLICT` names to be inferred onto that index and no other."""
    (index,) = (index for index in _table(model).indexes if index.name == index_name)
    expressions = []
    for expression in index.expressions:
        assert isinstance(expression, ColumnElement)
        expressions.append(expression)
    return expressions


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

    def bind(element: Any, **_: Any) -> ColumnElement[Any] | None:
        if isinstance(element, Column) and element.table is table:
            return literal(values[element.name], element.type)
        return None

    return [
        expression == replacement_traverse(expression, {}, bind)
        for expression in unique_key_expressions(model, index_name)
    ]


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


async def write_or_duplicate[T](db: AsyncSession, write: Awaitable[T], index_name: str, detail: str) -> T:
    """Run a create or rename `unique_key_taken` has cleared, and flush it, so a concurrent
    request that took the key between the probe and this write gets the probe's own 422 rather
    than a 500.

    Rolls back on any `IntegrityError` - the failed statement aborts the transaction - and
    re-raises one naming another constraint untouched, for whatever answers that one.
    """
    try:
        result = await write
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        if index_name in str(e.orig):
            raise DuplicateValueException(detail) from e
        raise
    return result
