"""The frontend's contact form (see `app/contact/page.tsx` in opendiving-web).

The only endpoint in this API that mails a human rather than a user: submissions are
forwarded to `CONTACT_FORM_EMAIL` with the submitter's address as `reply_to`. Nothing
is stored - there's no inbox in this app to read it from, so the operator's mailbox is
the system of record. Which is also why an instance that hasn't named one answers 503:
there is nowhere else for the message to go.
"""

from fastapi import APIRouter, HTTPException, Request

from ...core.config import settings
from ...core.utils.client_ip import client_ip
from ...core.utils.rate_limit import enforce_rate_limit
from ...schemas.contact import CONTACT_CATEGORY_LABELS, ContactMessageRequest, ContactMessageResponse
from ...services.email_service import send_contact_form_email

router = APIRouter(tags=["contact"])

_CONTACT_RESPONSE = ContactMessageResponse()


@router.post("/contact", response_model=ContactMessageResponse)
async def send_contact_message(request: Request, body: ContactMessageRequest) -> ContactMessageResponse:
    """Forwards a contact-form submission to the instance operator.

    Unauthenticated by design - someone locked out of their account is exactly the
    person who needs this - which also makes it the one endpoint that will send mail
    on an anonymous caller's say-so, hence the rate limits below.

    The submitted email address is never verified (no confirmation round-trip), so
    treat the `From:` line in the resulting mail as a claim, not an identity.

    503 when this instance has no `CONTACT_FORM_EMAIL`, which is the default: there is no
    address worth guessing for somebody else's install, and the alternative to refusing is
    delivering a stranger's support request to whichever inbox the setting used to point
    at - which is what this endpoint did until the default came out.
    """
    # Before the rate limits, so an instance that has the form switched off cannot have
    # its buckets spent by traffic that was never going to be delivered.
    if not settings.CONTACT_FORM_EMAIL:
        # A raw `HTTPException`: `core/exceptions/http_exceptions.py` has no class for 503,
        # the same reason `species.resolve` raises its own.
        raise HTTPException(status_code=503, detail="This instance has no contact address configured.")

    email = body.email.lower()

    await enforce_rate_limit(
        f"contact:email:{email}",
        settings.CONTACT_FORM_RATE_LIMIT_PER_EMAIL,
        settings.CONTACT_FORM_RATE_LIMIT_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"contact:ip:{client_ip(request)}",
        settings.CONTACT_FORM_RATE_LIMIT_PER_IP,
        settings.CONTACT_FORM_RATE_LIMIT_WINDOW_SECONDS,
    )

    await send_contact_form_email(
        name=body.name,
        email=email,
        category_label=CONTACT_CATEGORY_LABELS[body.category],
        subject=body.subject,
        message=body.message,
    )

    return _CONTACT_RESPONSE
