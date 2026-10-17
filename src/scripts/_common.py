"""What the one-shot scripts in this package share."""

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from ..app.core.config import configure_logging
from ..app.core.db.database import local_session
from ..app.core.setup import close_redis_cache_pool, create_redis_cache_pool


def log_at_info() -> None:
    """Log at INFO whatever `LOG_LEVEL` says: a person runs a script to watch it work.

    Called under `__main__`, after every import: importing `core.setup`, as this module does,
    applies `LOG_LEVEL` to the root logger, and whichever of the two runs last wins.
    """
    configure_logging("INFO")


@asynccontextmanager
async def session_with_cache() -> AsyncIterator[AsyncSession]:
    """A database session with the Redis cache pool open around it.

    `delete_keys_by_pattern` silently returns when `cache.client is None`, and only the API's
    lifespan sets it - which a script does not go through. Without the pool every dive a
    backfill rewrites keeps serving its old cached response for up to an hour, silently.
    """
    await create_redis_cache_pool()
    try:
        async with local_session() as session:
            yield session
    finally:
        await close_redis_cache_pool()


def add_parser_key_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--parser-key",
        default=None,
        help="Only re-read recordings holding a file recorded under this format (e.g. `suunto_xml`). "
        "This is what `dive_file.parser_key` is for; a recording holding two files is a candidate "
        "when either of them names it.",
    )


def add_user_id_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--user-id", type=int, default=None, help="Only this account's dives, to try a run on one first."
    )
