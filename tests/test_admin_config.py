"""Unit tests for the admin-panel startup guard in `core.config`.

The CRUDAdmin panel at `/admin` talks to the models directly, bypassing every ownership
check in `api/v1`. It used to be enabled by default and to fall back to a hardcoded
password published in this repository, with no `ENVIRONMENT` gate of the kind `/docs`
has - so a deploy that set `SECRET_KEY` but forgot `ADMIN_PASSWORD` served a full CRUD
interface behind a known credential. These tests pin the guard that replaced that.
"""

import pytest

from src.app.core.config import LEGACY_DEFAULT_ADMIN_PASSWORD, EnvironmentOption, Settings
from tests.helpers.model_metadata import diver_owned_hard_deleted, soft_deleting_models


def _settings(**overrides):
    """Build a `Settings` without reading the developer's own `src/.env`.

    The SMTP pair is here because these cases are production ones and
    `_require_smtp_outside_local` refuses to boot a production instance that cannot mail a
    sign-in link. It is unrelated to the admin panel - it just has to be satisfied to reach
    the guard under test.
    """
    base = {
        "SECRET_KEY": "test-secret-key-for-testing-only",
        "ENVIRONMENT": EnvironmentOption.PRODUCTION,
        "SMTP_HOST": "smtp.example.com",
        "EMAIL_FROM_ADDRESS": "noreply@opendiving.example",
        "CRUD_ADMIN_ENABLED": True,
        "ADMIN_PASSWORD": "a-real-password",
    }
    return Settings(**{**base, **overrides})


class TestAdminPasswordIsRequiredInProduction:
    def test_missing_password_fails_startup(self):
        with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
            _settings(ADMIN_PASSWORD=None)

    def test_the_old_boilerplate_password_fails_startup(self):
        """It is in this repository's history, so it is a published string, not a secret."""
        with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
            _settings(ADMIN_PASSWORD=LEGACY_DEFAULT_ADMIN_PASSWORD)

    def test_a_real_password_is_accepted(self):
        # The no-allowlist warning is expected here and asserted on its own below.
        settings = _settings(ADMIN_PASSWORD="a-real-password", CRUD_ADMIN_ALLOWED_IPS="203.0.113.7")

        assert settings.ADMIN_PASSWORD == "a-real-password"

    def test_disabling_the_panel_needs_no_password(self):
        """Not setting `ADMIN_PASSWORD` at all is a valid production posture - it means
        no admin account and no panel, which is the safe default.
        """
        settings = _settings(CRUD_ADMIN_ENABLED=False, ADMIN_PASSWORD=None)

        assert settings.CRUD_ADMIN_ENABLED is False

    @pytest.mark.parametrize("environment", [EnvironmentOption.LOCAL, EnvironmentOption.STAGING])
    def test_non_production_is_left_alone(self, environment):
        """Local development runs the panel without ceremony."""
        settings = _settings(ENVIRONMENT=environment, ADMIN_PASSWORD=None)

        assert settings.ENVIRONMENT == environment

    def test_an_enabled_panel_without_an_ip_allowlist_warns(self):
        with pytest.warns(UserWarning, match="CRUD_ADMIN_ALLOWED_IPS"):
            _settings()

    def test_no_warning_once_an_allowlist_is_set(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _settings(CRUD_ADMIN_ALLOWED_IPS="203.0.113.7")


# The models the panel registers, derived rather than listed, so that a new one cannot arrive
# with a `delete` button nobody noticed. Both sets are named by class name because the
# assertions below read `views.py` as text - importing `register_admin_views` means
# constructing a `CRUDAdmin`, which wants a database.
#
# `NOT_IN_THE_PANEL` is what the diver-owned hard-delete predicate reaches that `views.py`
# deliberately leaves out: sign-in state and passkeys are not logbook rows, and a `DiveFile`
# is a payload on the volume that no create/update form could meaningfully accept (`views.py`
# says the same about `CertificationFile`, which the predicate does not reach - it has no
# `user_id`). `User` is the one soft-deleting model registered without `delete`, and for its
# own reason: the account-deletion flow, not this one.
NOT_IN_THE_PANEL = {"AuthenticationRequest", "DiveFile", "WebauthnCredential", "User"}
PANEL_HARD_DELETED = sorted({model.__name__ for model in diver_owned_hard_deleted()} - NOT_IN_THE_PANEL)
PANEL_SOFT_DELETING = sorted({model.__name__ for model in soft_deleting_models()} - NOT_IN_THE_PANEL)


class TestHardDeletedModelsCannotBeDeletedFromThePanel:
    """The diver-owned models that hard-delete are registered without `"delete"`.

    Which models those are is read off the models themselves rather than listed here, for
    the reason `test_hard_delete.py` sets out at length: a new one copied from `GearSet`
    would otherwise arrive with no case anywhere and nothing would fail. The rest is
    asserted against the source for the same reason `TestDefaults` below is - importing
    `register_admin_views` means constructing a `CRUDAdmin`, which wants a database - and
    it is worth asserting at all because the button looks harmless and is not. FastCRUD's
    `delete` branches on whether the model carries `is_deleted`; since these lost it, the
    panel's delete would take the `DELETE FROM` branch and destroy the row, its schedules,
    its service records and every join row through the FK cascades. With **no cache
    invalidation**, which is route-level only, so Redis would go on serving the deleted
    rows for the rest of the TTL.
    """

    @staticmethod
    def _views_source() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "src" / "app" / "admin" / "views.py").read_text()

    @classmethod
    def _actions_for(cls, model: str) -> str:
        source = cls._views_source()
        assert f"model={model}," in source, f"{model} is not registered with the admin panel at all"
        block = source[source.index(f"model={model},") :]
        return block[block.index("allowed_actions=") : block.index("\n    )")]

    @pytest.mark.parametrize("model", PANEL_HARD_DELETED)
    def test_the_view_is_registered_without_delete(self, model: str):
        actions = self._actions_for(model)

        assert '"delete"' not in actions, model
        assert '"view", "create", "update"' in actions, model

    @pytest.mark.parametrize("model", PANEL_SOFT_DELETING)
    def test_the_soft_deleting_models_keep_theirs(self, model: str):
        """The distinction is soft-delete, not caution: these still flag a row rather than
        removing it, so the panel's delete stays what it always was for them."""
        assert '"delete"' in self._actions_for(model), model


class TestTheGlobalCatalogCannotBeDeletedFromThePanel:
    """`Species` and `SpeciesName` are registered without `"delete"` too, but for a
    different reason than the diver-owned hard-deleted models above.

    Those are one diver's rows. A species is **everybody's**: deleting one takes every
    `dive_species` row pointing at it through the FK cascade, silently removing a sighting
    from other people's dives - and with no cache invalidation, since that lives on the API
    routes and there is no route here to hang it on. Nothing in the app deletes a species by
    design (see `models/species.py`), so the panel does not either.

    Asserted against the source for the same reason the class above is: importing
    `register_admin_views` means constructing a `CRUDAdmin`, which wants a database.
    """

    @pytest.mark.parametrize("model", ("Species", "SpeciesName"))
    def test_the_view_is_registered_without_delete(self, model: str):
        source = TestHardDeletedModelsCannotBeDeletedFromThePanel._views_source()
        block = source[source.index(f"model={model},") :]
        actions = block[block.index("allowed_actions=") : block.index("\n    )")]

        assert '"delete"' not in actions, model
        assert '"view", "create", "update"' in actions, model

    def test_the_join_table_keeps_its_delete(self):
        """Unlinking a sighting from a dive is exactly what it should do, and it affects
        only that dive - so `DiveSpecies` is registered like the other join tables."""
        source = TestHardDeletedModelsCannotBeDeletedFromThePanel._views_source()
        block = source[source.index("model=DiveSpecies,") :]
        actions = block[block.index("allowed_actions=") : block.index("\n    )")]

        assert '"delete"' in actions


class TestDefaults:
    """Asserted against the source rather than `Settings.model_fields`, because
    `starlette.config.Config` resolves each field's default from the developer's own
    `src/.env` at import time - so the live default reflects whoever is running the
    tests, not what the code falls back to.
    """

    @staticmethod
    def _config_source() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "src" / "app" / "core" / "config.py").read_text()

    def test_the_admin_panel_is_off_by_default(self):
        assert 'config("CRUD_ADMIN_ENABLED", default=False)' in self._config_source()

    def test_the_admin_password_has_no_default(self):
        source = self._config_source()

        assert 'config("ADMIN_PASSWORD", default=None)' in source
        # The published boilerplate password may only appear as the value the guard
        # rejects, never as a fallback.
        assert f'default="{LEGACY_DEFAULT_ADMIN_PASSWORD}"' not in source
