"""Invitations into a closed instance: the member's three routes, and the stranger's one.

**In `open` mode the feature is absent rather than idle.** Every route here answers `404`,
which is what lets the web's settings card remove itself without knowing the mode - the
same self-hiding the sessions and passkeys cards already do on a 404 from their own list
route. Keeping invitations as an "invite a friend" feature on an open instance would be
more states to explain on the privacy page for something nobody has asked for.

The operator's own routes are next door in `api.v1.admin`, behind `get_current_superuser`.
The gate that reads these rows is `services.registration_gate`.
"""

import logging
import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.utils.client_ip import client_ip
from ...core.utils.pagination import clamp_pagination
from ...core.utils.rate_limit import enforce_rate_limit
from ...core.utils.request_context import RequestContext
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_invitations import (
    account_exists_for,
    crud_invitations,
    invitations_created_since,
    live_invitation_from,
)
from ...crud.crud_invite_requests import delete_invite_requests, record_invite_request
from ...models.invitation import Invitation
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.invitation import (
    InvitationCreateInternal,
    InvitationCreateRequest,
    InvitationRead,
    InvitationReadInternal,
    InvitationRevokedResponse,
)
from ...schemas.invite_request import InviteRequestAccepted, InviteRequestSubmission
from ...services.email_service import send_invitation_email
from ...services.registration_gate import registration_is_invite_only

router = APIRouter(tags=["invitations"])

logger = logging.getLogger(__name__)

_NOT_FOUND = "Invitation not found"

# What every route here says on an `open`-mode instance. A 404 rather than a 403 or a 501
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

    **The answer is identical for every address**, first request or fiftieth, whether or
    not it already has an account, whether or not somebody invited it last week - and the
    guarantee is structural rather than a matter of shaping the response: this handler
    never queries the `user` table at all, exactly as `POST /auth/email/request` never
    does. An anonymous endpoint that answered differently would be an account-existence
    oracle for anybody who can reach the port, which is precisely what the sign-in flow
    goes to some length to avoid being.

    Two consequences the design accepts. A row may be stored for an address that already
    has an account - the operator's queue carries a `has_account` flag so they can see it
    and remove it. And a repeat submission stores nothing, because the insert is
    `ON CONFLICT DO NOTHING`; the audit row is still written, because the IP and User-Agent
    on it are what bound abuse here.

    404 on an `open`-mode instance: there is nothing to request an invitation to.
    """
    _require_invite_mode()

    email = body.email.lower()

    # The mode check is above these deliberately, the way `send_contact_message` puts its
    # 503 above its own: an instance where this feature is off must not have its buckets
    # spent by traffic that was never going to be stored.
    await enforce_rate_limit(
        f"invite-request:email:{email}",
        settings.INVITE_REQUEST_RATE_LIMIT_PER_EMAIL,
        settings.INVITE_REQUEST_RATE_LIMIT_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"invite-request:ip:{client_ip(request)}",
        settings.INVITE_REQUEST_RATE_LIMIT_PER_IP,
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


@router.get("/user/invitations", response_model=PaginatedListResponse[InvitationRead])
async def read_invitations(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
) -> dict:
    """The caller's own invitations, newest first.

    Paginated rather than a bare list, unlike the sessions and passkeys lists beside it in
    the settings grid: those are bounded by a cap the app enforces, while this collection
    only grows - five a day, indefinitely - so it takes the pagination convention and its
    clamp.

    Not Redis-cached. Nothing embeds these rows, the list changes on the caller's own
    actions and on an invitee accepting, and an invitation shown as pending after it has
    been accepted is a wrong answer rather than a stale one.

    404 on an `open`-mode instance, which is what makes the web's card remove itself with
    no knowledge of the mode.
    """
    _require_invite_mode()

    page, items_per_page = clamp_pagination(page, items_per_page)

    # `schema_to_select` is the crud alias's own select schema, so the rows come back
    # carrying the internal ids; the public shape is built from them below rather than left
    # to `response_model` to strip. Explicit, because "the response model will drop it" is
    # exactly the reasoning that puts an internal id on the wire the day somebody adds a
    # route that reuses this helper without one.
    rows = cast(
        dict[str, Any],
        await crud_invitations.get_multi(
            db=db,
            offset=compute_offset(page, items_per_page),
            limit=items_per_page,
            schema_to_select=InvitationReadInternal,
            sort_columns=["created_at"],
            sort_orders=["desc"],
            user_id=current_user["id"],
        ),
    )
    rows["data"] = [
        InvitationRead(
            uuid=row["uuid"],
            email=row["email"],
            created_at=row["created_at"],
            accepted_at=row["accepted_at"],
            revoked_at=row["revoked_at"],
        ).model_dump()
        for row in rows["data"]
    ]
    return paginated_response(crud_data=rows, page=page, items_per_page=items_per_page)


@router.post("/user/invitations", response_model=InvitationRead, status_code=201)
async def create_invitation(
    request: Request,
    body: InvitationCreateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> InvitationRead:
    """Invite an address onto this instance.

    Each refusal is a distinct status so a client can show the message verbatim rather than
    guess at what happened:

    - **404** - `open` mode; the feature is not here.
    - **409** - the address already has an account. This does disclose, to a signed-in and
      throttled caller, that an address is registered. Accepted deliberately: the
      alternative is a `201` that creates nothing, and the inviter then looks for an
      invitation that is nowhere in their list. Inviting yourself lands here.
    - **409** - you already have a live, unaccepted invitation out to this address.
      Per-inviter, because the table is not unique on the address: somebody else may have
      invited them too, and each of you sees your own row.
    - **429** - either the throttle below or your quota. `INVITATIONS_PER_USER` per
      `INVITATIONS_WINDOW_DAYS`, counted over rows you created in the trailing window with
      revoked ones included, so the bound is on invitation emails you have caused to be
      sent. Superusers are exempt from the quota, not from the throttle.
    - **422** - a malformed address, from `EmailStr`.

    Creating an invitation **deletes any pending request row for the address**, in the same
    transaction as the insert: an address with a live invitation is no longer waiting in
    the operator's queue, and making that one commit is what keeps the two tables from
    disagreeing.

    A send that fails after the row commits answers 5xx and leaves the row - the same shape
    the magic-link path has, and the right one here: the address is admitted from that
    moment, so the invitation is real whether or not the mail arrived, and the inviter can
    tell them another way.
    """
    _require_invite_mode()

    email = body.email.lower()

    # **Above the existence check, and it is the quota that cannot do this job.** The 409
    # below is a distinguishable answer about whether an address is registered, and it
    # creates no invitation row - so the quota, which counts rows created, never charges for
    # it and a caller could walk a wordlist through this endpoint unbounded. `PATCH /user`
    # throttles its username-availability check for exactly this reason. Keyed per-user
    # rather than per-IP because the caller is authenticated, and applied to superusers too:
    # they are exempt from the quota, which bounds how many people they may invite, not from
    # the backstop against automated probing.
    await enforce_rate_limit(
        f"invitation-create:user:{current_user['id']}",
        settings.INVITATION_ATTEMPT_RATE_LIMIT_PER_USER,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    if await account_exists_for(db, email=email):
        # A raw `HTTPException`: `core/exceptions/http_exceptions.py` has no class for 409
        # that does not also compose its own message (`DuplicateValueException`), and these
        # two sentences are what the card shows.
        raise HTTPException(status_code=409, detail="That address already has an account on this instance.")

    if await live_invitation_from(db, email=email, user_id=current_user["id"]):
        raise HTTPException(status_code=409, detail="You have already invited that address.")

    if not current_user["is_superuser"]:
        sent = await invitations_created_since(
            db, user_id=current_user["id"], window_days=settings.INVITATIONS_WINDOW_DAYS
        )
        if sent >= settings.INVITATIONS_PER_USER:
            window = _window_phrase(settings.INVITATIONS_WINDOW_DAYS)
            raise HTTPException(
                status_code=429,
                detail=f"You can send {settings.INVITATIONS_PER_USER} invitations {window}. Try again later.",
            )

    created = await crud_invitations.create(
        db=db,
        object=InvitationCreateInternal(email=email, user_id=current_user["id"]),
        commit=False,
        schema_to_select=InvitationReadInternal,
        return_as_model=True,
    )
    await delete_invite_requests(db, emails=[email], commit=False)
    await record_auth_event(
        db,
        event_type=AuthEventType.INVITATION_CREATED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
        email=email,
        commit=False,
    )
    await db.commit()

    await send_invitation_email(email=email, inviter_name=current_user["name"])

    return InvitationRead(
        uuid=created.uuid,
        email=created.email,
        created_at=created.created_at,
        accepted_at=created.accepted_at,
        revoked_at=created.revoked_at,
    )


@router.delete("/user/invitation/{uuid}", response_model=InvitationRevokedResponse)
async def revoke_invitation(
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> InvitationRevokedResponse:
    """Withdraw an invitation you sent, so the address is no longer admitted.

    Stamps `revoked_at` rather than removing the row, which is what keeps the quota honest:
    it counts rows created in the window, so an invitation cannot be un-sent by revoking
    it. The row is deleted later by the retention sweep like any other unaccepted one.

    **409 for an invitation that has already been accepted.** The account exists; revoking
    would be a stamp asserting something untrue, and it would take away nothing - the
    address is admitted by having an account, not by the invitation. A second revoke of an
    already-revoked invitation succeeds and changes nothing, the same as revoking a session
    twice, because the row is not what is being removed.

    404 unless the caller owns it, exactly as for one that does not exist - the ownership
    contract every keyed route in this app honours.
    """
    _require_invite_mode()

    row = await fetch_owned_or_raise(
        db=db,
        crud=crud_invitations,
        uuid=uuid,
        current_user=current_user,
        schema=InvitationReadInternal,
        not_found_message=_NOT_FOUND,
    )

    if row.accepted_at is not None:
        raise HTTPException(
            status_code=409, detail="That invitation has already been accepted, so there is nothing to revoke."
        )

    if row.revoked_at is None:
        await db.execute(update(Invitation).where(Invitation.id == row.id).values(revoked_at=datetime.now(UTC)))
        await db.commit()

    return InvitationRevokedResponse()


def _window_phrase(days: int) -> str:
    """ "per day" reads better than "per 1 days" on the one refusal a diver actually sees."""
    return "per day" if days == 1 else f"every {days} days"
