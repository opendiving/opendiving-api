import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import _AsyncGeneratorContextManager, asynccontextmanager
from typing import Any

import anyio
import fastapi
import redis.asyncio as redis
from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from sqlalchemy import text

from ..api.dependencies import get_current_superuser
from ..middleware.client_cache_middleware import ClientCacheMiddleware
from ..middleware.security_headers_middleware import SecurityHeadersMiddleware
from ..models import *  # noqa: F403
from ..services import blob_store
from ..services.blob_store import ensure_storage_ready
from .config import (
    AppSettings,
    ClientSideCacheSettings,
    DatabaseSettings,
    EnvironmentOption,
    EnvironmentSettings,
    FrontendSettings,
    RedisCacheSettings,
    configure_logging,
    settings,
)
from .db.database import async_engine as engine
from .db.migrations import upgrade_to_head
from .utils import cache

# -------------- logging --------------
configure_logging(settings.LOG_LEVEL)

# httpx logs every request it makes at INFO as `HTTP Request: GET <full url> "..."`, and
# `services.geocoding_service` sends `GEOCODER_API_KEY` as a query parameter, which is where
# Nominatim-compatible mirrors want it. So at INFO the key is written into whatever collects
# this app's logs, defeating the care that service takes to log the request *path* and never
# the built URL.
#
# Pinned after `configure_logging` and independently of `LOG_LEVEL`, so that turning the app
# up to DEBUG to chase a problem does not also start writing the key out. WARNING rather
# than off, so a genuine httpx problem is still visible.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# -------------- database --------------
# Arbitrary constant; the only thing that matters is that every process running
# `apply_migrations` picks the same one. Namespaced mentally as "opendiving schema
# bootstrap".
_SCHEMA_BOOTSTRAP_LOCK_KEY = 8231907441002137


async def apply_migrations() -> None:
    """Bring the database up to the latest Alembic revision, unless told not to.

    Serialized behind a Postgres advisory lock because this runs in the lifespan, and the
    lifespan runs once *per worker*: under `gunicorn -w 4` against a database that is
    behind, four workers start the same upgrade simultaneously. Alembic takes no
    cross-process lock of its own - two workers both read `alembic_version`, both find the
    same revision, and both run it, so the loser dies on a `CREATE TABLE` for a table the
    winner just made and gunicorn eventually gives up on the whole container. (Inherited
    from `create_all`, which had the same race for the same reason.)

    The lock is transaction-scoped, so it releases when this block commits, and it makes
    read-version-then-upgrade atomic across processes: whoever gets in second re-reads
    `alembic_version` inside the lock, finds it already at `head`, and does nothing. Held
    on this connection while Alembic works on its own - the outer transaction touches no
    table, so it cannot deadlock against the DDL. Only ever contended on a cold or
    out-of-date database; on every subsequent boot the upgrade is a no-op.
    """
    if not settings.MIGRATE_ON_START:
        logger.info("MIGRATE_ON_START is false - skipping `alembic upgrade head`.")
        return

    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_BOOTSTRAP_LOCK_KEY})
        await asyncio.to_thread(upgrade_to_head)


async def warn_if_files_volume_looks_empty() -> None:
    """Shout if the database has file rows and the store has no blobs.

    The state this catches is a restore that brought the dump back and forgot the files
    archive, a compose file that lost its `files-data` mount, or a bucket switched under a
    running instance - all of which otherwise surface one 500 at a time, days later, as a
    diver tries to open a card.

    CRITICAL and keep serving, rather than a refusal: the documented restore order is
    database first, files second, so there is a legitimate window where this is true on
    purpose. Runs after the migrations because `storage_key` has to exist to be counted.

    `blob_store.has_any_key` rather than a walk, because on the S3 backend this is a
    network request made on every boot and a full listing would be an unbounded one. It
    asks for a single key.
    """
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text("SELECT (SELECT count(*) FROM dive_file) + (SELECT count(*) FROM certification_file) AS n")
                )
            ).scalar_one()
    except Exception:
        # Never a reason to fail startup: a schema this query cannot run against is either
        # a database `apply_migrations` was told not to touch or one that is mid-restore.
        logger.debug("Could not count stored file rows for the files-volume check", exc_info=True)
        return

    if rows and not await asyncio.to_thread(blob_store.has_any_key):
        logger.critical(
            "The database has %d stored file row(s) but %s is empty - it looks unmounted, not yet "
            "restored, or not the store these rows were written to, and file downloads will fail "
            "until it is. See "
            "https://github.com/opendiving/opendiving/blob/main/docs/backup-restore.md",
            rows,
            blob_store.describe_location(),
        )


# -------------- cache --------------
async def create_redis_cache_pool() -> None:
    cache.pool = redis.ConnectionPool.from_url(settings.REDIS_CACHE_URL)
    cache.client = redis.Redis.from_pool(cache.pool)  # type: ignore


async def close_redis_cache_pool() -> None:
    if cache.client is not None:
        await cache.client.aclose()  # type: ignore


# -------------- application --------------
async def set_threadpool_tokens(number_of_tokens: int = 100) -> None:
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = number_of_tokens


def lifespan_factory(
    settings: DatabaseSettings
    | RedisCacheSettings
    | AppSettings
    | ClientSideCacheSettings
    | EnvironmentSettings
    | FrontendSettings,
    apply_migrations_on_start: bool = True,
) -> Callable[[FastAPI], _AsyncGeneratorContextManager[Any]]:
    """Factory to create a lifespan async context manager for a FastAPI app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator:
        from asyncio import Event

        initialization_complete = Event()
        app.state.initialization_complete = initialization_complete

        await set_threadpool_tokens()

        try:
            if isinstance(settings, RedisCacheSettings):
                await create_redis_cache_pool()

            # Before the migrations, not after: the revision that moved the payloads out
            # of `bytea` writes files itself, so a store that cannot be written to has to
            # fail here rather than halfway through a data move.
            await asyncio.to_thread(ensure_storage_ready)

            if apply_migrations_on_start:
                await apply_migrations()

            await warn_if_files_volume_looks_empty()

            initialization_complete.set()

            yield

        finally:
            if isinstance(settings, RedisCacheSettings):
                await close_redis_cache_pool()

    return lifespan


# -------------- application --------------
def create_application(
    router: APIRouter,
    settings: DatabaseSettings
    | RedisCacheSettings
    | AppSettings
    | ClientSideCacheSettings
    | EnvironmentSettings
    | FrontendSettings,
    apply_migrations_on_start: bool = True,
    lifespan: Callable[[FastAPI], _AsyncGeneratorContextManager[Any]] | None = None,
    **kwargs: Any,
) -> FastAPI:
    """Creates and configures a FastAPI application based on the provided settings.

    This function initializes a FastAPI application and configures it with various settings
    and handlers based on the type of the `settings` object provided.

    Parameters
    ----------
    router : APIRouter
        The APIRouter object containing the routes to be included in the FastAPI application.

    settings
        An instance representing the settings for configuring the FastAPI application.
        It determines the configuration applied:

        - AppSettings: Configures basic app metadata like name, description, contact, and license info.
        - DatabaseSettings: Adds event handlers for bringing the schema up to `head` during startup.
        - RedisCacheSettings: Sets up event handlers for creating and closing a Redis cache pool.
        - ClientSideCacheSettings: Integrates middleware for client-side caching.
        - EnvironmentSettings: Conditionally sets documentation URLs and integrates custom routes for API documentation
          based on the environment type.

        Frame protection and `nosniff` come from no setting at all - they are added to
        every application this builds, see `SecurityHeadersMiddleware`.

    apply_migrations_on_start : bool
        A flag to indicate whether to run `alembic upgrade head` on application startup.
        Defaults to True. Distinct from the `MIGRATE_ON_START` setting, which is the
        operator's switch: this one is for apps built inside the test suite, which want
        no database work at all.

    **kwargs
        Additional keyword arguments passed directly to the FastAPI constructor.

    Returns
    -------
    FastAPI
        A fully configured FastAPI application instance.

    The function configures the FastAPI application with different features and behaviors
    based on the provided settings. It includes setting up database connections, a Redis
    cache pool, client-side caching, and customizing the API documentation based on the
    environment settings.
    """
    # --- before creating application ---
    if isinstance(settings, AppSettings):
        # These reach the served document through `application.openapi()` at the `/openapi.json`
        # route below, which reads them back off the app - so this block is the only place any of
        # them is spelled. `version` is here because FastAPI otherwise supplies its own hardcoded
        # `"0.1.0"`, which does not move when a release does; `or "unknown"` is the word
        # `/api/v1/health` already uses for an `APP_VERSION` that is `None` (a source tree nobody
        # installed), and `info.version` is a required string, so it cannot simply be omitted.
        to_update: dict[str, Any] = {
            "title": settings.APP_NAME,
            "description": settings.APP_DESCRIPTION,
            "version": settings.APP_VERSION or "unknown",
        }

        # Only when there is something to publish, which is not cosmetic: all three settings are
        # unset by default - `src/.env.example` ships the contact pair commented out and no
        # `LICENSE` line at all - and the dict is passed on without being looked inside. Built
        # unconditionally, `{"name": None}` fails the document's own validation, because `name` is
        # *required* wherever a license object appears: the whole of `/openapi.json` then 500s for
        # every operator who never set `LICENSE`, which is the default install. The contact pair
        # fails softer and publishes an empty `"contact": {}`. Neither is worth shipping to get a
        # field nobody filled in.
        contact = {
            key: value for key, value in (("name", settings.CONTACT_NAME), ("email", settings.CONTACT_EMAIL)) if value
        }
        if contact:
            to_update["contact"] = contact
        if settings.LICENSE_NAME:
            to_update["license_info"] = {"name": settings.LICENSE_NAME}

        kwargs.update(to_update)

    if isinstance(settings, EnvironmentSettings):
        kwargs.update({"docs_url": None, "redoc_url": None, "openapi_url": None})

    # Use custom lifespan if provided, otherwise use default factory
    if lifespan is None:
        lifespan = lifespan_factory(settings, apply_migrations_on_start=apply_migrations_on_start)

    application = FastAPI(lifespan=lifespan, **kwargs)
    application.include_router(router)

    if isinstance(settings, FrontendSettings):
        # The web app is served from a different origin than the API (e.g.
        # localhost:3000 vs localhost:8000 in local dev) and sends both an
        # `Authorization` header and JSON bodies with `withCredentials`/cookies -
        # all of which make the browser preflight with `OPTIONS` first. Without
        # this, FastAPI has no `OPTIONS` handler for any route, so every
        # preflight (and therefore every real cross-origin request) 405s.
        application.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.FRONTEND_URL],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            # `Content-Disposition` is not one of the CORS-safelisted response
            # headers, so without this the browser strips it from every download
            # response before JS ever sees it - the file endpoints and the four
            # `/export/*` ones all send a filename the web app then can't read.
            # Exposing it makes the server's name authoritative instead of
            # something each client has to re-derive from `export/naming.py`.
            expose_headers=["Content-Disposition"],
        )

    if isinstance(settings, ClientSideCacheSettings):
        # Starlette's `_MiddlewareFactory` protocol doesn't precisely match how
        # `BaseHTTPMiddleware` subclasses are typed; this is the standard, documented
        # way to register middleware and works correctly at runtime.
        application.add_middleware(ClientCacheMiddleware, max_age=settings.CLIENT_CACHE_MAX_AGE)  # type: ignore[arg-type]

    # Registered last on purpose. `add_middleware` inserts at the front of the stack, so
    # the last one added is the *outermost* - which is what puts it outside
    # `CORSMiddleware`, the one piece of the stack that answers a request itself (an
    # `OPTIONS` preflight) instead of calling through. Registered before it, this would
    # never see those responses. Everything routed, the admin panel `main.py` mounts
    # included, is covered either way: a mount lives in the router, inside all of this.
    #
    # Unconditional, unlike the middleware above it, because there is no deployment where
    # these headers are the wrong answer and a setting would only be one more thing that
    # can be quietly off.
    application.add_middleware(SecurityHeadersMiddleware)

    if isinstance(settings, EnvironmentSettings):
        if settings.ENVIRONMENT != EnvironmentOption.PRODUCTION:
            docs_router = APIRouter()
            if settings.ENVIRONMENT != EnvironmentOption.LOCAL:
                docs_router = APIRouter(dependencies=[Depends(get_current_superuser)])

            @docs_router.get("/docs", include_in_schema=False)
            async def get_swagger_documentation() -> fastapi.responses.HTMLResponse:
                return get_swagger_ui_html(openapi_url="/openapi.json", title="docs")

            @docs_router.get("/redoc", include_in_schema=False)
            async def get_redoc_documentation() -> fastapi.responses.HTMLResponse:
                return get_redoc_html(openapi_url="/openapi.json", title="docs")

            @docs_router.get("/openapi.json", include_in_schema=False)
            async def openapi() -> dict[str, Any]:
                # `application.openapi()` rather than a `get_openapi` call of our own: this is the
                # only `/openapi.json` there is (the built-in one is off - `openapi_url=None`
                # above), and a hand-rolled call publishes exactly the fields somebody remembered
                # to list. This one passes on everything configured, and re-derives the schema when
                # the route set changes rather than on every request.
                out: dict[str, Any] = application.openapi()
                return out

            application.include_router(docs_router)

    return application
