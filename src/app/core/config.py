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

    # `PATCH /user` answering "Username not available" is the same availability oracle
    # `/auth/complete` is, and a signed-in caller can walk a wordlist through it without
    # even needing a fresh onboarding token. Keyed per-user rather than per-IP because
    # it's authenticated, and applied only when a username change is actually requested -
    # the rest of the profile (name, avatar, email-preference toggle) reveals nothing and
    # shouldn't 429 a settings page.
    USERNAME_CHANGE_RATE_LIMIT_PER_USER: int = config("USERNAME_CHANGE_RATE_LIMIT_PER_USER", default=5)


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


class GeocodingSettings(BaseSettings):
    # Forward/reverse geocoding for dive sites, proxied server-side (see
    # `services.geocoding_service`). The wire format is Nominatim's, so `GEOCODER_URL` has
    # to name a Nominatim-compatible host; the keyless public instance is the default so a
    # self-hoster gets a working feature with no third-party account. Set it to an empty
    # string to turn geocoding off entirely - the endpoints then answer "no result" rather
    # than failing, exactly as they do when the provider is unreachable.
    GEOCODER_URL: str = config("GEOCODER_URL", default="https://nominatim.openstreetmap.org")
    GEOCODER_API_KEY: str | None = config("GEOCODER_API_KEY", default=None)

    # Asked for explicitly, because the alternative is not "no preference" - it is the
    # *local* script. Left unset, a reverse lookup of the Blue Hole answers "دهب, مصر",
    # which then lands in `dive_site.location` and is neither readable nor typeable for
    # most of the divers who log that site. One instance-wide value rather than the
    # caller's `Accept-Language`: it is part of the cache key, and per-caller languages
    # would multiply both the cache and the outbound calls by the number of locales.
    GEOCODER_LANGUAGE: str = config("GEOCODER_LANGUAGE", default="en")

    # Nominatim's policy requires a `User-Agent` that identifies the application, and
    # blocks generic ones. A public deployment that isn't this project's own should say so
    # here, since the address in it is where the provider's operators will complain.
    GEOCODER_USER_AGENT: str = config(
        "GEOCODER_USER_AGENT", default="OpenDiving (+https://github.com/opendiving/opendiving-api)"
    )

    # What one account may spend, and the only limit on these endpoints that can produce a
    # 429 - the provider cap below degrades instead. So this is purely an abuse bound, not
    # a pacing mechanism, and it is sized for the worst *legitimate* pattern rather than the
    # typical one: `/geocode/search` backs a type-ahead, which fires once per debounced
    # keystroke, and a diver adding sites for a week's trip can produce hundreds of calls in
    # an evening without doing anything unreasonable. It counts cache hits too, which
    # overstates the real cost - another reason to leave it loose.
    GEOCODER_RATE_LIMIT_WINDOW_SECONDS: int = config("GEOCODER_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    GEOCODER_RATE_LIMIT_PER_USER: int = config("GEOCODER_RATE_LIMIT_PER_USER", default=600)

    # What the *instance* may spend on the provider, counted across all users and applied
    # only to calls that actually leave (a cache hit costs nothing). The default is
    # Nominatim's published cap of one request per second. A self-hoster running their own
    # Nominatim has no such cap and should raise it rather than throttle themselves.
    #
    # Exceeding it is *not* a 429: the counter is global, so raising would mean one diver's
    # search rejecting another's. The call is skipped and logged, and the caller gets the
    # same "no suggestion" these endpoints already answer with when the provider is down.
    GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS: int = config("GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS", default=1)
    GEOCODER_PROVIDER_RATE_LIMIT_REQUESTS: int = config("GEOCODER_PROVIDER_RATE_LIMIT_REQUESTS", default=1)


class SpeciesSettings(BaseSettings):
    # The species catalog's two upstream sources, proxied server-side exactly as geocoding is
    # (see `services.species_service`). Neither takes a key and neither is optional in the
    # sense `GEOCODER_URL` is: emptying these does not switch the feature off, it degrades
    # search to whatever is already in the local catalog and makes resolving a *new* species
    # fail. That asymmetry is deliberate - a diver can type a location by hand, but they
    # cannot invent an AphiaID.
    #
    # WoRMS is the taxonomic authority: scientific names, synonyms, and the accepted-taxon
    # mapping that gives every catalog row its identity. Its REST webservice is free to use
    # with citation and asks only that it not be used to harvest the register wholesale,
    # which is why this app looks things up one pick at a time instead of importing.
    WORMS_API_URL: str = config("WORMS_API_URL", default="https://www.marinespecies.org/rest")
    # Wikidata is the common-name layer, keyed to WoRMS by property P850, and CC0. It is here
    # because WoRMS alone cannot answer "clownfish": *Amphiprion ocellaris* carries exactly
    # one vernacular in WoRMS, and it is in Japanese.
    WIKIDATA_API_URL: str = config("WIKIDATA_API_URL", default="https://www.wikidata.org/w/api.php")

    # Wikimedia's policy requires a descriptive `User-Agent` and blocks generic ones; WoRMS
    # asks to be told who is calling. One string satisfies both. A public deployment that
    # isn't this project's own should say so here - it is the address either provider's
    # operators will use before they block you.
    SPECIES_USER_AGENT: str = config(
        "SPECIES_USER_AGENT", default="OpenDiving (+https://github.com/opendiving/opendiving-api)"
    )

    # What one account may spend across `/species/search` and `/species/resolve` together,
    # and the only limit here that can produce a 429. Sized like the geocoder's for the same
    # reason: search backs a type-ahead, so an evening of logging a week's dives can honestly
    # produce hundreds of calls, and it counts cache hits too.
    SPECIES_RATE_LIMIT_WINDOW_SECONDS: int = config("SPECIES_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    SPECIES_RATE_LIMIT_PER_USER: int = config("SPECIES_RATE_LIMIT_PER_USER", default=600)

    # What the *instance* may spend on each provider, counted across all users and charged
    # only to calls that actually leave. Exceeding one is not a 429 - the counter is global,
    # so raising would mean one diver's search rejecting another's. That provider simply
    # contributes nothing to the search, and the other one still answers.
    #
    # Neither number is published by the provider it throttles. WoRMS states no rate limit at
    # all, and Wikimedia's applies to anonymous heavy use rather than to a call every few
    # seconds. Both are self-imposed politeness, set well above what a picker generates, and
    # a self-hoster with a relationship with either can raise them.
    SPECIES_WORMS_RATE_LIMIT_WINDOW_SECONDS: int = config("SPECIES_WORMS_RATE_LIMIT_WINDOW_SECONDS", default=60)
    SPECIES_WORMS_RATE_LIMIT_REQUESTS: int = config("SPECIES_WORMS_RATE_LIMIT_REQUESTS", default=120)
    SPECIES_WIKIDATA_RATE_LIMIT_WINDOW_SECONDS: int = config("SPECIES_WIKIDATA_RATE_LIMIT_WINDOW_SECONDS", default=60)
    SPECIES_WIKIDATA_RATE_LIMIT_REQUESTS: int = config("SPECIES_WIKIDATA_RATE_LIMIT_REQUESTS", default=300)


class ExportSettings(BaseSettings):
    # Fixed-window rate limit (see `core.utils.rate_limit`) on the three `/export/*`
    # endpoints, keyed per user and shared between them - the budget bounds total export
    # work, so the cheap CSV download draws on the same allowance as the archive.
    #
    # Authenticated and owner-only, so this is not an abuse
    # boundary the way the contact form's is - it is there because one archive request
    # reads every blob the caller owns, and nothing else in the API does that. The bound
    # is deliberately generous: this is a button a diver presses once, and someone
    # scripting a nightly backup of their own account should not hit it.
    EXPORT_RATE_LIMIT_WINDOW_SECONDS: int = config("EXPORT_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    EXPORT_RATE_LIMIT_PER_USER: int = config("EXPORT_RATE_LIMIT_PER_USER", default=10)


class ProxySettings(BaseSettings):
    # Addresses (or CIDR blocks) of reverse proxies whose `X-Forwarded-For` header may be
    # believed - see `core.utils.client_ip`. Every per-IP rate limit depends on this:
    # unset, a deployment behind nginx sees one client (the proxy) and throttles everyone
    # into a single shared bucket. Set wrongly - i.e. trusting a network that isn't
    # actually in front of you - callers can forge the header and evade the limits.
    #
    # Comma-separated, e.g. "172.16.0.0/12" for a Docker bridge network, or the address
    # of the load balancer. Leave unset when the app is reached directly.
    #
    # A plain string, split in `core.utils.client_ip`, rather than a `list[str]`: for a
    # complex field type pydantic-settings parses the environment variable itself and
    # expects JSON, so `TRUSTED_PROXY_IPS=172.16.0.0/12` fails validation at startup no
    # matter what `cast=` does here (that only produces the default). Same reason
    # `CRUD_ADMIN_ALLOWED_IPS_LIST` below is a bare annotation with no `config()` call.
    TRUSTED_PROXY_IPS: str | None = config("TRUSTED_PROXY_IPS", default=None)


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
    # `default=None`, like every other optional setting here. It used to default to the
    # *string* "None", which `admin.initialize` then had to compare against and translate
    # back - a sentinel that silently becomes a real password the moment anyone writes
    # CRUD_ADMIN_REDIS_PASSWORD="None" meaning it literally.
    CRUD_ADMIN_REDIS_PASSWORD: str | None = config("CRUD_ADMIN_REDIS_PASSWORD", default=None)
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
    GeocodingSettings,
    SpeciesSettings,
    ExportSettings,
    ProxySettings,
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
