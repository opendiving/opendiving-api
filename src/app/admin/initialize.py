import asyncio
import logging

from crudadmin import CRUDAdmin

from ..core.config import EnvironmentOption, configure_logging, settings, split_csv
from ..core.db.database import async_get_db
from .views import register_admin_views


def create_admin_interface() -> CRUDAdmin | None:
    """Create and configure the admin interface.

    Returns `None` - and so `main` never mounts anything at `CRUD_ADMIN_MOUNT_PATH` -
    unless `CRUD_ADMIN_ENABLED` is explicitly on. Production additionally requires a
    real `ADMIN_PASSWORD`, enforced at startup by `Settings._reject_insecure_admin_config`
    rather than here, so the failure is loud instead of a silently absent panel.

    Constructing the interface builds its routes but touches no database:
    `CRUDAdmin.__init__` calls `setup()`, which is synchronous route registration, while
    all the schema-creating and admin-user-seeding work lives in the separate,
    `await`-able `initialize()`. That split is the whole reason the panel can now run
    multi-worker - see `main` below.
    """
    if not settings.CRUD_ADMIN_ENABLED:
        return None

    admin_password = settings.ADMIN_PASSWORD
    session_backend = "memory"
    redis_config = None

    if settings.CRUD_ADMIN_REDIS_ENABLED:
        session_backend = "redis"
        redis_config = {
            "host": settings.CRUD_ADMIN_REDIS_HOST,
            "port": settings.CRUD_ADMIN_REDIS_PORT,
            "db": settings.CRUD_ADMIN_REDIS_DB,
            "password": settings.CRUD_ADMIN_REDIS_PASSWORD,
        }

    admin = CRUDAdmin(
        session=async_get_db,
        SECRET_KEY=settings.SECRET_KEY.get_secret_value(),
        mount_path=settings.CRUD_ADMIN_MOUNT_PATH,
        # `None` falls back to CRUDAdmin's per-container SQLite file - see
        # `CRUD_ADMIN_DB_URL` in `core.config` for why that only works single-process.
        admin_db_url=settings.CRUD_ADMIN_DB_URL,
        session_backend=session_backend,
        redis_config=redis_config,
        # CRUDAdmin reads these as "no restriction" only when they are `None`; an empty
        # list would be an allowlist matching nobody.
        allowed_ips=split_csv(settings.CRUD_ADMIN_ALLOWED_IPS) or None,
        allowed_networks=split_csv(settings.CRUD_ADMIN_ALLOWED_NETWORKS) or None,
        max_sessions_per_user=settings.CRUD_ADMIN_MAX_SESSIONS,
        session_timeout_minutes=settings.CRUD_ADMIN_SESSION_TIMEOUT,
        secure_cookies=settings.SESSION_SECURE_COOKIES,
        enforce_https=settings.ENVIRONMENT == EnvironmentOption.PRODUCTION,
        track_events=settings.CRUD_ADMIN_TRACK_EVENTS,
        track_sessions_in_db=settings.CRUD_ADMIN_TRACK_SESSIONS,
        initial_admin={"username": settings.ADMIN_USERNAME, "password": admin_password}
        if settings.ADMIN_USERNAME and admin_password
        else None,
    )

    register_admin_views(admin)

    return admin


async def main() -> None:
    """One-shot setup for the panel's own tables and its initial admin user.

    Run before the API starts:

        python -m app.admin.initialize

    Both compose files wire this up as the `admin_init` service, which `api` waits on via
    `service_completed_successfully`, so neither local development nor an install needs a
    manual step.

    It lives in this package rather than in `src/scripts/` - where it started - because
    the shipped image contains only the installed `app` package: `src/scripts/` and
    `src/app/` are both absent from it, so a `python -m src.scripts.initialize_admin`
    entrypoint could only ever run against a bind-mounted source tree. That was invisible
    while the only compose file was the development one, which mounts `./src`, and became
    a broken `admin_init` service the moment the install bundle's compose file
    (https://github.com/opendiving/opendiving/blob/main/docker-compose.yml) ran the
    published image instead.

    Why this isn't in the app's lifespan: it used to be, and the lifespan runs once *per
    worker*. Under `gunicorn -w 4` the four workers raced to create the same tables and
    insert the same initial admin row; the losers crashed and took the container with
    them. Creating a schema and seeding a row is a deployment step, not something each
    process should attempt on boot - the same reasoning that keeps
    `create_first_superuser` out of the lifespan.

    A no-op (exit 0) when `CRUD_ADMIN_ENABLED` is false, so it can sit unconditionally in
    a compose file or entrypoint without the panel having to be turned on.
    """
    configure_logging(settings.LOG_LEVEL)
    logger = logging.getLogger(__name__)

    admin = create_admin_interface()
    if admin is None:
        logger.info("CRUD_ADMIN_ENABLED is false - nothing to initialize.")
        return

    if settings.CRUD_ADMIN_DB_URL is None:
        # The default SQLite file is created relative to *this* process's working
        # directory, so in a one-shot container it lands somewhere the API container
        # cannot read. Worth saying out loud rather than leaving someone to wonder why
        # their admin login fails against an apparently-initialized panel.
        logger.warning(
            "CRUD_ADMIN_DB_URL is unset, so the panel is using a local SQLite file. "
            "That is single-process only: set it to a shared database (e.g. the app's "
            "Postgres) if the API runs more than one worker or container."
        )

    await admin.initialize()
    logger.info("Admin interface initialized (tables ready, initial admin ensured).")


if __name__ == "__main__":
    asyncio.run(main())
