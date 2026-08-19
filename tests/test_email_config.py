"""Unit tests for the from-address startup guard in `core.config`.

`EMAIL_FROM_ADDRESS` used to default to `onboarding@resend.dev`, an address deliverable
on exactly one provider's sandbox domain. Once SMTP became the transport that default
stopped being a default and became a trap: through an arbitrary relay it is an SPF/DKIM
failure that surfaces hours later, in a recipient's spam folder, with nothing in this
app's logs pointing at the cause. These tests pin the guard that replaced it.
"""

import pytest

from src.app.core.config import EnvironmentOption, Settings


def _settings(**overrides):
    """Build a `Settings` without reading the developer's own `src/.env`."""
    base = {
        "SECRET_KEY": "test-secret-key-for-testing-only",
        "ENVIRONMENT": EnvironmentOption.LOCAL,
        "CRUD_ADMIN_ENABLED": False,
        "SMTP_HOST": "smtp.example.com",
        "EMAIL_FROM_ADDRESS": "noreply@opendiving.example",
    }
    return Settings(**{**base, **overrides})


class TestFromAddressIsRequiredWithSMTP:
    @pytest.mark.parametrize(
        "environment", [EnvironmentOption.LOCAL, EnvironmentOption.STAGING, EnvironmentOption.PRODUCTION]
    )
    def test_a_relay_with_no_from_address_fails_startup(self, environment):
        """Every environment, deliberately. Unlike the admin-password guard next to it in
        `core.config`, this one has no `ENVIRONMENT` gate: a staging instance mailing from
        an address its relay won't send for is broken in exactly the same way a production
        one is, and finding that out at startup is the whole point.
        """
        with pytest.raises(ValueError, match="EMAIL_FROM_ADDRESS"):
            _settings(ENVIRONMENT=environment, EMAIL_FROM_ADDRESS=None)

    def test_no_relay_needs_no_from_address(self):
        """The documented local setup: nothing is sent, links are logged instead."""
        settings = _settings(SMTP_HOST=None, EMAIL_FROM_ADDRESS=None)

        assert settings.SMTP_HOST is None

    def test_both_configured_is_accepted(self):
        settings = _settings()

        assert settings.EMAIL_FROM_ADDRESS == "noreply@opendiving.example"
