from crudadmin import CRUDAdmin

from ..core.config import EnvironmentOption, settings, split_csv
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
    multi-worker - see `scripts.initialize_admin`.
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
