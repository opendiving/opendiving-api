"""Unit tests for the startup guards and DSN building in `core.config`.

The admin-panel guard has its own module (`test_admin_config.py`) and the from-address one
`test_email_config.py`; this covers the rest of what a fresh install can get wrong before
it has served a single request - a `SECRET_KEY` copied out of the template, a production
instance nobody can sign in to, an admin address belonging to a stranger, a password with
an `@` in it, and a `LOG_LEVEL` typo.
"""

import importlib.util
import logging
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import pytest
import starlette.config

from src.app.core.config import (
    PLACEHOLDER_SECRET_KEYS,
    EnvironmentOption,
    Settings,
    _installed_version,
    postgres_uri,
    redis_url,
    split_csv,
)


def _settings(**overrides):
    """Build a `Settings` without reading the developer's own `src/.env`."""
    base = {
        "SECRET_KEY": "test-secret-key-for-testing-only",
        "ENVIRONMENT": EnvironmentOption.LOCAL,
        "CRUD_ADMIN_ENABLED": False,
        "SMTP_HOST": None,
        "EMAIL_FROM_ADDRESS": None,
    }
    return Settings(**{**base, **overrides})


def _config_loaded_without_an_env_file(tmp_path, monkeypatch):
    """A second, independent copy of `core.config` whose `Config` reads a file that isn't
    there, so every setting falls back to the default declared in the source.

    Needed because `config()` resolves its default at import time against the developer's
    own `src/.env`: reading a value off the already-imported module reports what that file
    happens to say, not what the code declares. `config.py` imports nothing from this
    package, so executing it under another name leaves `sys.modules` - and every module
    already holding the real `settings` - untouched.
    """
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-testing-only")
    monkeypatch.setenv("ENVIRONMENT", "local")

    original = starlette.config.Config
    monkeypatch.setattr(starlette.config, "Config", lambda *_args, **_kwargs: original(tmp_path / "absent.env"))

    source = Path(__file__).resolve().parents[1] / "src" / "app" / "core" / "config.py"
    spec = importlib.util.spec_from_file_location("_config_without_an_env_file", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


class TestPlaceholderSecretKeysAreRefused:
    """`SECRET_KEY` signs every access, refresh and onboarding token, so a value published
    in this repository is not a weak key - it is a key anyone can mint tokens with.
    """

    @pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_SECRET_KEYS))
    def test_each_known_placeholder_fails_startup(self, placeholder: str):
        with pytest.raises(ValueError, match="SECRET_KEY"):
            _settings(SECRET_KEY=placeholder)

    def test_the_value_the_template_ships_is_one_of_them(self):
        """The whole point: `cp src/.env.example src/.env` must not produce a bootable
        instance. Read out of the template rather than restated, so editing one without
        the other fails here.
        """
        from pathlib import Path

        template = (Path(__file__).resolve().parents[1] / "src" / ".env.example").read_text()
        shipped = next(line for line in template.splitlines() if line.startswith("SECRET_KEY="))

        assert shipped.split("=", 1)[1].strip('"') in PLACEHOLDER_SECRET_KEYS

    @pytest.mark.parametrize("value", ["CHANGEME", "  change-me  ", "ChangeThis"])
    def test_casing_and_padding_do_not_get_past_it(self, value: str):
        with pytest.raises(ValueError, match="SECRET_KEY"):
            _settings(SECRET_KEY=value)

    def test_an_empty_key_fails_startup(self):
        with pytest.raises(ValueError, match="SECRET_KEY"):
            _settings(SECRET_KEY="   ")

    @pytest.mark.parametrize(
        "environment", [EnvironmentOption.LOCAL, EnvironmentOption.STAGING, EnvironmentOption.PRODUCTION]
    )
    def test_every_environment_is_guarded(self, environment):
        """No `ENVIRONMENT` gate, deliberately: the local instance is the one whose `.env`
        came straight out of the template, and a staging instance signing tokens with a
        published key is compromised exactly as a production one is.
        """
        with pytest.raises(ValueError, match="SECRET_KEY"):
            _settings(
                SECRET_KEY="change-me-openssl-rand-hex-32",
                ENVIRONMENT=environment,
                SMTP_HOST="smtp.example.com",
                EMAIL_FROM_ADDRESS="noreply@opendiving.example",
            )

    def test_a_generated_key_is_accepted(self):
        settings = _settings(SECRET_KEY="0f9c2b7a4e1d8c3f6a5b0e2d9c4f7a1b")

        assert settings.SECRET_KEY.get_secret_value() == "0f9c2b7a4e1d8c3f6a5b0e2d9c4f7a1b"

    def test_the_throwaway_key_ci_uses_is_accepted(self):
        """The guard is a list of published placeholders, not an entropy check - a rule
        strong enough to reject this would also reject keys people genuinely generated.
        """
        assert _settings().SECRET_KEY.get_secret_value() == "test-secret-key-for-testing-only"


class TestEveryDeployedEnvironmentRequiresARelay:
    """Sign-in is passwordless. With no relay there is no way to deliver a magic link, so
    nobody can get in - not even the first user of a fresh install - and the fallback that
    logs the link instead writes a live credential to whatever collects the logs.
    """

    @pytest.mark.parametrize("environment", [EnvironmentOption.PRODUCTION, EnvironmentOption.STAGING])
    def test_a_deployed_environment_without_smtp_fails_startup(self, environment):
        with pytest.raises(ValueError, match="SMTP_HOST"):
            _settings(ENVIRONMENT=environment)

    @pytest.mark.parametrize("environment", [EnvironmentOption.PRODUCTION, EnvironmentOption.STAGING])
    def test_a_deployed_environment_with_a_relay_is_accepted(self, environment):
        settings = _settings(
            ENVIRONMENT=environment,
            SMTP_HOST="smtp.example.com",
            EMAIL_FROM_ADDRESS="noreply@opendiving.example",
        )

        assert settings.SMTP_HOST == "smtp.example.com"

    def test_the_error_names_the_environment_that_was_configured(self):
        """`staging` used to be allowed through here, so an operator hitting this for the
        first time needs the message to name their own value rather than `production`.
        """
        with pytest.raises(ValueError, match="staging"):
            _settings(ENVIRONMENT=EnvironmentOption.STAGING)

    def test_local_may_log_the_link_instead(self):
        """The one environment where reading the link out of the logs is the documented
        way to sign in.
        """
        settings = _settings(ENVIRONMENT=EnvironmentOption.LOCAL)

        assert settings.SMTP_HOST is None


class TestGoogleSignInNeedsBothHalvesOfItsClient:
    """Signing in with Google redeems an authorization code at Google's token endpoint,
    which takes the client secret as well as the id. An instance with only the id renders
    the button and 401s every click, and the web app - which decides whether to render it
    from its *own* `GOOGLE_CLIENT_ID` - has no way to learn that the API is short a secret.
    So startup refuses rather than degrading.

    **Every case here names both variables explicitly**, and that is not belt-and-braces.
    `GOOGLE_CLIENT_ID` is a class-level default read from `src/.env` at import time, so a
    test that omits it is testing whatever the developer running it happens to have
    configured - which is a different guard on their machine than in CI, where there is no
    `src/.env` at all.
    """

    def test_an_id_without_a_secret_fails_startup(self):
        with pytest.raises(ValueError, match="GOOGLE_CLIENT_SECRET"):
            _settings(GOOGLE_CLIENT_ID="an-id.apps.googleusercontent.com", GOOGLE_CLIENT_SECRET=None)

    def test_the_error_names_both_variables_and_what_to_do(self):
        with pytest.raises(ValueError) as raised:
            _settings(GOOGLE_CLIENT_ID="an-id.apps.googleusercontent.com", GOOGLE_CLIENT_SECRET=None)

        message = str(raised.value)
        assert "GOOGLE_CLIENT_ID" in message
        assert "GOOGLE_CLIENT_SECRET" in message
        assert "Google Cloud Console" in message

    @pytest.mark.parametrize("secret", ["", "   "])
    def test_a_blank_secret_is_not_a_secret(self, secret: str):
        """`SecretStr("")` is truthy - it defines no `__bool__` - so a plain falsiness check
        would let an empty variable through and fail at the first sign-in instead.
        """
        with pytest.raises(ValueError, match="GOOGLE_CLIENT_SECRET"):
            _settings(GOOGLE_CLIENT_ID="an-id.apps.googleusercontent.com", GOOGLE_CLIENT_SECRET=secret)

    def test_both_halves_is_the_configuration_that_boots(self):
        settings = _settings(
            GOOGLE_CLIENT_ID="an-id.apps.googleusercontent.com", GOOGLE_CLIENT_SECRET="a-real-looking-secret"
        )

        assert settings.GOOGLE_CLIENT_SECRET is not None
        assert settings.GOOGLE_CLIENT_SECRET.get_secret_value() == "a-real-looking-secret"

    def test_neither_half_is_an_instance_with_google_sign_in_switched_off(self):
        settings = _settings(GOOGLE_CLIENT_ID=None, GOOGLE_CLIENT_SECRET=None)

        assert settings.GOOGLE_CLIENT_ID is None

    def test_a_stray_secret_without_an_id_is_not_worth_failing_over(self):
        """Only one direction is guarded. Google sign-in is off either way, and a leftover
        variable is not a broken instance.
        """
        settings = _settings(GOOGLE_CLIENT_ID=None, GOOGLE_CLIENT_SECRET="left-behind")

        assert settings.GOOGLE_CLIENT_ID is None

    def test_the_secret_has_no_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)

        assert _config_loaded_without_an_env_file(tmp_path, monkeypatch).settings.GOOGLE_CLIENT_SECRET is None

    def test_it_does_not_render_itself(self):
        """`SecretStr`, unlike the bare `str | None` its neighbours use, so a settings
        object reaching a traceback or a log line cannot spill it.
        """
        settings = _settings(
            GOOGLE_CLIENT_ID="an-id.apps.googleusercontent.com", GOOGLE_CLIENT_SECRET="a-real-looking-secret"
        )

        assert "a-real-looking-secret" not in repr(settings.GOOGLE_CLIENT_SECRET)
        assert "a-real-looking-secret" not in str(settings.GOOGLE_CLIENT_SECRET)


class TestTheFirstSuperuserAddressHasNoDefault:
    """`ADMIN_EMAIL` used to default to `admin@admin.com`, a real domain belonging to
    somebody else. Sign-in is passwordless and keyed on the address, so
    `scripts.create_first_superuser` would have created an `is_superuser` row whose magic
    link is delivered to whoever runs that domain.
    """

    def test_unset_is_a_configuration_the_app_accepts(self):
        """It was typed `str`, so an install that deliberately set nothing used to get the
        stranger's address rather than no account.
        """
        assert _settings(ADMIN_EMAIL=None).ADMIN_EMAIL is None

    def test_nothing_configuring_it_leaves_it_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ADMIN_EMAIL", raising=False)

        assert _config_loaded_without_an_env_file(tmp_path, monkeypatch).settings.ADMIN_EMAIL is None

    def test_the_display_name_keeps_its_default(self, tmp_path, monkeypatch):
        """`ADMIN_NAME` is a label on a row - nothing is keyed on it and nothing is
        delivered to it - so it is not the same question.
        """
        monkeypatch.delenv("ADMIN_NAME", raising=False)

        assert _config_loaded_without_an_env_file(tmp_path, monkeypatch).settings.ADMIN_NAME == "admin"


class TestRegistrationSettings:
    """`REGISTRATION_MODE` and the four numbers beside it.

    The default is the load-bearing one and it is a **breaking change**: an instance that
    sets nothing is now invite-only, where before it took anybody. Read off a `config.py`
    loaded with no `.env` in reach, because `config()` resolves its default at import
    against the developer's own `src/.env` - so reading `settings.REGISTRATION_MODE`
    directly would report what that file happens to say rather than what the code declares,
    which is exactly the assertion that would stop meaning anything.
    """

    def test_an_instance_that_configures_nothing_is_invite_only(self, tmp_path, monkeypatch):
        monkeypatch.delenv("REGISTRATION_MODE", raising=False)
        module = _config_loaded_without_an_env_file(tmp_path, monkeypatch)

        assert module.settings.REGISTRATION_MODE is module.RegistrationMode.INVITE

    def test_open_is_the_other_value_and_it_is_accepted(self):
        from src.app.core.config import RegistrationMode

        assert _settings(REGISTRATION_MODE=RegistrationMode.OPEN).REGISTRATION_MODE is RegistrationMode.OPEN

    @pytest.mark.parametrize("value", ["closed", "invite_only", "OPEN ", "", "true"])
    def test_an_unknown_value_fails_at_import(self, value: str):
        """The loud failure the boot guard section wants: `Settings()` runs at import, so a
        typo kills the container at startup and `pytest` at collection, rather than
        selecting a mode nobody chose. `EnvironmentOption` is the shape being copied."""
        with pytest.raises(ValueError):
            _settings(REGISTRATION_MODE=value)

    def test_the_quota_defaults_to_five_a_day(self, tmp_path, monkeypatch):
        """A rate rather than a lifetime allotment. Both halves are read together because
        neither means anything alone."""
        for name in ("INVITATIONS_PER_USER", "INVITATIONS_WINDOW_DAYS"):
            monkeypatch.delenv(name, raising=False)
        module = _config_loaded_without_an_env_file(tmp_path, monkeypatch)

        assert module.settings.INVITATIONS_PER_USER == 5
        assert module.settings.INVITATIONS_WINDOW_DAYS == 1

    def test_the_request_limits_mirror_the_contact_form(self, tmp_path, monkeypatch):
        """Same shape and same values: both endpoints are anonymous and both act on a
        stranger's say-so, so a divergence here would be a number with no argument behind
        it. Asserted against the contact form's own defaults rather than against literals,
        so the two move together or the test says so."""
        for name in (
            "INVITE_REQUEST_RATE_LIMIT_WINDOW_SECONDS",
            "INVITE_REQUEST_RATE_LIMIT_PER_EMAIL",
            "INVITE_REQUEST_RATE_LIMIT_PER_IP",
            "CONTACT_FORM_RATE_LIMIT_WINDOW_SECONDS",
            "CONTACT_FORM_RATE_LIMIT_PER_EMAIL",
            "CONTACT_FORM_RATE_LIMIT_PER_IP",
        ):
            monkeypatch.delenv(name, raising=False)
        loaded = _config_loaded_without_an_env_file(tmp_path, monkeypatch).settings

        assert loaded.INVITE_REQUEST_RATE_LIMIT_WINDOW_SECONDS == loaded.CONTACT_FORM_RATE_LIMIT_WINDOW_SECONDS
        assert loaded.INVITE_REQUEST_RATE_LIMIT_PER_EMAIL == loaded.CONTACT_FORM_RATE_LIMIT_PER_EMAIL
        assert loaded.INVITE_REQUEST_RATE_LIMIT_PER_IP == loaded.CONTACT_FORM_RATE_LIMIT_PER_IP

    def test_every_new_setting_is_in_the_template(self):
        """`src/.env.example`'s own rule: a setting that can be silently wrong belongs there
        as a commented block showing its default. The template is the canonical setup, so a
        setting missing from it is one a self-hoster only discovers by reading the source.
        """
        from pathlib import Path

        template = (Path(__file__).resolve().parents[1] / "src" / ".env.example").read_text()

        for name in (
            "REGISTRATION_MODE",
            "INVITATIONS_PER_USER",
            "INVITATIONS_WINDOW_DAYS",
            "INVITE_REQUEST_RATE_LIMIT_WINDOW_SECONDS",
            "INVITE_REQUEST_RATE_LIMIT_PER_EMAIL",
            "INVITE_REQUEST_RATE_LIMIT_PER_IP",
        ):
            assert name in template, name

        # The line to uncomment is the *non-default* value, the way `# WEB_NOINDEX=true`
        # flips away from its default in the bundle's own template: uncommenting a block is
        # a decision, and a template that restated the default would make it a no-op.
        assert '# REGISTRATION_MODE="open"' in template


class TestLogLevel:
    def test_a_level_name_is_normalized(self):
        assert _settings(LOG_LEVEL=" debug ").LOG_LEVEL == "DEBUG"

    def test_the_default_survives_normalization(self):
        assert _settings(LOG_LEVEL="INFO").LOG_LEVEL in logging.getLevelNamesMapping()

    def test_an_unknown_level_fails_startup(self):
        """Rather than raising from inside `configure_logging` at startup, where the
        traceback points at the logging module instead of at the typo.
        """
        with pytest.raises(ValueError, match="LOG_LEVEL"):
            _settings(LOG_LEVEL="verbose")


class TestPostgresCredentialsAreUrlEncoded:
    """`POSTGRES_URI` is interpolated into a URL, so a password containing URL syntax used
    to produce a DSN nobody wrote - and an error naming a host nobody configured.
    """

    def test_an_at_sign_in_the_password_does_not_split_the_dsn(self):
        assert postgres_uri("postgres", "p@ss", "db", 5432, "opendive") == "postgres:p%40ss@db:5432/opendive"

    @pytest.mark.parametrize("character,encoded", [("@", "%40"), ("/", "%2F"), (":", "%3A"), ("#", "%23")])
    def test_every_character_that_would_change_the_parse(self, character: str, encoded: str):
        assert postgres_uri("postgres", f"a{character}b", "db", 5432, "opendive").startswith(f"postgres:a{encoded}b@")

    def test_an_ordinary_password_is_left_alone(self):
        assert postgres_uri("postgres", "hunter2", "db", 5432, "opendive") == "postgres:hunter2@db:5432/opendive"

    def test_the_username_is_encoded_too(self):
        assert postgres_uri("open@diving", "pw", "db", 5432, "opendive").startswith("open%40diving:")


class TestRedisUrl:
    def test_no_password_leaves_the_url_bare(self):
        assert redis_url("redis", 6379, None) == "redis://redis:6379"

    def test_an_empty_password_is_treated_as_none(self):
        """An unset `REDIS_PASSWORD` reaches this as `""` through some paths, and
        `redis://:@host` is not the same URL as `redis://host`.
        """
        assert redis_url("redis", 6379, "") == "redis://redis:6379"

    def test_a_password_is_encoded(self):
        assert redis_url("redis", 6379, "p@ss/word") == "redis://:p%40ss%2Fword@redis:6379"


class TestSplitCsv:
    def test_none_and_empty_are_no_entries(self):
        assert split_csv(None) == []
        assert split_csv("") == []
        assert split_csv(" , ,") == []

    def test_entries_are_stripped(self):
        assert split_csv("203.0.113.7, 10.0.0.0/8 ,,192.0.2.1") == ["203.0.113.7", "10.0.0.0/8", "192.0.2.1"]

    def test_the_admin_allowlists_read_as_lists(self):
        settings = _settings(CRUD_ADMIN_ALLOWED_IPS="203.0.113.7, 203.0.113.8")

        assert split_csv(settings.CRUD_ADMIN_ALLOWED_IPS) == ["203.0.113.7", "203.0.113.8"]

    def test_a_bare_cidr_block_needs_no_json(self):
        """The reason these are comma-strings rather than `list[str]`: pydantic-settings
        parses a complex field type out of the environment itself and expects JSON, so
        `CRUD_ADMIN_ALLOWED_NETWORKS=10.0.0.0/8` used to be a startup failure.
        """
        settings = _settings(CRUD_ADMIN_ALLOWED_NETWORKS="10.0.0.0/8")

        assert split_csv(settings.CRUD_ADMIN_ALLOWED_NETWORKS) == ["10.0.0.0/8"]


class TestAuthCookieSecure:
    def test_it_defaults_to_true(self):
        """Asserted against the source rather than the live value, for the reason
        `test_admin_config.TestDefaults` documents: `starlette.config.Config` resolves each
        default from the developer's own `src/.env` at import time.
        """
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "src" / "app" / "core" / "config.py").read_text()

        assert 'config("AUTH_COOKIE_SECURE", default=True)' in source

    def test_it_can_be_turned_off_for_a_plain_http_instance(self):
        assert _settings(AUTH_COOKIE_SECURE=False).AUTH_COOKIE_SECURE is False


class TestAppVersionComesFromPackageMetadata:
    """`APP_VERSION` used to be duplicated into `src/.env.example`, so a released image
    reported whatever version the operator's `.env` happened to carry. It reaches
    `/api/v1/health`, the JSON export's `generator` block and the UDDF `<version>`
    element, all of which are how someone reports a bug against a specific build.
    """

    def test_it_matches_the_installed_distribution(self):
        from importlib import metadata

        assert _installed_version() == metadata.version("opendiving-api")

    def test_it_matches_pyproject(self):
        """The point of reading the metadata at all: one source of truth."""
        from pathlib import Path

        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        declared = next(line for line in pyproject.splitlines() if line.startswith("version = "))

        assert _installed_version() == declared.split("=", 1)[1].strip().strip('"')

    def test_an_uninstalled_source_tree_falls_back_to_none(self):
        """The same thing an unset `APP_VERSION` produced before, which every consumer
        already handles - `health.py` answers "unknown", the UDDF writer omits the
        element.
        """
        with patch("src.app.core.config.metadata.version", side_effect=metadata.PackageNotFoundError):
            assert _installed_version() is None

    def test_the_template_no_longer_carries_a_copy(self):
        from pathlib import Path

        template = (Path(__file__).resolve().parents[1] / "src" / ".env.example").read_text()

        assert not any(line.startswith("APP_VERSION") for line in template.splitlines())
