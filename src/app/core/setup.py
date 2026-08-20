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
from fastapi.openapi.utils import get_openapi
from sqlalchemy import text

from ..api.dependencies import get_current_superuser
from ..middleware.client_cache_middleware import ClientCacheMiddleware
from ..models import *  # noqa: F403
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
from .db.database import Base
from .db.database import async_engine as engine
from .utils import cache

# -------------- logging --------------
configure_logging(settings.LOG_LEVEL)

# httpx logs every request it makes at INFO as `HTTP Request: GET <full url> "..."`, and
# `services.geocoding_service` - the app's only outbound HTTP client - sends
# `GEOCODER_API_KEY` as a query parameter, which is where Nominatim-compatible mirrors want
# it. So at INFO the key is written into whatever collects this app's logs, defeating the
# care that service takes to log the request *path* and never the built URL.
#
# Pinned after `configure_logging` and independently of `LOG_LEVEL`, so that turning the app
# up to DEBUG to chase a problem does not also start writing the key out. WARNING rather
# than off, so a genuine httpx problem is still visible.
logging.getLogger("httpx").setLevel(logging.WARNING)

# -------------- database --------------
# Arbitrary constant; the only thing that matters is that every process runs `create_tables`
# picks the same one. Namespaced mentally as "opendiving schema bootstrap".
_SCHEMA_BOOTSTRAP_LOCK_KEY = 8231907441002137


async def create_tables() -> None:
    """Create any brand-new tables. Never alters existing ones - see `DECISIONS.md`.

    Serialized behind a Postgres advisory lock because this runs in the lifespan, and the
    lifespan runs once *per worker*: under `gunicorn -w 4` against a database that doesn't
    have the tables yet, four workers call `create_all` simultaneously. `checkfirst=True`
    doesn't save you - it inspects the catalog and then issues `CREATE TABLE`, so two
    workers can both look, both see nothing, and both try. The loser dies with
    `duplicate key value violates unique constraint "pg_type_typname_nsp_index"` and
    gunicorn eventually gives up on the whole container.

    The lock is transaction-scoped, so it releases when this block commits, and it makes
    the check-then-create pair atomic across processes: whoever gets in second re-inspects
    inside the lock, finds the tables, and does nothing. Only ever contended on a cold
    database - on every subsequent boot `create_all` is a no-op and the lock is
    uncontended.
    """
    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_BOOTSTRAP_LOCK_KEY})
        await conn.run_sync(Base.metadata.create_all)


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
    create_tables_on_start: bool = True,
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

            if create_tables_on_start:
                await create_tables()

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
    create_tables_on_start: bool = True,
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
        - DatabaseSettings: Adds event handlers for initializing database tables during startup.
        - RedisCacheSettings: Sets up event handlers for creating and closing a Redis cache pool.
        - ClientSideCacheSettings: Integrates middleware for client-side caching.
        - EnvironmentSettings: Conditionally sets documentation URLs and integrates custom routes for API documentation
          based on the environment type.

    create_tables_on_start : bool
        A flag to indicate whether to create database tables on application startup.
        Defaults to True.

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
        to_update = {
            "title": settings.APP_NAME,
            "description": settings.APP_DESCRIPTION,
            "contact": {"name": settings.CONTACT_NAME, "email": settings.CONTACT_EMAIL},
            "license_info": {"name": settings.LICENSE_NAME},
        }
        kwargs.update(to_update)

    if isinstance(settings, EnvironmentSettings):
        kwargs.update({"docs_url": None, "redoc_url": None, "openapi_url": None})

    # Use custom lifespan if provided, otherwise use default factory
    if lifespan is None:
        lifespan = lifespan_factory(settings, create_tables_on_start=create_tables_on_start)

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
            # response before JS ever sees it - the file endpoints and the three
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
                out: dict = get_openapi(title=application.title, version=application.version, routes=application.routes)
                return out

            application.include_router(docs_router)

    return application
