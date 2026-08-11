"""Transactional email delivery via Resend (https://resend.com).

Used for the magic-link sign-in email (see `api.v1.auth.request_email_link`), the
email-change confirmation/notification pair (see `api.v1.users`), the gear-service
digest (see `core.worker.functions.send_gear_service_digests`), and the contact form
(see `api.v1.contact`), all funneling through `_send` so the "run Resend's blocking
client off the event loop" plumbing only lives in one place.
"""

import html
import logging
from typing import Any

import anyio
import resend

from ..core.config import EnvironmentOption, settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    """Raised when an email that carries a credential cannot be delivered."""


def _send(payload: dict[str, Any]) -> None:
    resend.api_key = settings.RESEND_API_KEY
    resend.Emails.send(payload)  # type: ignore[arg-type]


def _refuse_to_log_credential_in_production(what: str) -> None:
    """Guards the "no API key, so log the link instead" fallback used by the two senders
    whose URL embeds a live single-use auth token.

    That fallback is a local-development convenience, and a good one - it's how you sign
    in without a Resend account. But the URL it prints *is* the credential, and
    `core.logger` writes to a rotating file on disk, so the same code path in production
    would quietly turn a forgotten `RESEND_API_KEY` into sign-in tokens sitting in
    plaintext. Fail loudly there instead: a 500 on a sign-in attempt is recoverable and
    obvious, a leaked token file is neither.
    """
    if settings.ENVIRONMENT == EnvironmentOption.PRODUCTION:
        raise EmailDeliveryError(f"RESEND_API_KEY is not configured, so {what} cannot be delivered.")


async def send_magic_link_email(email: str, magic_link_url: str) -> None:
    """Sends the magic-link sign-in email.

    A no-op (logged, not raised) when `RESEND_API_KEY` isn't configured, so local
    development without a Resend account doesn't hard-fail `POST /auth/email/request`
    - the link is still generated and logged so it can be used manually. In production
    that same condition raises instead, since the logged link is a live credential (see
    `_refuse_to_log_credential_in_production`).
    """
    if not settings.RESEND_API_KEY:
        _refuse_to_log_credential_in_production("the magic-link sign-in email")
        logger.warning("RESEND_API_KEY not configured; magic link for %s: %s", email, magic_link_url)
        return

    payload = {
        "from": settings.EMAIL_FROM_ADDRESS,
        "to": email,
        "subject": "Your OpenDiving sign-in link",
        "html": (
            "<p>Click the link below to continue signing in to OpenDiving:</p>"
            f'<p><a href="{magic_link_url}">{magic_link_url}</a></p>'
            f"<p>This link expires in {settings.MAGIC_LINK_TOKEN_EXPIRE_MINUTES} minutes "
            "and can only be used once. If you didn't request this, you can safely "
            "ignore this email.</p>"
        ),
    }

    # `resend`'s client makes a blocking HTTP call under the hood - run it off the
    # event loop thread so a slow/hanging call to Resend doesn't stall other requests.
    await anyio.to_thread.run_sync(_send, payload)


async def send_email_change_confirmation_email(new_email: str, confirm_url: str) -> None:
    """Sends the "confirm your new email address" link for `POST
    /user/email-change/request` - deliberately to `new_email`, not the
    account's current one, since the whole point is proving the caller actually
    controls the new address before the change takes effect.
    """
    if not settings.RESEND_API_KEY:
        _refuse_to_log_credential_in_production("the email-change confirmation")
        logger.warning("RESEND_API_KEY not configured; email-change confirmation for %s: %s", new_email, confirm_url)
        return

    payload = {
        "from": settings.EMAIL_FROM_ADDRESS,
        "to": new_email,
        "subject": "Confirm your new OpenDiving email address",
        "html": (
            "<p>Click the link below to confirm this address as your new OpenDiving "
            "account email:</p>"
            f'<p><a href="{confirm_url}">{confirm_url}</a></p>'
            f"<p>This link expires in {settings.EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES} minutes "
            "and can only be used once. If you didn't request this, you can safely "
            "ignore this email - your account email won't change.</p>"
        ),
    }

    await anyio.to_thread.run_sync(_send, payload)


async def send_gear_service_digest_email(email: str, lines: list[tuple[str, str, str]]) -> None:
    """Sends the "your gear needs servicing" digest.

    One email per user per run, never one per item - a diver whose whole kit comes due
    the same week should get a single list, not six separate emails.

    Each entry in `lines` is `(gear_item_label, due_text, gear_item_uuid)`, already
    ordered and phrased by the caller. This function deliberately does no status
    arithmetic of its own, so exactly one place (`services.gear_service`) decides what
    "overdue" means.

    The label and detail are escaped: unlike the magic-link or email-change bodies, they
    are built from `GearItem.brand`/`name` and `GearServiceSchedule.label`, which the
    diver typed and which no schema restricts to safe characters (see
    `core.worker.functions.send_gear_service_digests`). Same reasoning as
    `send_contact_form_email` below - content a person typed gets escaped, content this
    server composed doesn't.
    """
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not configured; gear service digest for %s: %s", email, lines)
        return

    items = "".join(
        f'<li><a href="{settings.FRONTEND_URL}/gear/{html.escape(item_uuid)}">'
        f"<strong>{html.escape(label)}</strong></a> - {html.escape(detail)}</li>"
        for label, detail, item_uuid in lines
    )
    subject = "Your dive gear needs servicing" if len(lines) == 1 else f"{len(lines)} pieces of gear need servicing"

    payload = {
        "from": settings.EMAIL_FROM_ADDRESS,
        "to": email,
        "subject": subject,
        "html": (
            "<p>A quick heads-up before your next trip - this gear is due for service:</p>"
            f"<ul>{items}</ul>"
            f'<p><a href="{settings.FRONTEND_URL}/gear">Review your gear</a>, or '
            f'<a href="{settings.FRONTEND_URL}/settings">turn these reminders off</a>.</p>'
        ),
    }

    await anyio.to_thread.run_sync(_send, payload)


async def send_contact_form_email(name: str, email: str, category_label: str, subject: str, message: str) -> None:
    """Forwards a contact-form submission to `CONTACT_FORM_EMAIL`.

    Every other sender in this module mails content this server composed itself; this
    one mails content a *stranger* typed, so it's the one place that has to escape its
    inputs - an unescaped `<a href=...>` in the message body would otherwise render as
    a live link in the recipient's mail client.

    `reply_to` is the submitter's address, so hitting Reply in the inbox answers the
    diver rather than the no-reply `from` address. The message is never sent *as* them
    (`from` stays `EMAIL_FROM_ADDRESS`): the domain's SPF/DKIM only covers our own
    address, and spoofing an arbitrary sender is what gets a domain blocklisted.

    A no-op (logged, not raised) when `RESEND_API_KEY` isn't configured, matching the
    rest of this module - the whole submission is written to the log in that case, so a
    local instance without a Resend account can still see what would have been sent.
    """
    if not settings.RESEND_API_KEY:
        logger.warning(
            "RESEND_API_KEY not configured; contact message from %s <%s> [%s] %s: %s",
            name,
            email,
            category_label,
            subject,
            message,
        )
        return

    body = html.escape(message).replace("\n", "<br>")
    payload = {
        "from": settings.EMAIL_FROM_ADDRESS,
        "to": settings.CONTACT_FORM_EMAIL,
        "reply_to": email,
        "subject": f"[{category_label}] {subject}",
        "html": (
            f"<p><strong>From:</strong> {html.escape(name)} &lt;{html.escape(email)}&gt;<br>"
            f"<strong>Category:</strong> {html.escape(category_label)}<br>"
            f"<strong>Subject:</strong> {html.escape(subject)}</p>"
            "<hr>"
            f"<p>{body}</p>"
        ),
    }

    await anyio.to_thread.run_sync(_send, payload)


async def send_email_changed_notification(old_email: str, new_email: str) -> None:
    """Best-effort security notice sent to an account's *previous* email address once
    a change completes, so the previous owner of that inbox finds out even if they
    weren't the one who made the change.
    """
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not configured; email-change notice for %s -> %s", old_email, new_email)
        return

    payload = {
        "from": settings.EMAIL_FROM_ADDRESS,
        "to": old_email,
        "subject": "Your OpenDiving account email was changed",
        "html": (
            f"<p>Your OpenDiving account email was just changed to <strong>{html.escape(new_email)}</strong>.</p>"
            "<p>If you made this change, no action is needed. If you didn't, please "
            "contact support immediately.</p>"
        ),
    }

    await anyio.to_thread.run_sync(_send, payload)
