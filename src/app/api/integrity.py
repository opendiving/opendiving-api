from collections.abc import Awaitable, Callable, Mapping
from typing import NoReturn

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions.http_exceptions import UnprocessableEntityException


async def write_or_422[T](db: AsyncSession, write: Awaitable[T], detail: str | Callable[[IntegrityError], str]) -> T:
    """Run one uncommitted write of a request and flush it, so a constraint it breaks is a 422
    naming that write rather than surfacing in the next one's statement.

    A failure rolls back the whole request - the parent row and every child written before
    it - so a route that commits once after its last write and invalidates after that commit
    leaves nothing behind for a cache to serve.
    """
    try:
        result = await write
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise UnprocessableEntityException(detail if isinstance(detail, str) else detail(e)) from e
    return result


async def refuse_vanished_reference(db: AsyncSession, exc: IntegrityError, messages: Mapping[str, str]) -> NoReturn:
    """Turn a foreign key lost between its resolve and the write - a concurrent delete - into
    the 422 a foreign or missing uuid gets, `messages` mapping each constraint to its sentence.

    Rolls back first: the failed statement leaves the session in an aborted transaction that
    refuses every later command. Re-raises any other constraint untouched - one this does not
    recognise is a bug somewhere else, and dressing it as "not found" would send the caller
    after the wrong thing.
    """
    await db.rollback()
    for constraint, detail in messages.items():
        if constraint in str(exc.orig):
            raise UnprocessableEntityException(detail) from exc
    raise exc
