"""Unit tests for the support-form endpoint (see `api.v1.support`).

Built against a minimal app exposing only the support router - like
`test_health.py`, this keeps the test off the full application lifespan (DB/Redis
setup), which this endpoint doesn't touch anyway.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.api.v1.support import router as support_router
from src.app.core.config import SMTPTLSMode, settings
from src.app.core.exceptions.http_exceptions import RateLimitException
from src.app.services.email_service import EmailDeliveryError, send_support_request_email

VALID_BODY = {
    "name": "Jacques Cousteau",
    "email": "Jacques@Example.com",
    "category": "import",
    "subject": "Suunto export won't import",
    "message": "The JSON my Ocean exports is rejected with a parse error.",
}


def _make_support_client() -> TestClient:
    app = FastAPI()
    app.include_router(support_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _configured_inbox():
    """Give every route test an instance that has a support address.

    `CONTACT_FORM_EMAIL` has no default, and the route 503s without one - so unpatched,
    these tests would read the developer's own `src/.env` and pass or fail depending on
    whose machine they run on. The 503 itself is asserted below with this fixture
    overridden.
    """
    with patch.object(settings, "CONTACT_FORM_EMAIL", "support@opendiving.example"):
        yield


class TestSendSupportRequest:
    def test_accepts_a_valid_submission_and_forwards_it(self):
        with (
            patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock) as mock_send,
            patch("src.app.api.v1.support.enforce_rate_limit", new_callable=AsyncMock),
        ):
            response = _make_support_client().post("/support", json=VALID_BODY)

            assert response.status_code == 200
            assert "on its way" in response.json()["message"]
            mock_send.assert_awaited_once()

    def test_forwards_the_human_readable_category_label(self):
        """The inbox sees "Dive-computer import", not the `import` slug."""
        with (
            patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock) as mock_send,
            patch("src.app.api.v1.support.enforce_rate_limit", new_callable=AsyncMock),
        ):
            _make_support_client().post("/support", json=VALID_BODY)

            assert mock_send.await_args.kwargs["category_label"] == "Dive-computer import"

    def test_lowercases_the_submitted_email(self):
        """So the per-email rate limit can't be sidestepped by varying the casing."""
        with (
            patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock) as mock_send,
            patch("src.app.api.v1.support.enforce_rate_limit", new_callable=AsyncMock) as mock_limit,
        ):
            _make_support_client().post("/support", json=VALID_BODY)

            assert mock_send.await_args.kwargs["email"] == "jacques@example.com"
            assert mock_limit.await_args_list[0].args[0] == "support:email:jacques@example.com"

    def test_rate_limits_by_email_and_by_ip(self):
        with (
            patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock),
            patch("src.app.api.v1.support.enforce_rate_limit", new_callable=AsyncMock) as mock_limit,
        ):
            _make_support_client().post("/support", json=VALID_BODY)

            keys = [call.args[0] for call in mock_limit.await_args_list]
            assert keys[0].startswith("support:email:")
            assert keys[1].startswith("support:ip:")

    def test_does_not_send_when_rate_limited(self):
        with (
            patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock) as mock_send,
            patch("src.app.api.v1.support.enforce_rate_limit", new_callable=AsyncMock) as mock_limit,
        ):
            mock_limit.side_effect = RateLimitException("Too many requests. Please try again later.")

            response = _make_support_client().post("/support", json=VALID_BODY)

            assert response.status_code == 429
            mock_send.assert_not_awaited()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("email", "not-an-email"),
            ("category", "partnership"),
            ("subject", "hi"),
            ("message", "too short"),
            ("name", ""),
        ],
    )
    def test_rejects_invalid_submissions(self, field: str, value: str):
        with (
            patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock) as mock_send,
            patch("src.app.api.v1.support.enforce_rate_limit", new_callable=AsyncMock),
        ):
            response = _make_support_client().post("/support", json={**VALID_BODY, field: value})

            assert response.status_code == 422
            mock_send.assert_not_awaited()

    def test_rejects_unknown_fields(self):
        """`extra="forbid"` - a form field the API doesn't know about is a bug on one
        side or the other, not something to silently drop on the floor.
        """
        with (
            patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock),
            patch("src.app.api.v1.support.enforce_rate_limit", new_callable=AsyncMock),
        ):
            response = _make_support_client().post("/support", json={**VALID_BODY, "cc": "someone@example.com"})

            assert response.status_code == 422


class TestAnInstanceWithNoSupportAddress:
    """`CONTACT_FORM_EMAIL` has no default, so this is the shipped state of a fresh
    install: the form is off rather than delivering somebody else's support mail to an
    inbox they never chose.
    """

    def test_answers_503_without_sending(self):
        with (
            patch.object(settings, "CONTACT_FORM_EMAIL", None),
            patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock) as mock_send,
            patch("src.app.api.v1.support.enforce_rate_limit", new_callable=AsyncMock),
        ):
            response = _make_support_client().post("/support", json=VALID_BODY)

            assert response.status_code == 503
            mock_send.assert_not_awaited()

    def test_does_not_spend_the_rate_limits(self):
        """The refusal is decided before the buckets are touched, so traffic to a switched
        off form cannot exhaust the allowance of an instance that later configures one.
        """
        with (
            patch.object(settings, "CONTACT_FORM_EMAIL", None),
            patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock),
            patch("src.app.api.v1.support.enforce_rate_limit", new_callable=AsyncMock) as mock_limit,
        ):
            _make_support_client().post("/support", json=VALID_BODY)

            mock_limit.assert_not_awaited()


class TestSendSupportRequestEmail:
    ARGS = {
        "name": "Jacques Cousteau",
        "email": "jacques@example.com",
        "category_label": "Bug report",
        "subject": "Profile chart is empty",
        "message": "The chart renders nothing for my last dive.",
    }

    @staticmethod
    def _configure(mock_settings) -> None:
        mock_settings.SMTP_HOST = "smtp.example.com"
        mock_settings.SMTP_PORT = 587
        mock_settings.SMTP_USERNAME = None
        mock_settings.SMTP_PASSWORD = None
        mock_settings.SMTP_TLS_MODE = SMTPTLSMode.STARTTLS
        mock_settings.EMAIL_FROM_ADDRESS = "noreply@mail.opendiving.app"
        mock_settings.CONTACT_FORM_EMAIL = "support@opendiving.app"

    @pytest.mark.asyncio
    async def test_noop_when_no_transport_is_configured(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.smtplib") as mock_smtplib,
        ):
            mock_settings.SMTP_HOST = None

            await send_support_request_email(**self.ARGS)

            mock_smtplib.SMTP.assert_not_called()
            mock_smtplib.SMTP_SSL.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_to_the_configured_inbox_replying_to_the_submitter(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            self._configure(mock_settings)

            await send_support_request_email(**self.ARGS)

            _send_fn, message = mock_run_sync.call_args.args
            assert message["To"] == "support@opendiving.app"
            # Never sent *as* the submitter - only our own address is SPF/DKIM-covered.
            assert message["From"] == "noreply@mail.opendiving.app"
            assert message["Reply-To"] == "jacques@example.com"
            assert message["Subject"] == "[Bug report] Profile chart is empty"

    @pytest.mark.asyncio
    async def test_escapes_html_in_the_submitted_message(self):
        """The one sender in this module whose content a stranger typed."""
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            self._configure(mock_settings)

            await send_support_request_email(
                **{**self.ARGS, "message": '<a href="https://evil.example">click</a>', "name": "<b>bold</b>"}
            )

            _send_fn, message = mock_run_sync.call_args.args
            body = message.get_content()
            assert "<a href=" not in body
            assert "&lt;a href=" in body
            assert "<b>bold</b>" not in body

    @pytest.mark.asyncio
    async def test_keeps_line_breaks_readable(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            self._configure(mock_settings)

            await send_support_request_email(**{**self.ARGS, "message": "line one\nline two"})

            _send_fn, message = mock_run_sync.call_args.args
            assert "line one<br>line two" in message.get_content()

    @pytest.mark.asyncio
    async def test_raises_rather_than_no_ops_without_a_recipient(self):
        """The route answers 503 before reaching this, so getting here at all is a bug.
        Unlike the missing-transport branch above it raises: the value of that log line is
        that a developer can read what *would* have been sent, and there is no equivalent
        consolation for mail with nowhere to go.
        """
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            self._configure(mock_settings)
            mock_settings.CONTACT_FORM_EMAIL = None

            with pytest.raises(EmailDeliveryError, match="CONTACT_FORM_EMAIL"):
                await send_support_request_email(**self.ARGS)

            mock_run_sync.assert_not_called()
