"""Liveness and readiness probes.

Two endpoints because they answer two different questions and have two different
consequences. `/health` says the process is up and serving; `/health/ready` says it can
actually do its job, which for this app means Postgres and Redis are both answering.
A liveness probe that round-trips its datastores is a liveness probe that reports a
database outage as "the API is dead", which is how a restart loop gets started.
"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.utils import cache

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Bounds each probe rather than inheriting whatever the datastore's own connect timeout
# is. A dependency that hangs instead of refusing would otherwise hold this request - and
# the worker serving it - for as long as it takes the caller to give up, which is the one
# failure mode a readiness endpoint exists to make legible.
_PROBE_TIMEOUT_SECONDS = 3.0

# Health answers are never cacheable. Without this `ClientCacheMiddleware` labels both of
# these `public, max-age=60` - a safe method with no `Authorization` header is exactly its
# public case - so a proxy in front of the instance could serve a monitor a 200 for a
# minute after the app stopped being able to produce one. The middleware never overwrites
# a `Cache-Control` an endpoint set itself, so setting it here is the whole opt-out.
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/health")
async def health_check(response: Response) -> dict[str, str]:
    """Liveness: the process is up and routing requests. Checks nothing else, on purpose.

    This is what the image's `HEALTHCHECK` used to run and what an orchestrator should
    point a restart-on-failure probe at, so it must not fail for a reason restarting the
    container cannot fix. Use `/health/ready` to find out whether it can serve.
    """
    response.headers.update(_NO_STORE)
    return {"status": "healthy", "version": settings.APP_VERSION or "unknown", "message": "API is running"}


@router.get("/health/ready")
async def readiness_check(response: Response, db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, str]:
    """Readiness: Postgres and Redis both answered. 503 with the failing dependency named.

    For uptime monitors and for `depends_on: condition: service_healthy` - a dependent
    that waits on `api` wants "can serve", not "has a process". Every read in this API is
    Redis-backed and every write is Postgres-backed, so an instance missing either one is
    not serving, whatever its process table says.

    Both checks are cheap by design (`SELECT 1`, `PING`) because a monitor runs this every
    few seconds forever: it must cost less than the traffic it is watching for.
    """
    response.headers.update(_NO_STORE)

    problems = [problem for problem in (await _database_problem(db), await _redis_problem()) if problem]
    if problems:
        # Raw `HTTPException`, as in `api/v1/contact.py`: there is no 503 class in
        # `core/exceptions/http_exceptions.py`. The header goes on the exception because
        # raising discards the injected `response` above - the error path is the one that
        # most needs to not be cached, so it cannot rely on it.
        raise HTTPException(status_code=503, detail=f"Not ready: {', '.join(problems)}", headers=_NO_STORE)

    return {"status": "ready", "database": "ok", "redis": "ok"}


async def _database_problem(db: AsyncSession) -> str | None:
    """Describes what is wrong with Postgres, or `None` if it answered."""
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            await db.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        # Logged rather than returned: the reason can name a host, a user and a port, and
        # this endpoint answers anyone who can reach it. The operator gets the detail from
        # the logs; the monitor gets which dependency, which is all it can act on. `%r`
        # because a bare `TimeoutError` stringifies to nothing, and a log line naming no
        # cause is the one this endpoint exists to avoid.
        logger.warning("Readiness probe: database check failed: %r", exc)
        return "database unreachable"

    return None


async def _redis_problem() -> str | None:
    """Describes what is wrong with Redis, or `None` if it answered.

    `cache.client is None` means the lifespan never built the pool, which is a different
    fault from an unreachable server and worth saying so - the same distinction
    `enforce_rate_limit` draws, for the same reason: `Redis.from_pool` connects lazily,
    so a configured-but-down Redis is an ordinary-looking object that raises on first use.
    """
    if cache.client is None:
        return "redis is not configured"

    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            await cache.client.ping()
    except (RedisError, OSError, TimeoutError) as exc:
        logger.warning("Readiness probe: Redis check failed: %r", exc)
        return "redis unreachable"

    return None
