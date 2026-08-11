from crudadmin import CRUDAdmin

from ..core.config import EnvironmentOption, settings
from ..core.db.database import async_get_db
from .views import register_admin_views


def create_admin_interface() -> CRUDAdmin | None:
    """Create and configure the admin interface.

    Returns `None` - and so `main` never mounts anything at `CRUD_ADMIN_MOUNT_PATH` -
    unless `CRUD_ADMIN_ENABLED` is explicitly on. Production additionally requires a
    real `ADMIN_PASSWORD`, enforced at startup by `Settings._reject_insecure_admin_config`
    rather than here, so the failure is loud instead of a silently absent panel.
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
            "password": settings.CRUD_ADMIN_REDIS_PASSWORD if settings.CRUD_ADMIN_REDIS_PASSWORD != "None" else None,
        }

    admin = CRUDAdmin(
        session=async_get_db,
        SECRET_KEY=settings.SECRET_KEY.get_secret_value(),
        mount_path=settings.CRUD_ADMIN_MOUNT_PATH,
        session_backend=session_backend,
        redis_config=redis_config,
        allowed_ips=settings.CRUD_ADMIN_ALLOWED_IPS_LIST if settings.CRUD_ADMIN_ALLOWED_IPS_LIST else None,
        allowed_networks=settings.CRUD_ADMIN_ALLOWED_NETWORKS_LIST
        if settings.CRUD_ADMIN_ALLOWED_NETWORKS_LIST
        else None,
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
