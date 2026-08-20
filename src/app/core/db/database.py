from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.asyncio.session import AsyncSession
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass

from ..config import settings


class Base(DeclarativeBase, MappedAsDataclass):
    pass


DATABASE_URI = settings.POSTGRES_URI
DATABASE_PREFIX = settings.POSTGRES_ASYNC_PREFIX
DATABASE_URL = f"{DATABASE_PREFIX}{DATABASE_URI}"


async_engine = create_async_engine(DATABASE_URL, echo=False, future=True)

local_session = async_sessionmaker(bind=async_engine, class_=AsyncSession, expire_on_commit=False)


async def async_get_db() -> AsyncGenerator[AsyncSession]:
    async with local_session() as db:
        yield db


async def release_read_transaction(db: AsyncSession) -> None:
    """End the read-only transaction the caller's lookups opened, before something slow.

    `AsyncSession` autobegins on the first `execute()`, so a connection that ran a
    sub-millisecond `SELECT` stays **idle-in-transaction** for however long the caller
    spends outside the database next. The pool is `create_async_engine`'s default - five
    connections plus ten overflow - so on the order of fifteen concurrent requests park
    every connection doing nothing, and unrelated endpoints then wait out `pool_timeout`
    and fail. What makes it worth a named helper is how invisible it is: the event loop is
    free the entire time, nothing looks slow, nothing logs, and the symptom is 500s
    somewhere else entirely.

    Three call sites, reached by three different slow things - an outbound HTTP call to a
    species register, a `run_in_threadpool` parse of an uploaded export, and a threadpool
    write-plus-`fsync` of an uploaded card. Each of the three carried its own copy of this
    until the third one arrived; see *"The read transaction is released before either
    endpoint goes outbound"* in `DECISIONS.md`, which called that in advance.

    `rollback` rather than `commit` because it states what is true at every call site:
    nothing is being persisted. If a write ever grows above one of these calls, it wants
    its own commit rather than to be swept up by this.

    **The precondition is the caller's to check, and it is not "nothing has been written".**
    `rollback` expires live ORM objects regardless of `expire_on_commit=False`, which
    applies to commit only - so releasing while the caller still holds an entity turns its
    next attribute access into a silent reload. Every call site releases only where the
    preceding lookup handed back something detached: a `Row` of scalars, a frozen dataclass,
    a Pydantic model, or `None`. `services/species_service.py` has the one path that returns
    *before* releasing for exactly this reason.
    """
    await db.rollback()
