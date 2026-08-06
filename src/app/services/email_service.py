"""Transactional email delivery via Resend (https://resend.com).

Currently only used to send the magic-link sign-in email (see
`api.v1.auth.request_email_link`), but kept as its own module so other transactional
emails can reuse `_send` without duplicating the "run Resend's blocking client off the
event loop" plumbing.
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
