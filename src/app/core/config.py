import os
import warnings
from enum import Enum
from typing import Self

from pydantic import SecretStr, model_validator
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
    # OpenAPI document metadata only ("who maintains this API", shown in `/docs`) -
    # *not* where the frontend's contact form delivers to. That's
    # `ContactSettings.CONTACT_FORM_EMAIL` below.
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
    # How long the token minted by `POST /dive/parse` stays usable for attaching the
    # parsed file to a dive (see `create_dive_file_token` in `core.security`). Much
    # longer than the tokens above because it is a staleness bound, not a credential
    # lifetime: a diver may import a file and then spend an evening filling in sites,
    # gear and notes before saving. It grants nothing beyond storing bytes this server
    # already parsed for that same user.
    DIVE_FILE_TOKEN_EXPIRE_MINUTES: int = config("DIVE_FILE_TOKEN_EXPIRE_MINUTES", default=1440)


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


# The password the upstream boilerplate shipped as `ADMIN_PASSWORD`'s default. It is
# published in this repo's history, so it is treated as "no password at all" rather
# than as a credential - see `Settings._reject_insecure_admin_config`.
LEGACY_DEFAULT_ADMIN_PASSWORD = "!Ch4ng3Th1sP4ssW0rd!"


class FirstUserSettings(BaseSettings):
    ADMIN_NAME: str = config("ADMIN_NAME", default="admin")
    ADMIN_EMAIL: str = config("ADMIN_EMAIL", default="admin@admin.com")
    ADMIN_USERNAME: str = config("ADMIN_USERNAME", default="admin")
    # No default: the admin panel grants full create/update/delete over every model
    # (see `admin.views`), so an unset password must mean "no admin account", never
    # "a well-known one". `admin.initialize.create_admin_interface` skips
    # `initial_admin` entirely when this is `None`.
    ADMIN_PASSWORD: str | None = config("ADMIN_PASSWORD", default=None)


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

    # The two remaining auth endpoints, limited per-IP over the same window.
    #
    # `/auth/complete` is the username-availability oracle (it answers "Username not
    # available"), so it gets a low ceiling - nobody legitimately creates ten accounts
    # from one address in a quarter of an hour.
    #
    # `/auth/refresh` is deliberately far more generous: it's called on a timer by every
    # open tab, and an office or campus behind one NAT gateway is a single IP as far as
    # this counter is concerned. The limit is here to bound replay of a stolen cookie,
    # not to pace normal use.
    AUTH_COMPLETE_RATE_LIMIT_PER_IP: int = config("AUTH_COMPLETE_RATE_LIMIT_PER_IP", default=10)
    AUTH_REFRESH_RATE_LIMIT_PER_IP: int = config("AUTH_REFRESH_RATE_LIMIT_PER_IP", default=240)

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


class ContactSettings(BaseSettings):
    # Inbox the frontend's contact form (`POST /api/v1/contact`) delivers to. A
    # self-hosted instance should point this at its own operator - the default is the
    # address for the project's own deployment, and mail sent there about someone
    # else's server is not something we can act on.
    CONTACT_FORM_EMAIL: str = config("CONTACT_FORM_EMAIL", default="contact@opendiving.app")

    # Fixed-window rate limits (see `core.utils.rate_limit`), keyed separately by the
    # submitted email and by client IP, mirroring the magic-link limits above. The
    # endpoint is unauthenticated and sends mail, so this is the only thing standing
    # between it and being used as a relay; the window is deliberately much longer
    # than the auth one, since nobody legitimately files ten support requests an hour.
    CONTACT_FORM_RATE_LIMIT_WINDOW_SECONDS: int = config("CONTACT_FORM_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    CONTACT_FORM_RATE_LIMIT_PER_EMAIL: int = config("CONTACT_FORM_RATE_LIMIT_PER_EMAIL", default=3)
    CONTACT_FORM_RATE_LIMIT_PER_IP: int = config("CONTACT_FORM_RATE_LIMIT_PER_IP", default=10)


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
    # Off by default. The panel is mounted at a fixed, guessable path and bypasses the
    # whole `api.v1` authorization story (it talks to the models directly), so it has
    # to be something an operator turns on deliberately rather than something a fresh
    # deploy inherits. `src/.env.example` enables it for local development.
    CRUD_ADMIN_ENABLED: bool = config("CRUD_ADMIN_ENABLED", default=False)
    CRUD_ADMIN_MOUNT_PATH: str = config("CRUD_ADMIN_MOUNT_PATH", default="/admin")

    # Where the panel keeps its *own* tables (admin users, sessions, event log) - not the
    # application's data, which it reads through the normal `async_get_db`.
    #
    # Unset means CRUDAdmin's default: a SQLite file under `crudadmin_data/`, local to
    # whichever container's filesystem happens to create it. That is fine for one process
    # and wrong for anything else - two workers race to create the tables and to insert
    # the initial admin, and a one-shot init container would write a file the API
    # container can never see. Point this at Postgres and all of that goes away.
    CRUD_ADMIN_DB_URL: str | None = config("CRUD_ADMIN_DB_URL", default=None)

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
    ContactSettings,
    FrontendSettings,
    GearServiceSettings,
    TestSettings,
    RedisCacheSettings,
    ClientSideCacheSettings,
    RedisQueueSettings,
    CRUDAdminSettings,
    EnvironmentSettings,
):
    @model_validator(mode="after")
    def _reject_insecure_admin_config(self) -> Self:
        """Refuses to boot a production instance whose admin panel is reachable with a
        credential that isn't one.

        `SECRET_KEY` already fails startup when unset; the admin password deserves the
        same treatment, because the panel it guards is a full CRUD interface over
        `User`, `Dive`, `GearItem` and everything else (`admin.views`). Getting this
        wrong is silent - the app starts, serves traffic, and simply happens to have an
        open door - so the check has to happen here rather than being left to a reader
        of `.env.example`.
        """
        if not self.CRUD_ADMIN_ENABLED or self.ENVIRONMENT != EnvironmentOption.PRODUCTION:
            return self

        if not self.ADMIN_PASSWORD or self.ADMIN_PASSWORD == LEGACY_DEFAULT_ADMIN_PASSWORD:
            raise ValueError(
                "CRUD_ADMIN_ENABLED is true in production but ADMIN_PASSWORD is unset or still "
                "the boilerplate default. Set a real ADMIN_PASSWORD, or set CRUD_ADMIN_ENABLED=false "
                "to not expose the admin panel at all."
            )

        if not self.CRUD_ADMIN_ALLOWED_IPS_LIST and not self.CRUD_ADMIN_ALLOWED_NETWORKS_LIST:
            # Advisory rather than fatal: plenty of deployments put the panel behind a
            # VPN or bastion instead, and hard-failing would break those.
            warnings.warn(
                "The admin panel is enabled in production with no CRUD_ADMIN_ALLOWED_IPS_LIST or "
                "CRUD_ADMIN_ALLOWED_NETWORKS_LIST. It is reachable from anywhere that can reach the API.",
                stacklevel=2,
            )

        return self


settings = Settings()
