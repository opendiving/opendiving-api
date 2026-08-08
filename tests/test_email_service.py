"""Unit tests for the Resend-backed magic-link email sender."""

from unittest.mock import patch

import pytest

from src.app.services.email_service import send_magic_link_email


class TestSendMagicLinkEmail:
    @pytest.mark.asyncio
    async def test_noop_when_resend_not_configured(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.resend") as mock_resend,
        ):
            mock_settings.RESEND_API_KEY = None

            await send_magic_link_email("user@example.com", "https://app.example.com/auth/verify?token=abc")

            mock_resend.Emails.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_via_resend_when_configured(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            mock_settings.RESEND_API_KEY = "re_test_key"
            mock_settings.EMAIL_FROM_ADDRESS = "onboarding@resend.dev"
            mock_settings.MAGIC_LINK_TOKEN_EXPIRE_MINUTES = 30
            mock_run_sync.return_value = None

            await send_magic_link_email("user@example.com", "https://app.example.com/auth/verify?token=abc")

            mock_run_sync.assert_called_once()
            _send_fn, payload = mock_run_sync.call_args.args
            assert payload["to"] == "user@example.com"
            assert payload["from"] == "onboarding@resend.dev"
            assert "https://app.example.com/auth/verify?token=abc" in payload["html"]

    @pytest.mark.asyncio
    async def test_send_helper_sets_api_key_and_calls_resend(self):
        from src.app.services.email_service import _send

        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.resend") as mock_resend,
        ):
            mock_settings.RESEND_API_KEY = "re_test_key"
            payload = {"to": "user@example.com"}

            _send(payload)

            assert mock_resend.api_key == "re_test_key"
            mock_resend.Emails.send.assert_called_once_with(payload)
