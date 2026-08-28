import logging
import os
import warnings
from enum import Enum
from importlib import metadata
from typing import Self
from urllib.parse import quote, urlparse

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings
from starlette.config import Config

current_file_dir = os.path.dirname(os.path.realpath(__file__))
env_path = os.path.join(current_file_dir, "..", "..", ".env")
config = Config(env_path)


def split_csv(raw: str | None) -> list[str]:
    """Parse one of this app's comma-separated list settings.

    Several settings are lists of addresses or networks, and every one of them is typed
    `str | None` rather than `list[str]`: for a complex field type pydantic-settings
    parses the environment variable itself and expects JSON, so `TRUSTED_PROXY_IPS=
    172.16.0.0/12` fails validation at startup no matter what `cast=` does at the field.
    Shared so the three of them agree on what "empty" and "spaces after the comma" mean.
    """
    return [entry.strip() for entry in (raw or "").split(",") if entry.strip()]


def _installed_version() -> str | None:
    """This distribution's version, from `pyproject.toml` by way of the installed
    metadata.

    `APP_VERSION` used to be duplicated into `src/.env.example`, which meant a released
    image reported whatever version the operator's own `.env` happened to carry - the
    template's, usually, frozen at whenever they copied it. Reading it here makes
    `pyproject.toml` the single source, and the setting an override rather than the
    definition. It reaches `/api/v1/health`, the JSON export's `generator` block and the
    UDDF `<version>` element, all of which are how someone reports a bug against a
    specific build.

    `None` when the app is run from a source tree that was never installed - the same
    thing an unset `APP_VERSION` produced before, and every consumer already handles it.
    """
    try:
        return metadata.version("opendiving-api")
    except metadata.PackageNotFoundError:
        return None


class AppSettings(BaseSettings):
    APP_NAME: str = config("APP_NAME", default="FastAPI app")
    APP_DESCRIPTION: str | None = config("APP_DESCRIPTION", default=None)
    APP_VERSION: str | None = config("APP_VERSION", default=_installed_version())
    LICENSE_NAME: str | None = config("LICENSE", default=None)
    # OpenAPI document metadata only ("who maintains this API", shown in `/docs`) -
    # *not* where the frontend's contact form delivers to. That's
    # `ContactSettings.CONTACT_FORM_EMAIL` below.
    CONTACT_NAME: str | None = config("CONTACT_NAME", default=None)
    CONTACT_EMAIL: str | None = config("CONTACT_EMAIL", default=None)


# `SECRET_KEY` values that stand for "I have not set this yet": the one `src/.env.example`
# ships, and the stock stand-ins people type in its place. Every one of them is a published
# string, so it signs *anybody's* access, refresh and onboarding tokens - which is not a
# weak key, it is no key at all. Treated exactly like `LEGACY_DEFAULT_ADMIN_PASSWORD` below,
# and matched case-insensitively after stripping.
#
# An explicit list rather than an entropy heuristic on purpose: the failure this guards is
# "the template's own value reached a deployment", not "the operator chose badly", and a
# heuristic that rejects a key someone genuinely generated is a worse bug than the one it
# prevents. CI's `test-secret-key-for-testing-only` is deliberately not in here.
PLACEHOLDER_SECRET_KEYS = frozenset(
    {
        "change-me-openssl-rand-hex-32",
        "change-me",
        "changeme",
        "changethis",
        "secret",
        "your-secret-key",
        "your_secret_key",
    }
)


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

    # The refresh cookie is `Secure` everywhere it should be, which is everywhere reached
    # over HTTPS. The escape hatch exists for the one deployment shape that cannot have
    # that - an instance on a LAN address with no certificate - where a `Secure` cookie is
    # simply dropped by the browser and the symptom is "signed out on every reload" with
    # nothing in any log. Default `true`, so turning it off is a decision someone makes.
    AUTH_COOKIE_SECURE: bool = config("AUTH_COOKIE_SECURE", default=True)


class DatabaseSettings(BaseSettings):
    # The API brings the schema up to `head` on startup, so upgrading an instance is
    # `docker compose pull && docker compose up -d` and nothing else. Off is for operators
    # who want to run `alembic upgrade head` themselves - during a backup window, or from a
    # one-shot container - and it is the same escape hatch Miniflux ships as
    # `RUN_MIGRATIONS`. Turning it off does *not* fall back to anything: the app then boots
    # against whatever schema it finds, and a version behind on migrations fails at the
    # first query that needs the new column.
    MIGRATE_ON_START: bool = config("MIGRATE_ON_START", default=True)


def postgres_uri(user: str, password: str, server: str, port: int, database: str) -> str:
    """The `user:password@host:port/db` half of the DSN, with the credentials
    percent-encoded.

    They are user-chosen strings dropped into a URL, so a password containing `@`, `/`,
    `:` or `#` - all of them ordinary in a generated password, and `@` is what most
    generators reach for first - silently produces a different DSN than the operator
    wrote. `p@ss@db:5432/opendive` parses with `p` as the user and `ss` as the host, and
    the resulting error names a host nobody configured.
    """
    return f"{quote(user, safe='')}:{quote(password, safe='')}@{server}:{port}/{database}"


class PostgresSettings(DatabaseSettings):
    POSTGRES_USER: str = config("POSTGRES_USER", default="postgres")
    POSTGRES_PASSWORD: str = config("POSTGRES_PASSWORD", default="postgres")
    POSTGRES_SERVER: str = config("POSTGRES_SERVER", default="localhost")
    POSTGRES_PORT: int = config("POSTGRES_PORT", default=5432)
    POSTGRES_DB: str = config("POSTGRES_DB", default="postgres")
    POSTGRES_SYNC_PREFIX: str = config("POSTGRES_SYNC_PREFIX", default="postgresql://")
    POSTGRES_ASYNC_PREFIX: str = config("POSTGRES_ASYNC_PREFIX", default="postgresql+asyncpg://")
    POSTGRES_URI: str = postgres_uri(POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_SERVER, POSTGRES_PORT, POSTGRES_DB)


# The password the upstream boilerplate shipped as `ADMIN_PASSWORD`'s default. It is
# published in this repo's history, so it is treated as "no password at all" rather
# than as a credential - see `Settings._reject_insecure_admin_config`.
LEGACY_DEFAULT_ADMIN_PASSWORD = "!Ch4ng3Th1sP4ssW0rd!"


class FirstUserSettings(BaseSettings):
    # A display name, not an identity - nothing is keyed on it and nothing is delivered
    # to it - so unlike `ADMIN_EMAIL` below it keeps its default.
    ADMIN_NAME: str = config("ADMIN_NAME", default="admin")

    # No default, for the same reason `CONTACT_FORM_EMAIL` and `EMAIL_FROM_ADDRESS` have
    # none. It used to be `admin@admin.com`, a domain belonging to a stranger: sign-in is
    # passwordless and keyed on the email, so `scripts.create_first_superuser` would have
    # created an `is_superuser` account whose magic link is delivered to whoever runs
    # that domain. No address is right for somebody else's install, so unset means the
    # script exits without creating anything rather than guessing.
    ADMIN_EMAIL: str | None = config("ADMIN_EMAIL", default=None)

    ADMIN_USERNAME: str = config("ADMIN_USERNAME", default="admin")
    # No default: the admin panel grants full create/update/delete over every model
    # (see `admin.views`), so an unset password must mean "no admin account", never
    # "a well-known one". `admin.initialize.create_admin_interface` skips
    # `initial_admin` entirely when this is `None`.
    ADMIN_PASSWORD: str | None = config("ADMIN_PASSWORD", default=None)


class GoogleAuthSettings(BaseSettings):
    # OAuth 2.0 client ID from the Google Cloud Console, shared with the frontend
    # (`NEXT_PUBLIC_GOOGLE_CLIENT_ID`) - it's used there to build the authorization URL
    # the visitor is sent to, and here both to exchange the code that comes back and, as
    # the expected `aud` claim, to verify that the resulting ID token belongs to this app
    # rather than some other Google OAuth client. Not a secret; it rides in a URL the
    # visitor can read.
    GOOGLE_CLIENT_ID: str | None = config("GOOGLE_CLIENT_ID", default=None)

    # The other half of that client, and the one value here that genuinely is a secret:
    # it is what lets this server redeem an authorization code at Google's token
    # endpoint, and it never leaves the server - not to the browser, not to the `web`
    # container (see the `environment:` block in the install bundle's compose file,
    # https://github.com/opendiving/opendiving/blob/main/docker-compose.yml, which names
    # `GOOGLE_CLIENT_ID` and deliberately not this).
    #
    # `SecretStr` rather than the bare `str | None` that `SMTP_PASSWORD` and
    # `GEOCODER_API_KEY` beside it use: those predate this, and a value that must not
    # reach a log line is better served by a type whose `repr` cannot spill it into a
    # traceback than by everyone remembering. `Settings` is never dumped anywhere, but
    # "never" is a property to hold structurally.
    #
    # Setting `GOOGLE_CLIENT_ID` without this fails startup - see
    # `Settings._require_google_client_secret`.
    GOOGLE_CLIENT_SECRET: SecretStr | None = config("GOOGLE_CLIENT_SECRET", default=None, cast=SecretStr)


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

    # Wrong guesses allowed against the six-digit code printed beside the link, after
    # which the code is spent and only the link still works (see
    # `crud.crud_authentication_requests.register_failed_code_attempt`).
    #
    # This is the boundary, not the rate limit: 5 guesses in a space of 10^6 is roughly a
    # one-in-200,000 shot per request, and every request emails the victim, so an attack
    # worth running is one they are watching arrive. Raising it is not a knob to reach for
    # casually - each extra guess raises those odds linearly, and the reason the
    # cap can be this tight is that a diver who mistypes twice can simply read the code
    # again from the email that is already open in front of them.
    SIGN_IN_CODE_ATTEMPTS_MAX: int = config("SIGN_IN_CODE_ATTEMPTS_MAX", default=5)

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


class SMTPTLSMode(Enum):
    STARTTLS = "starttls"
    TLS = "tls"
    NONE = "none"


class EmailSettings(BaseSettings):
    # SMTP is the only transport (see `services.email_service`). Every provider speaks it -
    # Resend included, as `SMTP_HOST=smtp.resend.com` with the API key as `SMTP_PASSWORD` -
    # and so does every relay a self-hoster already has. Unset `SMTP_HOST` is the documented
    # local setup, and currently the only one in use: nothing is sent and the magic-link URL
    # is logged instead.
    SMTP_HOST: str | None = config("SMTP_HOST", default=None)
    SMTP_PORT: int = config("SMTP_PORT", default=587)
    # Independently optional, because an anonymous relay is a legitimate setup (Mailpit, an
    # internal postfix). The login is attempted only when a username is configured.
    SMTP_USERNAME: str | None = config("SMTP_USERNAME", default=None)
    SMTP_PASSWORD: str | None = config("SMTP_PASSWORD", default=None)
    # `starttls` (587) and `tls` (465) both verify certificates. `none` exists for a relay
    # on the loopback or the compose network - Mailpit, a local postfix - and nothing else.
    SMTP_TLS_MODE: SMTPTLSMode = config("SMTP_TLS_MODE", default=SMTPTLSMode.STARTTLS)

    # No default: the old one (`onboarding@resend.dev`) was only ever deliverable on
    # Resend's sandbox domain, and through an arbitrary relay it is an SPF/DKIM failure at
    # send time - hours after the misconfiguration, in someone else's spam folder. The
    # validator on `Settings` turns that into a startup error instead.
    EMAIL_FROM_ADDRESS: str | None = config("EMAIL_FROM_ADDRESS", default=None)


class ContactSettings(BaseSettings):
    # Inbox the frontend's contact form (`POST /api/v1/contact`) delivers to.
    #
    # No default, for the same reason `EMAIL_FROM_ADDRESS` has none. It used to default to
    # the address for *this project's* own deployment, so a self-hosted instance quietly
    # routed its users' support mail - password trouble, lost dives, whatever they typed -
    # to an inbox belonging to strangers who can neither see that server nor help them.
    # There is no address we can guess that is right for somebody else's install, so unset
    # means the endpoint answers 503 (`api.v1.contact`) rather than delivering somewhere.
    CONTACT_FORM_EMAIL: str | None = config("CONTACT_FORM_EMAIL", default=None)

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


class FileStorageSettings(BaseSettings):
    # Where uploaded dive-computer exports and c-card images are stored. Everything under
    # it is written and read by `services/blob_store.py` and by nothing else.
    #
    # No new required `.env` value, on purpose: both compose files mount a named volume at
    # this default, so a correct install needs nothing typed. It is a setting at all
    # because a bind-mount install wants to point it at a real directory - and because the
    # test suite and CI have to repoint it away from a path they cannot create.
    FILE_STORAGE_DIR: str = config("FILE_STORAGE_DIR", default="/data/files")


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
    # matter what `cast=` does here (that only produces the default). `split_csv` at the
    # top of this file parses all three settings that took this trade - this one and the
    # two `CRUD_ADMIN_ALLOWED_*` below.
    TRUSTED_PROXY_IPS: str | None = config("TRUSTED_PROXY_IPS", default=None)


class FrontendSettings(BaseSettings):
    # Used to build the magic-link URL emailed to the user (`{FRONTEND_URL}/auth/verify?token=...`),
    # and - through `passkey_rp_id`/`passkey_origin` below - it *is* the passkey domain.
    #
    # Changing its hostname orphans every registered passkey, because browsers scope a
    # credential to the RP ID it was created under and will not offer it to another. The
    # magic link is the recovery path when that happens. `src/.env.example` carries the same
    # warning where an operator sets this, and the operator-facing version is under "Sign-in"
    # in https://github.com/opendiving/opendiving/blob/main/docs/configuration.md
    FRONTEND_URL: str = config("FRONTEND_URL", default="http://localhost:3000")

    @property
    def passkey_rp_id(self) -> str:
        """The WebAuthn Relying Party ID: the bare hostname of `FRONTEND_URL`.

        Derived rather than configured, so there is no second place for it to be wrong -
        the same reasoning that rejected a `PASSKEYS_ENABLED` knob. The
        ceremony belongs to the *frontend* origin; this API's own host never appears in it,
        which the split-origin dev topology makes impossible to get accidentally right.

        `localhost` in dev, which browsers treat as a secure context, so the whole feature
        works locally over plain HTTP. An IP address is *not* a valid RP ID no matter what
        certificate fronts it - the browser offers the API and then throws `SecurityError`.
        """
        return urlparse(self.FRONTEND_URL).hostname or ""

    @property
    def passkey_origin(self) -> str:
        """The origin a `clientDataJSON` from that frontend will carry.

        **Rebuilt from the parse, never the raw setting.** Browsers write a bare
        `scheme://host[:port]`, so a `FRONTEND_URL` with a trailing slash - the most
        ordinary way to write a URL variable - would fail every ceremony's origin check
        with a 401 while magic links (plain concatenation) kept working, pointing nowhere
        near the cause.
        """
        parsed = urlparse(self.FRONTEND_URL)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def google_redirect_uri(self) -> str:
        """Where Google sends the visitor back after they approve the sign-in, and the
        `redirect_uri` this server will exchange an authorization code against.

        Rebuilt from the parse rather than concatenated, for the reason `passkey_origin`
        above gives: a `FRONTEND_URL` with a trailing slash is the most ordinary way to
        write a URL variable, and `{FRONTEND_URL}/auth/google/callback` would then produce
        a double slash that Google matches against nothing it has registered.

        Derived rather than configured, so there is no second place for it to be wrong -
        but the browser still *sends* the URI it used, and `POST /auth/google` refuses to
        exchange against any other. That comparison is a diagnostic rather than a control:
        Google already binds a code to the URI it saw and will not accept one it has no
        registration for, so what the check buys is that a `FRONTEND_URL` disagreeing with
        the origin the visitor actually reached fails in this app's own error, naming this
        setting, instead of as a `redirect_uri_mismatch` from Google that names neither.
        """
        parsed = urlparse(self.FRONTEND_URL)
        return f"{parsed.scheme}://{parsed.netloc}/auth/google/callback"


class PasskeySettings(BaseSettings):
    """Everything passkeys need beyond `FRONTEND_URL`, all defaulted - registering one
    adds nothing to any install's required configuration.
    """

    # How long a minted challenge stays in Redis (`auth:passkey-challenge:*`). Long enough
    # to sit on the login page with conditional UI armed, short enough to bound how long a
    # captured ceremony has to be replayed - and the challenge is `GETDEL`-consumed on the
    # first verify attempt regardless, so this is only the ceiling on an *unused* one.
    PASSKEY_CHALLENGE_TTL_SECONDS: int = config("PASSKEY_CHALLENGE_TTL_SECONDS", default=600)

    # At most this many credentials per account. Abuse hygiene rather than product policy:
    # a diver with a phone, a laptop and two security keys is nowhere near it.
    PASSKEY_MAX_CREDENTIALS_PER_USER: int = config("PASSKEY_MAX_CREDENTIALS_PER_USER", default=10)

    # Fixed-window rate limits over `MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS`, the window every
    # auth limit shares.
    #
    # The options ceiling is high on purpose and matches `AUTH_REFRESH_RATE_LIMIT_PER_IP`
    # exactly: conditional UI arms on every signed-out page view that supports it, including
    # the landing-page hero, so options are minted at page-view frequency, and an office
    # behind one NAT gateway is a single IP to this counter. `/auth/refresh` faced the same
    # situation while firing *less* often, so anything lower here contradicts that call. If
    # abuse ever shows, the lever is arming conditional UI on first focus of the email input
    # rather than on mount - not a lower ceiling.
    #
    # None of these is the security boundary. The single-use challenge and the signature
    # are; rate limiting fails open (see `core.utils.rate_limit`).
    PASSKEY_OPTIONS_RATE_LIMIT_PER_IP: int = config("PASSKEY_OPTIONS_RATE_LIMIT_PER_IP", default=240)
    PASSKEY_VERIFY_RATE_LIMIT_PER_IP: int = config("PASSKEY_VERIFY_RATE_LIMIT_PER_IP", default=30)
    PASSKEY_REGISTER_RATE_LIMIT_PER_USER: int = config("PASSKEY_REGISTER_RATE_LIMIT_PER_USER", default=10)


class GearServiceSettings(BaseSettings):
    # Hour of the day (UTC) the gear-service digest cron runs - see
    # `core.worker.functions.send_gear_service_digests`. Configurable mainly so local
    # development can park it somewhere harmless; 07:00 UTC lands mid-morning across
    # Europe, which is close enough given everything here is date-granular.
    GEAR_SERVICE_DIGEST_HOUR: int = config("GEAR_SERVICE_DIGEST_HOUR", default=7)


class AccountDeletionSettings(BaseSettings):
    # How long a deletion request sits reversible before `purge_deleted_accounts`
    # destroys the account (see `core.worker.functions`). The account is dark from the
    # moment the button is pressed either way - this is only how long the way back in
    # stays open, not a period the app keeps working.
    #
    # `0` means the next hourly sweep purges it, which is the setting an operator who
    # wants no grace period at all would reach for. It changes the confirmation email's
    # copy rather than being a special case in the job: at zero there is nothing to tell
    # the user to sign in before.
    #
    # 14 rather than 30, and the reason is a published sentence: the bundled privacy page
    # promises erasure "within 30 days", and a 30-day window swept hourly lands at "30
    # days and change". Raising this past roughly 29 makes that sentence false for the
    # instance - https://github.com/opendiving/opendiving/blob/main/docs/configuration.md
    # says so.
    ACCOUNT_DELETION_GRACE_DAYS: int = config("ACCOUNT_DELETION_GRACE_DAYS", default=14)

    # Fixed-window per-user limit over `MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS`, because
    # `DELETE /user` sends mail. Deliberately not the thing that stops a double submit -
    # a two-request race beats any counter, which is why the endpoint's real guard is a
    # database predicate (`WHERE is_deleted = false`) and this is only here to stop one
    # account being used to pump the relay.
    ACCOUNT_DELETION_RATE_LIMIT_PER_USER: int = config("ACCOUNT_DELETION_RATE_LIMIT_PER_USER", default=5)


class TestSettings(BaseSettings): ...


# One password for both Redis pools. The bundled `redis` container needs none and the
# default is unset, but a managed Redis - or any server with `requirepass` - always wants
# one, and the cache and the arq queue are the same server in every configuration this app
# ships. Two variables would be two ways to get one thing half-right.
_REDIS_PASSWORD: str | None = config("REDIS_PASSWORD", default=None)


def redis_url(host: str, port: int, password: str | None) -> str:
    """A `redis://` DSN, percent-encoding the password for the same reason
    `postgres_uri` encodes its credentials.
    """
    credentials = f":{quote(password, safe='')}@" if password else ""
    return f"redis://{credentials}{host}:{port}"


class RedisSettings(BaseSettings):
    REDIS_PASSWORD: str | None = _REDIS_PASSWORD


class RedisCacheSettings(RedisSettings):
    REDIS_CACHE_HOST: str = config("REDIS_CACHE_HOST", default="localhost")
    REDIS_CACHE_PORT: int = config("REDIS_CACHE_PORT", default=6379)
    REDIS_CACHE_URL: str = redis_url(REDIS_CACHE_HOST, REDIS_CACHE_PORT, _REDIS_PASSWORD)


class ClientSideCacheSettings(BaseSettings):
    CLIENT_CACHE_MAX_AGE: int = config("CLIENT_CACHE_MAX_AGE", default=60)


class RedisQueueSettings(RedisSettings):
    REDIS_QUEUE_HOST: str = config("REDIS_QUEUE_HOST", default="localhost")
    REDIS_QUEUE_PORT: int = config("REDIS_QUEUE_PORT", default=6379)


class CRUDAdminSettings(BaseSettings):
    # Off by default. The panel is mounted at a fixed, guessable path and bypasses the
    # whole `api.v1` authorization story (it talks to the models directly), so it has
    # to be something an operator turns on deliberately rather than something a fresh
    # deploy inherits. `src/.env.example` ships the whole block commented out, local
    # development included: turning the panel on is an edit, never an inheritance.
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

    # Who may reach the panel at all, comma-separated and parsed by `split_csv`, e.g.
    # CRUD_ADMIN_ALLOWED_IPS="203.0.113.7,203.0.113.8" or
    # CRUD_ADMIN_ALLOWED_NETWORKS="10.0.0.0/8". Unset means "from anywhere the API is
    # reachable", which is what `_reject_insecure_admin_config` warns about in production.
    #
    # These were `list[str] | None` bare annotations with no `config()` call, for the
    # pydantic-settings reason `split_csv` documents - which made them settable only as
    # JSON, and only through an *exported* environment variable rather than the `src/.env`
    # file every other setting here comes from and the docs teach. Comma-strings make them
    # configurable the same way as `TRUSTED_PROXY_IPS`, which had already made this trade.
    CRUD_ADMIN_ALLOWED_IPS: str | None = config("CRUD_ADMIN_ALLOWED_IPS", default=None)
    CRUD_ADMIN_ALLOWED_NETWORKS: str | None = config("CRUD_ADMIN_ALLOWED_NETWORKS", default=None)
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


LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class LoggingSettings(BaseSettings):
    # Any level name the stdlib knows (DEBUG, INFO, WARNING, ERROR, CRITICAL), case
    # insensitive. Applied by `configure_logging` below; validated at startup rather than
    # left for `logging.basicConfig` to raise on, so a typo names itself.
    LOG_LEVEL: str = config("LOG_LEVEL", default="INFO")


def configure_logging(level: str) -> None:
    """Point the root logger at stderr at `level`.

    Called by both entrypoints - `core.setup` for the API, `core.worker.functions` for the
    worker - because there is no third place both of them already import. `core.logger`
    used to be that place, and was imported by nothing at all: it configured a rotating
    file handler that never existed in any running process, while `core.setup` had to pin
    the httpx logger by hand with a comment explaining that a hazard guarded only in dead
    configuration is not guarded. This module owns `LOG_LEVEL`, so it owns applying it.

    stderr rather than stdout because that is `basicConfig`'s default stream and there is
    no reason to fight it: both are captured identically by `docker compose logs` and by
    every collector, and `uvicorn` and `arq` already log there.

    The level is set on the root logger explicitly as well as passed to `basicConfig`,
    which does nothing at all when the root logger already has a handler - under a server
    that installed its own, `LOG_LEVEL` would otherwise be exactly the dead configuration
    described above.
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    logging.getLogger().setLevel(level)


class Settings(
    AppSettings,
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
    FileStorageSettings,
    ProxySettings,
    FrontendSettings,
    PasskeySettings,
    GearServiceSettings,
    AccountDeletionSettings,
    TestSettings,
    RedisCacheSettings,
    ClientSideCacheSettings,
    RedisQueueSettings,
    CRUDAdminSettings,
    EnvironmentSettings,
    LoggingSettings,
):
    @model_validator(mode="after")
    def _reject_placeholder_secret_key(self) -> Self:
        """Refuses to boot on a `SECRET_KEY` that is published somewhere.

        `SECRET_KEY` signs every access, refresh and onboarding token this app issues, so
        a value anyone can read is a key anyone can mint tokens with - full impersonation
        of any account, including a superuser's. The canonical setup is
        `cp src/.env.example src/.env`, and that template has to carry *something* in the
        slot, so the value it carries must not be one an instance can run with.

        Every environment, with no `ENVIRONMENT` gate: a staging instance signing tokens
        with a published key is compromised in exactly the way a production one is, and
        the local instance is the one whose `.env` came straight from the template.
        """
        secret = self.SECRET_KEY.get_secret_value().strip()

        if not secret or secret.lower() in PLACEHOLDER_SECRET_KEYS:
            raise ValueError(
                "SECRET_KEY is unset or still a placeholder from src/.env.example. It signs every "
                "token this app issues, so a published value lets anyone mint one for any account. "
                "Generate your own: openssl rand -hex 32"
            )
        return self

    @model_validator(mode="after")
    def _require_smtp_outside_local(self) -> Self:
        """Refuses to boot a deployed instance that cannot send mail.

        Sign-in is passwordless: without a relay there is no way to deliver a magic link,
        so *nobody* can get in - not even the first user, on a freshly installed instance.
        The failure is otherwise invisible until someone tries, and then it is a 500 from
        `POST /auth/email/request` (`services.email_service` refuses to log a live token
        off `local`, deliberately) with a message about email transport on a page about
        signing in.

        Every environment except `local`. `local` is the one where the logged-link
        fallback *is* the documented sign-in flow - the developer reads the link out of
        `docker compose logs api` - and anything else is a deployment someone other than
        the developer can reach. This used to be scoped to `production` alone, on the
        argument that staging is close enough to local to be run the same way on purpose;
        that holds only while staging is a second laptop. A staging box more than one
        person can reach is a real deployment, and the sign-in links in its logs are real
        credentials.
        """
        if self.ENVIRONMENT != EnvironmentOption.LOCAL and not self.SMTP_HOST:
            raise ValueError(
                f"ENVIRONMENT is {self.ENVIRONMENT.value} but SMTP_HOST is not set. Sign-in is "
                "passwordless, so without a mail relay nobody can sign in at all - including the "
                "first user. Point SMTP_* at any relay you trust, or run this instance as "
                "ENVIRONMENT=local."
            )
        return self

    @model_validator(mode="after")
    def _require_google_client_secret(self) -> Self:
        """Refuses to boot an instance that offers Google sign-in but cannot complete one.

        The browser never receives a token from Google any more: it comes back with an
        authorization code, and this server redeems that code at Google's token endpoint -
        which takes both halves of the OAuth client. So a `GOOGLE_CLIENT_ID` with no secret
        is an instance that renders the button and then 401s every click.

        Refusing rather than warning and treating Google as unconfigured, because the
        warning cannot be made to work end to end. The web app decides whether to render
        the button from **its own** `GOOGLE_CLIENT_ID` and has no way to learn that the API
        lacks a secret, so degrading here would leave a visible button that always fails.
        Failing startup puts the error where the mistake was made, which is the same
        argument `_require_from_address_with_smtp` above makes.

        What this guarantees is exactly one thing: *an API that is running has both halves
        of its Google credentials.* It does not promise that sign-in works - a `FRONTEND_URL`
        disagreeing with the origin the visitor reached still breaks it (`POST /auth/google`
        says so, naming the setting), and a redirect URI not registered in the Google Cloud
        Console still breaks it at Google. Three failures, three distinct messages, none of
        them silent.

        Only this direction. A secret with no client id is an instance with Google sign-in
        switched off and one stray variable, which is harmless and not worth a startup
        failure.

        This lives on the combined class rather than on `GoogleAuthSettings`, where both
        fields are declared, because a cross-field validator has to see the whole settings
        object - the same reason the four validators around it are here.
        """
        if not self.GOOGLE_CLIENT_ID:
            return self

        secret = self.GOOGLE_CLIENT_SECRET.get_secret_value().strip() if self.GOOGLE_CLIENT_SECRET else ""
        if not secret:
            raise ValueError(
                "GOOGLE_CLIENT_ID is set but GOOGLE_CLIENT_SECRET is not. Signing in with Google "
                "now redeems an authorization code at Google's token endpoint, which needs both "
                "halves of the OAuth client. Copy the client secret from the same Google Cloud "
                "Console credential the id came from, or unset GOOGLE_CLIENT_ID to turn Google "
                "sign-in off."
            )
        return self

    @model_validator(mode="after")
    def _normalize_log_level(self) -> Self:
        """Rejects a `LOG_LEVEL` the stdlib doesn't know, rather than letting
        `configure_logging` raise from inside application startup where the traceback
        points at logging rather than at the typo.
        """
        level = self.LOG_LEVEL.strip().upper()

        if level not in logging.getLevelNamesMapping():
            known = ", ".join(sorted(logging.getLevelNamesMapping()))
            raise ValueError(f"LOG_LEVEL={self.LOG_LEVEL!r} is not a logging level. Use one of: {known}.")

        self.LOG_LEVEL = level
        return self

    @model_validator(mode="after")
    def _require_from_address_with_smtp(self) -> Self:
        """Refuses to boot an instance that has a relay configured but no address to send
        as.

        `EMAIL_FROM_ADDRESS` used to default to `onboarding@resend.dev`, which was
        deliverable on exactly one provider's sandbox domain. Through an arbitrary relay
        that default is not a default at all - it is an SPF/DKIM failure that surfaces
        hours later, in a recipient's spam folder, with nothing in this app's logs
        pointing at the cause. Failing startup puts the error where the mistake was made.

        This is why `src/.env.example` ships the from-address *commented out*: the
        canonical setup is copying that file, so a template with a plausible-looking
        address active in it would satisfy any check made here and reintroduce exactly
        the failure this exists to prevent.
        """
        if self.SMTP_HOST and not self.EMAIL_FROM_ADDRESS:
            raise ValueError(
                "SMTP_HOST is set but EMAIL_FROM_ADDRESS is not. Set it to an address on a "
                "domain your relay is allowed to send for - there is no safe default, and a "
                "wrong one fails at delivery time rather than here."
            )
        return self

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

        if not split_csv(self.CRUD_ADMIN_ALLOWED_IPS) and not split_csv(self.CRUD_ADMIN_ALLOWED_NETWORKS):
            # Advisory rather than fatal: plenty of deployments put the panel behind a
            # VPN or bastion instead, and hard-failing would break those.
            warnings.warn(
                "The admin panel is enabled in production with no CRUD_ADMIN_ALLOWED_IPS or "
                "CRUD_ADMIN_ALLOWED_NETWORKS. It is reachable from anywhere that can reach the API.",
                stacklevel=2,
            )

        return self


settings = Settings()
