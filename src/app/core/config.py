import os
from enum import Enum

from pydantic import SecretStr
from pydantic_settings import BaseSettings
from starlette.config import Config

current_file_dir = os.path.dirname(os.path.realpath(__file__))
env_path = os.path.join(current_file_dir, "..", "..", ".env")
config = Config(env_path)


class AppSettings(BaseSettings):
    APP_NAME: str = config("APP_NAME", default="FastAPI app")
    APP_DESCRIPTION: str | None = config("APP_DESCRIPTION", default=None)
    APP_VERSION: str | None = config("APP_VERSION", default=None)
    LICENSE_NAME: str | None = config("LICENSE", default=None)
    CONTACT_NAME: str | None = config("CONTACT_NAME", default=None)
    CONTACT_EMAIL: str | None = config("CONTACT_EMAIL", default=None)


class CryptSettings(BaseSettings):
    SECRET_KEY: SecretStr = config("SECRET_KEY", cast=SecretStr)
    ALGORITHM: str = config("ALGORITHM", default="HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = config("ACCESS_TOKEN_EXPIRE_MINUTES", default=30)
    REFRESH_TOKEN_EXPIRE_DAYS: int = config("REFRESH_TOKEN_EXPIRE_DAYS", default=7)
    # How long a temporary, post-verification-but-pre-account "onboarding" JWT (see
    # `create_onboarding_token`/`verify_onboarding_token` in `core.security`) stays
    # valid for. Deliberately short - it exists only to carry a verified identity from
    # `/auth/email/verify` or `/auth/google` to `/auth/complete`.
    ONBOARDING_TOKEN_EXPIRE_MINUTES: int = config("ONBOARDING_TOKEN_EXPIRE_MINUTES", default=30)


class DatabaseSettings(BaseSettings):
    pass


class SQLiteSettings(DatabaseSettings):
    SQLITE_URI: str = config("SQLITE_URI", default="./sql_app.db")
    SQLITE_SYNC_PREFIX: str = config("SQLITE_SYNC_PREFIX", default="sqlite:///")
    SQLITE_ASYNC_PREFIX: str = config("SQLITE_ASYNC_PREFIX", default="sqlite+aiosqlite:///")


class MySQLSettings(DatabaseSettings):
    MYSQL_USER: str = config("MYSQL_USER", default="username")
    MYSQL_PASSWORD: str = config("MYSQL_PASSWORD", default="password")
    MYSQL_SERVER: str = config("MYSQL_SERVER", default="localhost")
    MYSQL_PORT: int = config("MYSQL_PORT", default=5432)
    MYSQL_DB: str = config("MYSQL_DB", default="dbname")
    MYSQL_URI: str = f"{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_SERVER}:{MYSQL_PORT}/{MYSQL_DB}"
    MYSQL_SYNC_PREFIX: str = config("MYSQL_SYNC_PREFIX", default="mysql://")
    MYSQL_ASYNC_PREFIX: str = config("MYSQL_ASYNC_PREFIX", default="mysql+aiomysql://")
    MYSQL_URL: str | None = config("MYSQL_URL", default=None)


class PostgresSettings(DatabaseSettings):
    POSTGRES_USER: str = config("POSTGRES_USER", default="postgres")
    POSTGRES_PASSWORD: str = config("POSTGRES_PASSWORD", default="postgres")
    POSTGRES_SERVER: str = config("POSTGRES_SERVER", default="localhost")
    POSTGRES_PORT: int = config("POSTGRES_PORT", default=5432)
    POSTGRES_DB: str = config("POSTGRES_DB", default="postgres")
    POSTGRES_SYNC_PREFIX: str = config("POSTGRES_SYNC_PREFIX", default="postgresql://")
    POSTGRES_ASYNC_PREFIX: str = config("POSTGRES_ASYNC_PREFIX", default="postgresql+asyncpg://")
    POSTGRES_URI: str = f"{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_SERVER}:{POSTGRES_PORT}/{POSTGRES_DB}"
    POSTGRES_URL: str | None = config("POSTGRES_URL", default=None)


class FirstUserSettings(BaseSettings):
    ADMIN_NAME: str = config("ADMIN_NAME", default="admin")
    ADMIN_EMAIL: str = config("ADMIN_EMAIL", default="admin@admin.com")
    ADMIN_USERNAME: str = config("ADMIN_USERNAME", default="admin")
    ADMIN_PASSWORD: str = config("ADMIN_PASSWORD", default="!Ch4ng3Th1sP4ssW0rd!")


class GoogleAuthSettings(BaseSettings):
    # OAuth 2.0 client ID from the Google Cloud Console, shared with the frontend
    # (`NEXT_PUBLIC_GOOGLE_CLIENT_ID`) - it's used there to request an ID token and
    # here, as the expected `aud` claim, to verify that token actually belongs to
    # this app rather than some other Google OAuth client. Not a secret - safe to
    # ship to the browser - so there's no accompanying `GOOGLE_CLIENT_SECRET`.
    GOOGLE_CLIENT_ID: str | None = config("GOOGLE_CLIENT_ID", default=None)


class MagicLinkSettings(BaseSettings):
    MAGIC_LINK_TOKEN_EXPIRE_MINUTES: int = config("MAGIC_LINK_TOKEN_EXPIRE_MINUTES", default=30)

    # Fixed-window rate limits (see `core.utils.rate_limit`), keyed separately by email
    # and by client IP - the former stops one address from being spammed, the latter
    # stops one caller from spamming many addresses (and, being far higher, is not a
    # meaningful enumeration side-channel).
    MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS: int = config("MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS", default=900)
    MAGIC_LINK_REQUEST_RATE_LIMIT_PER_EMAIL: int = config("MAGIC_LINK_REQUEST_RATE_LIMIT_PER_EMAIL", default=3)
    MAGIC_LINK_REQUEST_RATE_LIMIT_PER_IP: int = config("MAGIC_LINK_REQUEST_RATE_LIMIT_PER_IP", default=15)
    MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP: int = config("MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP", default=30)

    # Email-change confirmation (see `POST /user/email-change/request`/
    # `POST /user/email-change/verify` in `api.v1.users`) reuses the same
    # `AuthenticationRequest` mechanics as sign-in, with its own expiry/rate limit
    # (keyed per-user, not per-email - it's an authenticated action).
    EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES: int = config("EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES", default=30)
    EMAIL_CHANGE_REQUEST_RATE_LIMIT_PER_USER: int = config("EMAIL_CHANGE_REQUEST_RATE_LIMIT_PER_USER", default=3)


class EmailSettings(BaseSettings):
    # https://resend.com - used to deliver the magic-link email (see `services.email_service`).
    RESEND_API_KEY: str | None = config("RESEND_API_KEY", default=None)
    EMAIL_FROM_ADDRESS: str = config("EMAIL_FROM_ADDRESS", default="onboarding@resend.dev")


class FrontendSettings(BaseSettings):
    # Used to build the magic-link URL emailed to the user (`{FRONTEND_URL}/auth/verify?token=...`).
    FRONTEND_URL: str = config("FRONTEND_URL", default="http://localhost:3000")


class GearServiceSettings(BaseSettings):
    # Hour of the day (UTC) the gear-service digest cron runs - see
    # `core.worker.functions.send_gear_service_digests`. Configurable mainly so local
    # development can park it somewhere harmless; 07:00 UTC lands mid-morning across
    # Europe, which is close enough given everything here is date-granular.
    GEAR_SERVICE_DIGEST_HOUR: int = config("GEAR_SERVICE_DIGEST_HOUR", default=7)


class TestSettings(BaseSettings): ...


class RedisCacheSettings(BaseSettings):
    REDIS_CACHE_HOST: str = config("REDIS_CACHE_HOST", default="localhost")
    REDIS_CACHE_PORT: int = config("REDIS_CACHE_PORT", default=6379)
    REDIS_CACHE_URL: str = f"redis://{REDIS_CACHE_HOST}:{REDIS_CACHE_PORT}"


class ClientSideCacheSettings(BaseSettings):
    CLIENT_CACHE_MAX_AGE: int = config("CLIENT_CACHE_MAX_AGE", default=60)


class RedisQueueSettings(BaseSettings):
    REDIS_QUEUE_HOST: str = config("REDIS_QUEUE_HOST", default="localhost")
    REDIS_QUEUE_PORT: int = config("REDIS_QUEUE_PORT", default=6379)


class CRUDAdminSettings(BaseSettings):
    CRUD_ADMIN_ENABLED: bool = config("CRUD_ADMIN_ENABLED", default=True)
    CRUD_ADMIN_MOUNT_PATH: str = config("CRUD_ADMIN_MOUNT_PATH", default="/admin")

    CRUD_ADMIN_ALLOWED_IPS_LIST: list[str] | None = None
    CRUD_ADMIN_ALLOWED_NETWORKS_LIST: list[str] | None = None
    CRUD_ADMIN_MAX_SESSIONS: int = config("CRUD_ADMIN_MAX_SESSIONS", default=10)
    CRUD_ADMIN_SESSION_TIMEOUT: int = config("CRUD_ADMIN_SESSION_TIMEOUT", default=1440)
    SESSION_SECURE_COOKIES: bool = config("SESSION_SECURE_COOKIES", default=True)

    CRUD_ADMIN_TRACK_EVENTS: bool = config("CRUD_ADMIN_TRACK_EVENTS", default=True)
    CRUD_ADMIN_TRACK_SESSIONS: bool = config("CRUD_ADMIN_TRACK_SESSIONS", default=True)

    CRUD_ADMIN_REDIS_ENABLED: bool = config("CRUD_ADMIN_REDIS_ENABLED", default=False)
    CRUD_ADMIN_REDIS_HOST: str = config("CRUD_ADMIN_REDIS_HOST", default="localhost")
    CRUD_ADMIN_REDIS_PORT: int = config("CRUD_ADMIN_REDIS_PORT", default=6379)
    CRUD_ADMIN_REDIS_DB: int = config("CRUD_ADMIN_REDIS_DB", default=0)
    CRUD_ADMIN_REDIS_PASSWORD: str | None = config("CRUD_ADMIN_REDIS_PASSWORD", default="None")
    CRUD_ADMIN_REDIS_SSL: bool = config("CRUD_ADMIN_REDIS_SSL", default=False)


class EnvironmentOption(Enum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class EnvironmentSettings(BaseSettings):
    ENVIRONMENT: EnvironmentOption = config("ENVIRONMENT", default=EnvironmentOption.LOCAL)


class Settings(
    AppSettings,
    SQLiteSettings,
    PostgresSettings,
    CryptSettings,
    FirstUserSettings,
    GoogleAuthSettings,
    MagicLinkSettings,
    EmailSettings,
    FrontendSettings,
    GearServiceSettings,
    TestSettings,
    RedisCacheSettings,
    ClientSideCacheSettings,
    RedisQueueSettings,
    CRUDAdminSettings,
    EnvironmentSettings,
):
    pass


settings = Settings()
