"""Unit tests for the startup guards and DSN building in `core.config`.

The admin-panel guard has its own module (`test_admin_config.py`) and the from-address one
`test_email_config.py`; this covers the rest of what a fresh install can get wrong before
it has served a single request - a `SECRET_KEY` copied out of the template, a production
instance nobody can sign in to, a password with an `@` in it, a `LOG_LEVEL` typo, and a
setting the app no longer reads.
"""

import ast
import importlib.util
import logging
import re
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import pytest
import starlette.config

from src.app.core.config import (
    PLACEHOLDER_SECRET_KEYS,
    S3_REQUIRED_SETTINGS,
    EnvironmentOption,
    FileStorageBackendOption,
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

    def test_the_refusal_points_at_a_file_the_reader_has(self):
        """It used to name `src/.env.example` as the source of the placeholder. The front
        door's `example.env` ships the same value and its by-hand install downloads that
        file straight to `.env`, so an operator meets this refusal with no `src/` tree on
        the machine at all - sent, by the error itself, to a file they cannot open. `.env`
        is the file both audiences are editing.
        """
        with pytest.raises(ValueError) as raised:
            _settings(SECRET_KEY="change-me-openssl-rand-hex-32")

        message = str(raised.value)
        assert "your .env" in message
        assert "src/" not in message
        assert "openssl rand -hex 32" in message

    def test_the_refusal_keeps_the_prefix_the_front_door_indexes_on(self):
        """The front door's `docs/troubleshooting.md` opens an entry with this string in
        bold - it is what an operator pastes in from their terminal, and nothing over there
        is generated from this file. Rewording the opening breaks that page's findability
        with no error anywhere, which is how the `.env` fix nearly went in. Changing it is a
        deliberate pair of edits across two repositories, not a local one.

        Asserted as a substring rather than a prefix because pydantic wraps this in its own
        `1 validation error for Settings / Value error, …` frame - what reaches the
        operator's terminal never starts with our text, and the thing the page has to match
        is the contiguous phrase inside it.
        """
        with pytest.raises(ValueError) as raised:
            _settings(SECRET_KEY="change-me-openssl-rand-hex-32")

        assert "SECRET_KEY is unset or still a placeholder" in str(raised.value)

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
        """`SecretStr("")` is falsy - it defines `__len__` - and the `if settings.X` checks
        on optional secrets rely on that, but `SecretStr("   ")` is not, so a falsiness check
        alone would let a blank variable through and fail at the first sign-in instead.
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


class TestRetiredSettings:
    @pytest.mark.parametrize("name", ["ADMIN_NAME", "ADMIN_EMAIL", "CRUD_ADMIN_REDIS_SSL"])
    def test_one_left_in_the_environment_does_not_stop_startup(self, name, tmp_path, monkeypatch):
        """`Settings` forbids extra keys, but pydantic-settings reads only the variables named
        by its fields, so an operator's configuration still setting one of these upgrades
        cleanly.
        """
        monkeypatch.setenv(name, "true")

        assert not hasattr(_config_loaded_without_an_env_file(tmp_path, monkeypatch).settings, name)


class TestRegistrationSettings:
    """`REGISTRATION_MODE`, `PROJECT_OPERATED` and the four numbers beside them.

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

    def test_an_instance_that_configures_nothing_is_not_the_project_s(self, tmp_path, monkeypatch):
        """`PROJECT_OPERATED` is off unless somebody says otherwise: the generic landing-page
        copy is the one true of every install, and only the instance the project itself runs
        has a reason to claim to be it. Read off a `config.py` loaded with no `.env` in reach,
        for the reason the mode's default is."""
        monkeypatch.delenv("PROJECT_OPERATED", raising=False)
        module = _config_loaded_without_an_env_file(tmp_path, monkeypatch)

        assert module.settings.PROJECT_OPERATED is False

    def test_the_project_s_own_instance_switches_it_on(self):
        assert _settings(PROJECT_OPERATED=True).PROJECT_OPERATED is True

    def test_the_quota_defaults_to_five_a_day(self, tmp_path, monkeypatch):
        """A rate rather than a lifetime allotment. Both halves are read together because
        neither means anything alone."""
        for name in ("INVITATIONS_PER_USER", "INVITATIONS_WINDOW_DAYS"):
            monkeypatch.delenv(name, raising=False)
        module = _config_loaded_without_an_env_file(tmp_path, monkeypatch)

        assert module.settings.INVITATIONS_PER_USER == 5
        assert module.settings.INVITATIONS_WINDOW_DAYS == 1

    def test_the_probe_throttle_is_looser_than_the_quota(self, tmp_path, monkeypatch):
        """They bound different things, and only the throttle bounds the 409 oracle.

        The relationship is what is asserted rather than either number: the throttle is a
        backstop against automated probing, the quota is how many people one member may
        invite, and a diver who mistypes an address twice must not be spending the budget
        that decides whether their fifth real invitation goes out.
        """
        for name in ("INVITATIONS_PER_USER", "INVITATION_ATTEMPT_RATE_LIMIT_PER_USER"):
            monkeypatch.delenv(name, raising=False)
        loaded = _config_loaded_without_an_env_file(tmp_path, monkeypatch).settings

        assert loaded.INVITATION_ATTEMPT_RATE_LIMIT_PER_USER > loaded.INVITATIONS_PER_USER

    def test_the_request_limits_mirror_the_support_form(self, tmp_path, monkeypatch):
        """Same shape and same values: both endpoints are anonymous and both act on a
        stranger's say-so, so a divergence here would be a number with no argument behind
        it. Asserted against the support form's own defaults rather than against literals,
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

    def test_the_template_offers_the_non_default_value(self):
        template = (Path(__file__).resolve().parents[1] / "src" / ".env.example").read_text()

        # The line to uncomment is the *non-default* value, the way `# WEB_NOINDEX=true`
        # flips away from its default in the bundle's own template: uncommenting a block is
        # a decision, and a template that restated the default would make it a no-op.
        assert '# REGISTRATION_MODE="open"' in template
        assert "# PROJECT_OPERATED=true" in template
        assert "# JOIN_CHANNELS=" in template


#: `Settings` fields `src/.env.example` deliberately does not list, each for a reason that
#: makes listing it wrong rather than merely unnecessary.
NOT_IN_THE_TEMPLATE = {
    # A copy in the template freezes the build identity at whenever the operator copied
    # it; `pyproject.toml` and the `Dockerfile`'s build arg are the sources.
    "APP_VERSION",
    "APP_COMMIT",
    # The SQLAlchemy dialects the app is built against, not a choice: the image installs
    # `asyncpg` and nothing else for the async side.
    "POSTGRES_SYNC_PREFIX",
    "POSTGRES_ASYNC_PREFIX",
}


def _env_names_by_field() -> dict[str, str]:
    """Each `Settings` field's variable name. Usually the field's own, but `config()` can
    read another (`LICENSE_NAME` reads `LICENSE`), and the template lists what `.env` sets.
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "app" / "core" / "config.py").read_text()
    names: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "config"
        ):
            names[node.target.id] = ast.literal_eval(node.value.args[0])
    return {field: names.get(field, field) for field in Settings.model_fields}


class TestTheTemplateListsEverySetting:
    """The template is what a self-hoster configures from, so a setting missing from it is
    one they discover only by reading the source."""

    @staticmethod
    def _assigned(template: str) -> set[str]:
        """The names on assignment lines, commented out or not: a name, `=`, and one value
        ending the line. Prose that happens to start with `NAME=value` lists nothing."""
        return set(re.findall(r'^#?\s*([A-Z][A-Z0-9_]*)=(?:"[^"]*"|\S*)\s*$', template, re.MULTILINE))

    @classmethod
    def _listed(cls) -> set[str]:
        return cls._assigned((Path(__file__).resolve().parents[1] / "src" / ".env.example").read_text())

    def test_a_comment_mentioning_a_setting_does_not_list_it(self):
        template = (
            'APP_NAME="OpenDiving"\n'
            "# MIGRATE_ON_START=true\n"
            "# FILE_STORAGE_BACKEND=s3 - startup refuses and names whichever is missing\n"
            '# ENVIRONMENT="production" with CRUD_ADMIN_ENABLED=true, startup fails\n'
        )

        assert self._assigned(template) == {"APP_NAME", "MIGRATE_ON_START"}

    def test_every_setting_is_listed(self):
        listed = self._listed()
        names = _env_names_by_field()
        missing = [env for field, env in names.items() if field not in NOT_IN_THE_TEMPLATE and env not in listed]

        assert not missing, f"add these to src/.env.example, or to NOT_IN_THE_TEMPLATE with a reason: {missing}"

    def test_every_exemption_is_a_setting_the_template_leaves_out(self):
        """Keeps the exemptions honest: a retired field or one the template has since
        picked up should leave the list, not linger as a hole the first test can't see."""
        names = _env_names_by_field()

        assert NOT_IN_THE_TEMPLATE <= names.keys()
        assert not {names[field] for field in NOT_IN_THE_TEMPLATE} & self._listed()


class TestCredentialsDoNotRenderThemselves:
    @pytest.mark.parametrize(
        "name",
        [
            "SECRET_KEY",
            "ADMIN_PASSWORD",
            "GOOGLE_CLIENT_SECRET",
            "SMTP_PASSWORD",
            "GEOCODER_API_KEY",
            "S3_SECRET_ACCESS_KEY",
            "CRUD_ADMIN_REDIS_PASSWORD",
            "CRUD_ADMIN_DB_URL",
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
        ],
    )
    def test_neither_repr_nor_str_spills_the_value(self, name: str):
        value = getattr(_settings(**{name: "a-real-looking-secret"}), name)

        assert "a-real-looking-secret" not in repr(value)
        assert "a-real-looking-secret" not in str(value)
        assert value.get_secret_value() == "a-real-looking-secret"

    def test_the_dsns_carry_the_password_as_configured(self, tmp_path, monkeypatch):
        """The DSNs are built from the unwrapped passwords, so what reaches Postgres and
        Redis is the configured value - and `repr(settings)` shows neither."""
        for name, value in {
            "POSTGRES_USER": "opendive",
            "POSTGRES_PASSWORD": "pg-p@ss",
            "POSTGRES_SERVER": "db",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "opendive",
            "REDIS_CACHE_HOST": "redis",
            "REDIS_CACHE_PORT": "6379",
            "REDIS_PASSWORD": "redis-p@ss",
        }.items():
            monkeypatch.setenv(name, value)

        loaded = _config_loaded_without_an_env_file(tmp_path, monkeypatch).settings

        assert loaded.POSTGRES_URI.get_secret_value() == "opendive:pg-p%40ss@db:5432/opendive"
        assert loaded.REDIS_CACHE_URL.get_secret_value() == "redis://:redis-p%40ss@redis:6379"
        assert "p@ss" not in repr(loaded) and "p%40ss" not in repr(loaded)

    def test_a_dsn_in_the_environment_cannot_outrun_its_parts(self, tmp_path, monkeypatch):
        """A set `POSTGRES_URI` would point the app - and the test suite, past its
        `POSTGRES_DB` redirect - somewhere the parts do not say."""
        monkeypatch.setenv("POSTGRES_DB", "opendive_test")
        monkeypatch.setenv("POSTGRES_URI", "elsewhere:pw@other-host:5432/opendive")
        monkeypatch.setenv("REDIS_CACHE_URL", "redis://other-host:6379")

        loaded = _config_loaded_without_an_env_file(tmp_path, monkeypatch).settings

        assert loaded.POSTGRES_URI.get_secret_value().endswith("@localhost:5432/opendive_test")
        assert loaded.REDIS_CACHE_URL.get_secret_value() == "redis://localhost:6379"

    def test_a_refusal_does_not_echo_the_configuration(self):
        """A validator's error would otherwise print `input_value=` with every setting it
        was handed, passwords included."""
        with pytest.raises(ValueError) as excinfo:
            _settings(SECRET_KEY="change-me", POSTGRES_PASSWORD="hunter2-in-the-log")

        assert "hunter2" not in str(excinfo.value)
        assert "input_value" not in str(excinfo.value)


class TestGeocoderDefaults:
    def test_pins_ask_public_nominatim_and_searches_public_photon(self, tmp_path, monkeypatch):
        """Both keyless public instances, so an instance that configures nothing geocodes
        with no third-party account."""
        monkeypatch.delenv("GEOCODER_URL", raising=False)
        monkeypatch.delenv("GEOCODER_SEARCH_URL", raising=False)

        loaded = _config_loaded_without_an_env_file(tmp_path, monkeypatch).settings

        assert (loaded.GEOCODER_URL, loaded.GEOCODER_SEARCH_URL) == (
            "https://nominatim.openstreetmap.org",
            "https://photon.komoot.io",
        )


class TestJoinChannels:
    """`JOIN_CHANNELS`: `slug=Label` pairs, validated at startup like `REGISTRATION_MODE`,
    so a typo stops the process rather than quietly making a posted link dead."""

    def test_an_instance_that_configures_nothing_has_no_channels(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JOIN_CHANNELS", raising=False)
        module = _config_loaded_without_an_env_file(tmp_path, monkeypatch)

        assert module.settings.JOIN_CHANNELS is None
        assert module.settings.join_channels == {}

    @pytest.mark.parametrize("value", ["", " ", ",,"])
    def test_empty_is_no_channels(self, value: str):
        assert _settings(JOIN_CHANNELS=value).join_channels == {}

    def test_pairs_become_a_mapping_in_order(self):
        channels = _settings(JOIN_CHANNELS=" scubaboard = ScubaBoard , reddit=Reddit r/scuba ").join_channels

        assert list(channels.items()) == [("scubaboard", "ScubaBoard"), ("reddit", "Reddit r/scuba")]

    @pytest.mark.parametrize(
        "pair",
        [
            "ScubaBoard=ScubaBoard",
            "scuba_board=ScubaBoard",
            "=ScubaBoard",
            f"{'a' * 33}=Long",
            "waitlist=The Waitlist",
            "bootstrap=First",
            "scubaboard",
            "scubaboard=",
            f"scubaboard={'x' * 41}",
        ],
    )
    def test_a_bad_pair_refuses_to_start_and_names_itself(self, pair: str):
        with pytest.raises(ValueError, match=re.escape(repr(pair))):
            _settings(JOIN_CHANNELS=f"reddit=Reddit,{pair}")

    def test_a_slug_listed_twice_refuses_to_start(self):
        with pytest.raises(ValueError, match="listed twice"):
            _settings(JOIN_CHANNELS="reddit=Reddit,reddit=Reddit again")

    def test_every_fixed_source_word_is_refused(self):
        """The guarantee the counter's key space rests on: a channel named after one of the
        fixed words would be counted as that door."""
        from src.app.core.config import AccountSource

        for word in AccountSource:
            with pytest.raises(ValueError, match="reserved"):
                _settings(JOIN_CHANNELS=f"{word.value}=Anything")

    def test_the_longest_slug_and_label_are_accepted(self):
        slug, label = "a" * 32, "x" * 40

        assert _settings(JOIN_CHANNELS=f"{slug}={label}").join_channels == {slug: label}


class TestTheObjectStoreBackendNeedsItsCredentials:
    """`FILE_STORAGE_BACKEND=s3` with a variable missing is the configuration that starts
    cleanly and then loses a diver's c-card, so it does not start.
    """

    _S3 = {
        "FILE_STORAGE_BACKEND": FileStorageBackendOption.S3,
        "S3_ENDPOINT_URL": "https://example.invalid",
        "S3_BUCKET": "opendiving-files",
        "S3_ACCESS_KEY_ID": "an-access-key",
        "S3_SECRET_ACCESS_KEY": "a-secret",
    }

    def test_a_fully_configured_bucket_boots(self):
        assert _settings(**self._S3).FILE_STORAGE_BACKEND is FileStorageBackendOption.S3

    @pytest.mark.parametrize("missing", S3_REQUIRED_SETTINGS)
    def test_any_one_of_the_four_missing_fails_startup_and_is_named(self, missing: str):
        """Named, because the operator's next action is to go and set that one variable -
        and because botocore's own failure for an unset endpoint mentions none of them."""
        with pytest.raises(ValueError, match=missing):
            _settings(**{**self._S3, missing: None})

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_value_is_not_a_value(self, blank: str):
        with pytest.raises(ValueError, match="S3_BUCKET"):
            _settings(**{**self._S3, "S3_BUCKET": blank})

    def test_the_error_says_how_to_get_back_to_a_working_instance(self):
        with pytest.raises(ValueError, match="FILE_STORAGE_BACKEND=local"):
            _settings(**{**self._S3, "S3_BUCKET": None})

    def test_the_error_points_at_a_file_the_reader_has(self):
        """It used to name `src/.env.example`. A self-hoster installs the front door's
        bundle and has no `src/` tree at all, so that sent them to a file they cannot open -
        in a refusal they hit at startup. `.env` is true for both audiences: the installer
        writes it out of `example.env` keeping every comment, and a developer's `src/.env`
        comes from `src/.env.example` the same way.
        """
        with pytest.raises(ValueError) as raised:
            _settings(**{**self._S3, "S3_BUCKET": None})

        message = str(raised.value)
        assert "object-storage block in your .env" in message
        assert "src/" not in message

    def test_the_local_backend_never_looks_at_the_group(self):
        """The property that keeps this validator off everyone's back, and the reason
        `local` is the default: a cross-field check that fired on an untouched configuration
        would take out `docker compose up` and pytest collection together."""
        assert _settings().FILE_STORAGE_BACKEND is FileStorageBackendOption.LOCAL
        assert _settings(S3_BUCKET=None).S3_ENDPOINT_URL is None

    def test_an_unknown_backend_fails_at_import(self):
        """`FILE_STORAGE_BACKEND=minio` reading as `local` would write a hosted instance's
        uploads into a container layer, silently."""
        with pytest.raises(ValueError):
            _settings(FILE_STORAGE_BACKEND="minio")

    def test_the_defaults_are_the_local_ones(self, tmp_path, monkeypatch):
        for name in ("FILE_STORAGE_BACKEND", *S3_REQUIRED_SETTINGS, "S3_REGION", "S3_PREFIX"):
            monkeypatch.delenv(name, raising=False)
        module = _config_loaded_without_an_env_file(tmp_path, monkeypatch)

        # `FILE_STORAGE_DIR` is deliberately absent from these assertions: `conftest.py` sets
        # it in the environment before the app is importable at all, so no copy of this
        # module can report the declared default.
        assert module.settings.FILE_STORAGE_BACKEND is module.FileStorageBackendOption.LOCAL
        assert module.settings.S3_REGION == "auto"
        assert module.settings.S3_PREFIX is None

    def test_the_group_is_in_the_template(self):
        template = (Path(__file__).resolve().parents[1] / "src" / ".env.example").read_text()

        for name in ("FILE_STORAGE_BACKEND", "FILE_STORAGE_DIR", "S3_REGION", "S3_PREFIX", *S3_REQUIRED_SETTINGS):
            assert name in template, name


class TestTheStorageLimit:
    """One account's share of the store, in MB. On unless blanked, and never zero."""

    def test_an_instance_that_configures_nothing_allows_a_gigabyte(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STORAGE_LIMIT_MB", raising=False)
        module = _config_loaded_without_an_env_file(tmp_path, monkeypatch)

        assert module.settings.STORAGE_LIMIT_MB == 1024

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_is_no_limit(self, blank: str):
        assert _settings(STORAGE_LIMIT_MB=blank).STORAGE_LIMIT_MB is None

    def test_a_blank_line_in_the_env_file_is_no_limit(self, tmp_path, monkeypatch):
        """The file a host run reads through `Config`, where the value arrives as the default.
        It goes through the same validator as an environment variable: pydantic-settings
        validates defaults."""
        monkeypatch.delenv("STORAGE_LIMIT_MB", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("STORAGE_LIMIT_MB=\n")
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-testing-only")
        monkeypatch.setenv("ENVIRONMENT", "local")
        original = starlette.config.Config
        monkeypatch.setattr(starlette.config, "Config", lambda *_args, **_kwargs: original(env_file))
        source = Path(__file__).resolve().parents[1] / "src" / "app" / "core" / "config.py"
        spec = importlib.util.spec_from_file_location("_config_with_a_blank_limit", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.settings.STORAGE_LIMIT_MB is None

    def test_a_blank_environment_variable_is_no_limit(self, tmp_path, monkeypatch):
        """What compose's `env_file` hands the process for `STORAGE_LIMIT_MB=`."""
        monkeypatch.setenv("STORAGE_LIMIT_MB", "")
        module = _config_loaded_without_an_env_file(tmp_path, monkeypatch)

        assert module.settings.STORAGE_LIMIT_MB is None

    def test_a_number_is_megabytes(self):
        assert _settings(STORAGE_LIMIT_MB="2048").STORAGE_LIMIT_MB == 2048

    @pytest.mark.parametrize("value", [0, -1])
    def test_below_one_fails_startup_naming_the_setting_and_the_file(self, value: int):
        """`0` reads as "unlimited" to anyone who has met that convention elsewhere, and would
        refuse every upload here."""
        with pytest.raises(ValueError) as raised:
            _settings(STORAGE_LIMIT_MB=value)

        message = str(raised.value)
        assert "STORAGE_LIMIT_MB" in message
        assert ".env" in message
        assert "blank" in message

    def test_it_is_in_the_template(self):
        template = (Path(__file__).resolve().parents[1] / "src" / ".env.example").read_text()
        assert "STORAGE_LIMIT_MB" in template


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


class TestTheSessionWindows:
    def test_the_defaults_are_a_week_ninety_days_and_a_minute(self, tmp_path, monkeypatch):
        module = _config_loaded_without_an_env_file(tmp_path, monkeypatch)

        assert module.settings.REFRESH_TOKEN_EXPIRE_DAYS == 7
        assert module.settings.EXTENDED_REFRESH_TOKEN_EXPIRE_DAYS == 90
        assert module.settings.REFRESH_TOKEN_REUSE_GRACE_SECONDS == 60

    @pytest.mark.parametrize("value", [-1, 0, 4])
    def test_a_grace_inside_the_rotation_race_fails_startup(self, value: int):
        """The grace is also where a spent token starts revoking its session, so a value
        near zero is not "off" - it signs a diver out for their own two tabs."""
        with pytest.raises(ValueError, match="REFRESH_TOKEN_REUSE_GRACE_SECONDS"):
            _settings(REFRESH_TOKEN_REUSE_GRACE_SECONDS=value)

    def test_the_floor_itself_is_accepted(self):
        assert _settings(REFRESH_TOKEN_REUSE_GRACE_SECONDS=5).REFRESH_TOKEN_REUSE_GRACE_SECONDS == 5


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


class TestDiveConditionsProvider:
    """`DIVE_CONDITIONS_PROVIDER`: off unless named, and a name it does not know stops the
    process rather than quietly fetching nothing."""

    def test_an_instance_that_configures_nothing_fetches_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DIVE_CONDITIONS_PROVIDER", raising=False)
        module = _config_loaded_without_an_env_file(tmp_path, monkeypatch)

        assert module.settings.DIVE_CONDITIONS_PROVIDER == ""
        assert module.settings.dive_conditions is False

    def test_open_meteo_switches_it_on(self):
        assert _settings(DIVE_CONDITIONS_PROVIDER=" open-meteo ").dive_conditions is True

    @pytest.mark.parametrize("value", ["openmeteo", "Open-Meteo", "worldweatheronline"])
    def test_an_unknown_provider_refuses_to_start_and_names_itself(self, value: str):
        with pytest.raises(ValueError, match=re.escape(repr(value))):
            _settings(DIVE_CONDITIONS_PROVIDER=value)

    @pytest.mark.asyncio
    async def test_the_instance_config_says_whether_conditions_are_fetched(self, monkeypatch):
        from src.app.api.v1.config import read_instance_config
        from src.app.core.config import settings

        monkeypatch.setattr(settings, "DIVE_CONDITIONS_PROVIDER", "open-meteo")
        assert (await read_instance_config()).dive_conditions is True
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_PROVIDER", "")
        assert (await read_instance_config()).dive_conditions is False
