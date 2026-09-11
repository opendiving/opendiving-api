"""Unit tests for the SMTP-backed transactional email senders."""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest

from src.app.core.config import EnvironmentOption, SMTPTLSMode
from src.app.services.email_service import (
    SMTP_TIMEOUT_SECONDS,
    EmailDeliveryError,
    _build_message,
    _send,
    send_contact_form_email,
    send_email_change_confirmation_email,
    send_gear_service_digest_email,
    send_invitation_email,
    send_magic_link_email,
)


def _configured(mock_settings, **overrides) -> None:
    """Puts a mocked `settings` into the "transport is configured" state.

    `patch(...)` hands back a `MagicMock`, on which every unset attribute is truthy - so
    a test that forgets one of these gets a mock host and a mock TLS mode rather than a
    failure, and the branch it meant to exercise silently isn't the one that runs.
    """
    mock_settings.SMTP_HOST = "smtp.example.com"
    mock_settings.SMTP_PORT = 587
    mock_settings.SMTP_USERNAME = None
    mock_settings.SMTP_PASSWORD = None
    mock_settings.SMTP_TLS_MODE = SMTPTLSMode.STARTTLS
    mock_settings.EMAIL_FROM_ADDRESS = "noreply@opendiving.example"
    # Not transport, but the same trap: `send_invitation_email` branches its opening
    # sentence on this, and an unset attribute on a `MagicMock` is truthy - so every test
    # here would silently exercise the project-operated copy. The default is the one an
    # install gets without touching anything; the tests that want the other side pass it.
    mock_settings.PROJECT_OPERATED = False
    for key, value in overrides.items():
        setattr(mock_settings, key, value)


class TestBuildMessage:
    def test_maps_every_field_onto_the_message(self):
        with patch("src.app.services.email_service.settings") as mock_settings:
            _configured(mock_settings)

            message = _build_message(
                to="diver@example.com",
                subject="Subject line",
                html_body="<p>Body</p>",
                reply_to="stranger@example.com",
            )

            assert message["From"] == "noreply@opendiving.example"
            assert message["To"] == "diver@example.com"
            assert message["Subject"] == "Subject line"
            assert message["Reply-To"] == "stranger@example.com"
            assert message.get_content_type() == "text/html"
            assert "<p>Body</p>" in message.get_content()

    def test_omits_reply_to_when_not_given(self):
        with patch("src.app.services.email_service.settings") as mock_settings:
            _configured(mock_settings)

            message = _build_message(to="diver@example.com", subject="s", html_body="<p>b</p>")

            assert message["Reply-To"] is None

    def test_a_subject_carrying_crlf_does_not_raise(self):
        """`EmailMessage` refuses a header containing a newline. The contact form's
        subject is stranger-typed and unrestricted, so without the flattening in
        `_header_safe` a `\\r\\n` in that JSON string would 500 in our own code.
        """
        with patch("src.app.services.email_service.settings") as mock_settings:
            _configured(mock_settings)

            message = _build_message(
                to="a@example.com", subject="hello\r\nBcc: victim@example.com", html_body="<p>b</p>"
            )

            assert "\n" not in str(message["Subject"])
            assert message["Bcc"] is None

    def test_refuses_to_send_from_nothing(self):
        """The startup validator makes this unreachable; it exists so that a sender
        written without the `SMTP_HOST` check fails loudly rather than mailing `From:
        None`.
        """
        with patch("src.app.services.email_service.settings") as mock_settings:
            _configured(mock_settings, EMAIL_FROM_ADDRESS=None)

            with pytest.raises(EmailDeliveryError):
                _build_message(to="a@example.com", subject="s", html_body="<p>b</p>")


class TestSendOverSMTP:
    """`_send` is the whole transport. These are the tests that would catch a silent
    downgrade to an unverified connection or a connection with no timeout.
    """

    @staticmethod
    def _message() -> MagicMock:
        return MagicMock(name="message")

    def test_starttls_upgrades_a_plain_connection_with_a_verifying_context(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.smtplib") as mock_smtplib,
        ):
            _configured(mock_settings)
            message = self._message()

            _send(message)

            mock_smtplib.SMTP.assert_called_once_with("smtp.example.com", 587, timeout=SMTP_TIMEOUT_SECONDS)
            mock_smtplib.SMTP_SSL.assert_not_called()
            client = mock_smtplib.SMTP.return_value
            context = client.starttls.call_args.kwargs["context"]
            assert isinstance(context, ssl.SSLContext)
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname is True
            client.send_message.assert_called_once_with(message)

    def test_tls_mode_connects_with_smtp_ssl_and_never_starttls(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.smtplib") as mock_smtplib,
        ):
            _configured(mock_settings, SMTP_TLS_MODE=SMTPTLSMode.TLS, SMTP_PORT=465)

            _send(self._message())

            mock_smtplib.SMTP.assert_not_called()
            args, kwargs = mock_smtplib.SMTP_SSL.call_args
            assert args == ("smtp.example.com", 465)
            assert kwargs["timeout"] == SMTP_TIMEOUT_SECONDS
            assert isinstance(kwargs["context"], ssl.SSLContext)
            assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
            mock_smtplib.SMTP_SSL.return_value.starttls.assert_not_called()

    def test_none_mode_neither_wraps_nor_upgrades(self):
        """For a relay on the compose network or the loopback - Mailpit, a local
        postfix - and nothing else.
        """
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.smtplib") as mock_smtplib,
        ):
            _configured(mock_settings, SMTP_TLS_MODE=SMTPTLSMode.NONE, SMTP_HOST="mailpit", SMTP_PORT=1025)

            _send(self._message())

            mock_smtplib.SMTP.assert_called_once_with("mailpit", 1025, timeout=SMTP_TIMEOUT_SECONDS)
            mock_smtplib.SMTP_SSL.assert_not_called()
            mock_smtplib.SMTP.return_value.starttls.assert_not_called()

    def test_does_not_log_in_to_an_anonymous_relay(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.smtplib") as mock_smtplib,
        ):
            _configured(mock_settings, SMTP_USERNAME=None, SMTP_PASSWORD="ignored")

            _send(self._message())

            mock_smtplib.SMTP.return_value.login.assert_not_called()

    def test_logs_in_when_a_username_is_configured(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.smtplib") as mock_smtplib,
        ):
            _configured(mock_settings, SMTP_USERNAME="resend", SMTP_PASSWORD="re_test_key")

            _send(self._message())

            mock_smtplib.SMTP.return_value.login.assert_called_once_with("resend", "re_test_key")

    def test_transport_failures_propagate(self):
        """Load-bearing for the gear digest, which sends before it marks: a swallowed
        failure there is a reminder that silently never arrives.
        """
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.smtplib") as mock_smtplib,
        ):
            _configured(mock_settings)
            mock_smtplib.SMTP.return_value.send_message.side_effect = smtplib.SMTPDataError(451, b"try later")

            with pytest.raises(smtplib.SMTPDataError):
                _send(self._message())

    def test_refuses_to_connect_to_nothing(self):
        with patch("src.app.services.email_service.settings") as mock_settings:
            _configured(mock_settings, SMTP_HOST=None)

            with pytest.raises(EmailDeliveryError):
                _send(self._message())


class TestSendMagicLinkEmail:
    @pytest.mark.asyncio
    async def test_noop_when_no_transport_is_configured(self):
        """`ENVIRONMENT` is pinned rather than left to the mock: the credential guard now
        raises on everything but `local`, and a `MagicMock` attribute is not `local`.
        """
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.smtplib") as mock_smtplib,
        ):
            mock_settings.SMTP_HOST = None
            mock_settings.ENVIRONMENT = EnvironmentOption.LOCAL

            await send_magic_link_email("user@example.com", "https://app.example.com/auth/verify?token=abc", "481052")

            mock_smtplib.SMTP.assert_not_called()
            mock_smtplib.SMTP_SSL.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_over_smtp_when_configured(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            _configured(mock_settings)
            mock_settings.MAGIC_LINK_TOKEN_EXPIRE_MINUTES = 30
            mock_run_sync.return_value = None

            await send_magic_link_email("user@example.com", "https://app.example.com/auth/verify?token=abc", "481052")

            mock_run_sync.assert_called_once()
            _send_fn, message = mock_run_sync.call_args.args
            assert message["To"] == "user@example.com"
            assert message["From"] == "noreply@opendiving.example"
            assert "https://app.example.com/auth/verify?token=abc" in message.get_content()

    @pytest.mark.asyncio
    async def test_the_blocking_client_runs_off_the_event_loop_thread(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            _configured(mock_settings)
            mock_settings.MAGIC_LINK_TOKEN_EXPIRE_MINUTES = 30

            await send_magic_link_email("user@example.com", "https://app.example.com/auth/verify?token=abc", "481052")

            assert mock_run_sync.call_args.args[0] is _send


class TestSendInvitationEmail:
    """The ninth sender, and the one that carries somebody else's name into a stranger's
    inbox."""

    # What `get_content()` returns for the self-hosted mail, verbatim - trailing newline
    # included, since `EmailMessage` adds one. Written out rather than assembled from the
    # sender's own f-strings on purpose: an expected value built the way the code builds it
    # agrees with any edit to either, which is the one thing this assertion exists to catch.
    SELF_HOSTED_SUBJECT = "Ada Reef invited you to OpenDiving"
    SELF_HOSTED_BODY = (
        '<p>Ada Reef has invited you to their OpenDiving log book at <a href="https://dive.example.com">'
        "https://dive.example.com</a>.</p>"
        "<p>Sign in with <strong>invitee@example.com</strong> - the address this was sent to - "
        "and your account will be created:</p>"
        '<p><a href="https://dive.example.com/signin">https://dive.example.com/signin</a></p>'
        "<p>There is no password to choose: you enter your address, and a sign-in link and code arrive in "
        "this mailbox. If you weren't expecting this, you can safely ignore it - nothing has been created "
        "in your name.</p>\n"
    )

    @staticmethod
    async def _invitation(*, project_operated: bool) -> EmailMessage:
        """One invitation, handed back as the message that would have been posted."""
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            _configured(mock_settings, PROJECT_OPERATED=project_operated)
            mock_settings.FRONTEND_URL = "https://dive.example.com"

            await send_invitation_email("invitee@example.com", "Ada Reef")

        # The sender hands `_send` the message as a positional argument; naming the type
        # here is what keeps mypy from reading the rest of this class as `Any`.
        message: EmailMessage = mock_run_sync.call_args.args[1]
        return message

    @pytest.mark.asyncio
    async def test_a_self_hosted_instance_sends_what_it_always_sent(self):
        """`PROJECT_OPERATED` is off on every install but the project's own, so the branch
        below must not cost those instances a single character - not a word reordered, not a
        hyphen turned into a dash. Hence a whole-message comparison rather than a handful of
        `in` checks, which is what the rest of this class uses and what would have let a
        rewrite of the shared paragraphs through."""
        message = await self._invitation(project_operated=False)

        assert message["Subject"] == self.SELF_HOSTED_SUBJECT
        assert message.get_content() == self.SELF_HOSTED_BODY

    @pytest.mark.asyncio
    async def test_the_project_s_own_instance_invites_you_to_opendiving_itself(self):
        """ "Invited you to *their* log book" describes the inviter as the invitee's host,
        which is what a self-hosted instance is and what the instance the project runs is
        not: there the inviter is another diver on the same service, and somebody who was
        told they would be notified when their spot was ready would be reading a sentence
        about a personal logbook nobody offered them. The inviter is still named - it is what
        makes the mail legible rather than a cold one from a domain they may not know."""
        message = await self._invitation(project_operated=True)
        body = message.get_content()

        assert "Ada Reef has invited you to OpenDiving at " in body
        assert '<a href="https://dive.example.com">https://dive.example.com</a>' in body
        assert "their OpenDiving log book" not in body
        # Stronger than the sentence above, and deliberately so: no phrasing of "log book"
        # belongs in this mail on the instance the project operates.
        assert "log book" not in body
        # The subject names the inviter and the app and asserts nothing about who runs
        # either, so it is the same line on both instances rather than a branch writing one
        # sentence twice.
        assert message["Subject"] == self.SELF_HOSTED_SUBJECT

    @pytest.mark.asyncio
    async def test_the_two_instances_differ_in_one_sentence_and_no_more(self):
        """Copy selection, not two emails free to drift apart. Which address to sign in with,
        that there is no password to choose and that an unexpected invitation can be ignored
        are facts about the app, true wherever it runs, and a second copy of them is a second
        place for them to go stale."""
        self_hosted = (await self._invitation(project_operated=False)).get_content()
        project_operated = (await self._invitation(project_operated=True)).get_content()

        assert self_hosted != project_operated
        assert self_hosted.split("</p>", 1)[1] == project_operated.split("</p>", 1)[1]

    @pytest.mark.asyncio
    async def test_it_carries_no_token_and_links_to_signin(self):
        """An invitation is an allow-list entry, not a bearer credential: signing in already
        proves ownership of the address, so a token here would prove nothing the sign-in
        does not - and would be a redemption surface to carry through `/auth/verify` into
        onboarding. The address is in the *body* as well as the header, because a person who
        forwards this needs to know which mailbox was invited."""
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            _configured(mock_settings)
            mock_settings.FRONTEND_URL = "https://dive.example.com"

            await send_invitation_email("Invitee@example.com", "Ada Reef")

        _send_fn, message = mock_run_sync.call_args.args
        body = message.get_content()
        assert message["To"] == "Invitee@example.com"
        assert "Ada Reef" in message["Subject"]
        assert 'href="https://dive.example.com/signin"' in body
        assert "Invitee@example.com" in body
        # No token and no query parameter of any kind - the link is the bare sign-in page.
        assert "token=" not in body
        assert "/signin?" not in body
        # And no claim about the registration mode. `POST /admin/invitations` has no mode
        # check, so a superuser on an `open` instance can send this - a sentence asserting
        # registration is by invitation would be false there, and the copy does not need it:
        # the first paragraph already says who invited them and the second says which
        # address to use.
        assert "by invitation" not in body

    @pytest.mark.asyncio
    async def test_the_inviter_s_name_is_escaped(self):
        """It is another diver's free-text profile field going into HTML somebody else
        reads, which is exactly the shape `send_passkey_added_email` escapes for."""
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            _configured(mock_settings)
            mock_settings.FRONTEND_URL = "https://dive.example.com"

            await send_invitation_email("invitee@example.com", "<script>alert(1)</script>")

        body = mock_run_sync.call_args.args[1].get_content()
        assert "<script>" not in body
        assert "&lt;script&gt;" in body

    @pytest.mark.asyncio
    async def test_it_logs_rather_than_sending_on_local(self):
        """The local-development path every sender has: no relay, so the invitation is
        readable in `docker compose logs api` like the magic link is."""
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.smtplib") as mock_smtplib,
        ):
            mock_settings.SMTP_HOST = None
            mock_settings.ENVIRONMENT = EnvironmentOption.LOCAL
            mock_settings.FRONTEND_URL = "http://localhost:3000"

            await send_invitation_email("invitee@example.com", "Ada Reef")

        mock_smtplib.SMTP.assert_not_called()


class TestSendGearServiceDigestEmail:
    LINES = [
        ("Scubapro MK25 EVO", "Service overdue since 1 Jul 2026", "0199-aaaa"),
        ("Faber AL80", "Hydrostatic test due 20 Aug 2026", "0199-bbbb"),
    ]

    @pytest.mark.asyncio
    async def test_noop_when_no_transport_is_configured(self):
        """Local development without a relay must not hard-fail the cron."""
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.smtplib") as mock_smtplib,
        ):
            mock_settings.SMTP_HOST = None

            await send_gear_service_digest_email("diver@example.com", self.LINES)

            mock_smtplib.SMTP.assert_not_called()

    @pytest.mark.asyncio
    async def test_lists_every_item_and_links_to_it(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            _configured(mock_settings)
            mock_settings.FRONTEND_URL = "https://app.example.com"

            await send_gear_service_digest_email("diver@example.com", self.LINES)

            _send_fn, message = mock_run_sync.call_args.args
            body = message.get_content()
            assert message["To"] == "diver@example.com"
            assert message["Subject"] == "2 pieces of gear need servicing"
            assert "Scubapro MK25 EVO" in body
            assert "Hydrostatic test due 20 Aug 2026" in body
            # Every line links straight to the item it's about...
            assert "https://app.example.com/gear/0199-aaaa" in body
            # ...and there's always a way out of the reminders.
            assert "https://app.example.com/settings" in body

    @pytest.mark.asyncio
    async def test_gear_names_are_escaped(self):
        """Gear names and brands are diver-typed and unconstrained by any schema, so they
        reach this HTML as untrusted input - same footing as the contact form's fields.
        """
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            _configured(mock_settings)
            mock_settings.FRONTEND_URL = "https://app.example.com"

            await send_gear_service_digest_email(
                "diver@example.com",
                [("<img src=x onerror=alert(1)>", "Service overdue since <b>ages</b>", "0199-aaaa")],
            )

            _send_fn, message = mock_run_sync.call_args.args
            body = message.get_content()
            assert "<img src=x" not in body
            assert "&lt;img src=x onerror=alert(1)&gt;" in body
            assert "<b>ages</b>" not in body
            # The markup this function composes itself is still real markup.
            assert "<li><a href=" in body

    @pytest.mark.asyncio
    async def test_subject_is_singular_for_one_item(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            _configured(mock_settings)
            mock_settings.FRONTEND_URL = "https://app.example.com"

            await send_gear_service_digest_email("diver@example.com", self.LINES[:1])

            _send_fn, message = mock_run_sync.call_args.args
            assert message["Subject"] == "Your dive gear needs servicing"


class TestContactFormHeaders:
    """The contact form is the only sender whose text reaches a *header* - see
    `test_contact.py` for the rest of its behaviour.
    """

    @pytest.mark.asyncio
    async def test_a_stranger_typed_subject_with_crlf_is_flattened(self):
        with (
            patch("src.app.services.email_service.settings") as mock_settings,
            patch("src.app.services.email_service.anyio.to_thread.run_sync") as mock_run_sync,
        ):
            _configured(mock_settings)
            mock_settings.CONTACT_FORM_EMAIL = "contact@opendiving.example"

            await send_contact_form_email(
                name="Jacques Cousteau",
                email="jacques@example.com",
                category_label="Bug report",
                subject="Profile chart\r\nBcc: victim@example.com",
                message="The chart renders nothing.",
            )

            _send_fn, message = mock_run_sync.call_args.args
            assert "\n" not in str(message["Subject"])
            assert message["Bcc"] is None


class TestCredentialBearingEmailsOutsideLocal:
    """No transport configured makes the magic-link and email-change senders log the
    full URL - which embeds a live, single-use auth token - to a rotating file on disk.
    That is the right trade on `local` and the wrong one on any instance somebody other
    than the developer can reach, staging included.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("environment", [EnvironmentOption.PRODUCTION, EnvironmentOption.STAGING])
    @pytest.mark.parametrize(
        ("sender", "args"),
        [
            (
                send_magic_link_email,
                ("user@example.com", "https://app.example.com/auth/verify?token=secret", "481052"),
            ),
            (
                send_email_change_confirmation_email,
                ("new@example.com", "https://app.example.com/settings/email?token=secret"),
            ),
            # Nothing here is a credential, and it takes the credential-carrying shape
            # anyway. The reasoning is the *consequence* of a silent failure rather than the
            # sensitivity of the payload: a notice nobody gets costs a heads-up, an
            # invitation nobody gets is an invitee who never learns they were invited while
            # their inviter's quota was spent on it.
            (send_invitation_email, ("invitee@example.com", "Ada Reef")),
        ],
    )
    async def test_raises_instead_of_logging_the_token(self, sender, args, environment):
        with patch("src.app.services.email_service.settings") as mock_settings:
            mock_settings.SMTP_HOST = None
            mock_settings.ENVIRONMENT = environment

            with pytest.raises(EmailDeliveryError):
                await sender(*args)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("environment", [EnvironmentOption.PRODUCTION, EnvironmentOption.STAGING])
    async def test_nothing_is_logged_on_the_way_out(self, caplog, environment):
        """The raise has to happen *before* the warning, not alongside it - a token that
        reaches the log has leaked whether or not the caller also got a 500.
        """
        with patch("src.app.services.email_service.settings") as mock_settings:
            mock_settings.SMTP_HOST = None
            mock_settings.ENVIRONMENT = environment

            with caplog.at_level(logging.WARNING, logger="src.app.services.email_service"):
                with pytest.raises(EmailDeliveryError):
                    await send_magic_link_email(
                        "user@example.com", "https://app.example.com/auth/verify?token=secret", "481052"
                    )

            assert "token=secret" not in caplog.text

    @pytest.mark.asyncio
    async def test_still_logs_the_link_on_local(self, caplog):
        with patch("src.app.services.email_service.settings") as mock_settings:
            mock_settings.SMTP_HOST = None
            mock_settings.ENVIRONMENT = EnvironmentOption.LOCAL

            with caplog.at_level(logging.WARNING, logger="src.app.services.email_service"):
                await send_magic_link_email(
                    "user@example.com", "https://app.example.com/auth/verify?token=secret", "481052"
                )

            # Signing in without a relay configured is the whole point of the fallback.
            assert "token=secret" in caplog.text
