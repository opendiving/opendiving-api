"""A stranger's request for an invitation into a closed instance.

**In `open` mode the feature is absent rather than idle**: the route answers `404`, since
anyone may register there and there is nothing to ask the operator for. A member brings
somebody in through their connect link, which is not an invitation feature and exists in
every mode, in `api.v1.connect_links`.

The operator's invitations are next door in `api.v1.admin`, behind `get_current_superuser`.
The gate that reads their rows is `services.registration_gate`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.utils.client_ip import client_ip_bucket
from ...core.utils.rate_limit import enforce_rate_limit
from ...core.utils.request_context import RequestContext
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_invite_requests import record_invite_request
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.invite_request import InviteRequestAccepted, InviteRequestSubmission
from ...services.registration_gate import registration_is_invite_only

router = APIRouter(tags=["invitations"])

# What the route says on an `open`-mode instance. A 404 rather than a 403 or a 501
# because the feature genuinely is not here: there is nothing to be forbidden from.
_FEATURE_ABSENT = "This instance does not use invitations."

_INVITE_REQUEST_ACCEPTED = InviteRequestAccepted()


def _require_invite_mode() -> None:
    """404 unless this instance is invite-only."""
    if not registration_is_invite_only():
        raise NotFoundException(_FEATURE_ABSENT)


@router.post("/invite-requests", response_model=InviteRequestAccepted, status_code=202)
async def request_an_invite(
    request: Request, body: InviteRequestSubmission, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> InviteRequestAccepted:
    """Ask the operator of a closed instance for an invitation.

    Anonymous. **The answer is identical for every address** - first request or fiftieth,
    with an account already or without one, invited last week or never - so it never reveals
    whether an address is known here. Rate limited per IP and per address; over either limit
    is a 429. 404 on an instance in `open` registration mode: there is nothing to request an
    invitation to.
    """
    # The sameness is structural rather than a matter of shaping the response: this handler
    # never queries the `user` table at all, exactly as `POST /auth/email/request` never does.
    # An anonymous endpoint that answered differently would be an account-existence oracle for
    # anybody who can reach the port, which is precisely what the sign-in flow goes to some
    # length to avoid being. Two consequences accepted: a row may be stored for an address that
    # already has an account - the operator's queue carries `has_account` so they can see it and
    # remove it - and a repeat submission stores nothing, the insert being
    # `ON CONFLICT DO NOTHING`, while its audit row is still written (see below).
    _require_invite_mode()

    email = body.email.lower()

    # The mode check is above these deliberately, the way `send_support_request` puts its
    # 503 above its own: an instance where this feature is off must not have its buckets
    # spent by traffic that was never going to be stored.
    # Per-IP before per-address, as `request_email_link` orders them and for its reason.
    await enforce_rate_limit(
        f"invite-request:ip:{client_ip_bucket(request)}",
        settings.INVITE_REQUEST_RATE_LIMIT_PER_IP,
        settings.INVITE_REQUEST_RATE_LIMIT_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"invite-request:email:{email}",
        settings.INVITE_REQUEST_RATE_LIMIT_PER_EMAIL,
        settings.INVITE_REQUEST_RATE_LIMIT_WINDOW_SECONDS,
    )

    await record_invite_request(db, email=email, commit=False)
    # Unconditional, including on the submission whose insert did nothing: what this row
    # carries beyond the address is the IP and User-Agent, and those are the whole reason
    # an anonymous write site has an audit event at all. User-less, so it ages out on the
    # 7-day anonymous tier like the other pre-account events.
    await record_auth_event(
        db, event_type=AuthEventType.INVITE_REQUESTED, context=RequestContext.from_request(request), email=email
    )
    await db.commit()

    return _INVITE_REQUEST_ACCEPTED
