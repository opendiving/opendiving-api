"""Transactional email delivery via Resend (https://resend.com).

Used for the magic-link sign-in email (see `api.v1.auth.request_email_link`), the
email-change confirmation/notification pair (see `api.v1.users`), and the gear-service
digest (see `core.worker.functions.send_gear_service_digests`), all funneling through
`_send` so the "run Resend's blocking client off the event loop" plumbing only lives in
one place.
"""

import logging
from typing import Any

import anyio
import resend

from ..core.config import settings

logger = logging.getLogger(__name__)


def _send(payload: dict[str, Any]) -> None:
    resend.api_key = settings.RESEND_API_KEY
    resend.Emails.send(payload)  # type: ignore[arg-type]


async def send_magic_link_email(email: str, magic_link_url: str) -> None:
    """Sends the magic-link sign-in email.

    A no-op (logged, not raised) when `RESEND_API_KEY` isn't configured, so local
    development without a Resend account doesn't hard-fail `POST /auth/email/request`
    - the link is still generated and logged so it can be used manually.
    """
    if not settings.RESEND_API_KEY:
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
    """
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not configured; gear service digest for %s: %s", email, lines)
        return

    items = "".join(
        f'<li><a href="{settings.FRONTEND_URL}/gear/{item_uuid}"><strong>{label}</strong></a> - {detail}</li>'
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
            f"<p>Your OpenDiving account email was just changed to <strong>{new_email}</strong>.</p>"
            "<p>If you made this change, no action is needed. If you didn't, please "
            "contact support immediately.</p>"
        ),
    }

    await anyio.to_thread.run_sync(_send, payload)
