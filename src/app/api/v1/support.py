"""The frontend's support form (see `app/support/page.tsx` in opendiving-web).

The only endpoint in this API that mails a human rather than a user: submissions are
forwarded to `CONTACT_FORM_EMAIL` with the submitter's address as `reply_to`. Nothing
is stored - there's no inbox in this app to read it from, so the operator's mailbox is
the system of record. Which is also why an instance that hasn't named one answers 503:
there is nowhere else for the message to go.
"""

from fastapi import APIRouter, HTTPException, Request

from ...core.config import settings
from ...core.utils.client_ip import client_ip_bucket
from ...core.utils.rate_limit import enforce_rate_limit
from ...schemas.support import SUPPORT_CATEGORY_LABELS, SupportRequest, SupportResponse
from ...services.email_service import send_support_request_email

router = APIRouter(tags=["support"])

_SUPPORT_RESPONSE = SupportResponse()


@router.post("/support", response_model=SupportResponse)
async def send_support_request(request: Request, body: SupportRequest) -> SupportResponse:
    """Forward a support-form submission to the instance operator.

    Anonymous, so someone locked out of their account can still reach the operator. Nothing
    is stored: the message goes to the operator's mailbox with the submitted `email` as its
    reply-to, and that address is never verified, so the operator reads it as a claim, not
    an identity. Rate limited per IP and per address. 503 when this instance has no support
    address configured, in which case nothing is counted against the limits.
    """
    # Anonymous is what makes this the one endpoint that sends mail on an anonymous caller's
    # say-so, hence the rate limits. No default address: there is none worth guessing for
    # somebody else's install, and the alternative to refusing is delivering a stranger's
    # support request to whichever inbox a stale setting points at.
    #
    # The 503 comes before the rate limits, so an instance that has the form switched off
    # cannot have its buckets spent by traffic that was never going to be delivered.
    if not settings.CONTACT_FORM_EMAIL:
        # A raw `HTTPException`: `core/exceptions/http_exceptions.py` has no class for 503,
        # the same reason `species.resolve` raises its own.
        raise HTTPException(status_code=503, detail="This instance has no support address configured.")

    email = body.email.lower()

    # Per-IP before per-address, as `request_email_link` orders them and for its reason.
    await enforce_rate_limit(
        f"support:ip:{client_ip_bucket(request)}",
        settings.CONTACT_FORM_RATE_LIMIT_PER_IP,
        settings.CONTACT_FORM_RATE_LIMIT_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"support:email:{email}",
        settings.CONTACT_FORM_RATE_LIMIT_PER_EMAIL,
        settings.CONTACT_FORM_RATE_LIMIT_WINDOW_SECONDS,
    )

    await send_support_request_email(
        name=body.name,
        email=email,
        category_label=SUPPORT_CATEGORY_LABELS[body.category],
        subject=body.subject,
        message=body.message,
    )

    return _SUPPORT_RESPONSE
