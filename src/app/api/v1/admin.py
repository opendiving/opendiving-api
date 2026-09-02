"""The operator's contract: the invite queue, and inviting from it in a batch.

**The first routes in `/api/v1` to be superuser-gated.** `is_superuser` has existed on
`User` since the beginning and until now gated exactly one thing - the docs router on
`staging` (`core/setup.py`). These three are the second, and they are gated by a
**router-level** dependency rather than per handler, so the route-walking auth guard
(`tests/helpers/routes.py`, which reads include-level dependencies through the merged
dependant) sees the marker on every route here without each one naming it, and a fourth
route added to this module cannot arrive unprotected.

**Not the CRUDAdmin panel, and not a page.** These are ordinary JSON routes; the UI that
drives them is a superuser-gated section of the web app. The deciding argument is auth:
this app has no passwords, the access token is a bearer header held in browser memory and
the refresh cookie is single-use and `SameSite=lax`, so a browser *navigating* to a
server-rendered admin page on this origin carries no credential this app recognises. That
leaves an API-side admin either keeping a second identity with its own password, which is
what CRUDAdmin is, or building a cookie-to-page auth bridge nobody else ships.

The web gate is a convenience over this one and never a substitute: a non-superuser is
refused here whatever page they came from.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_superuser
from ...core.db.database import async_get_db
from ...core.utils.pagination import clamp_pagination
from ...core.utils.request_context import RequestContext
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_invitations import account_exists_for, crud_invitations, live_invitation_from
from ...crud.crud_invite_requests import delete_invite_requests
from ...models.invite_request import InviteRequest
from ...models.user import User
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.invitation import (
    AdminInvitationBatchRequest,
    AdminInvitationBatchResponse,
    AdminInvitationOutcome,
    InvitationCreateInternal,
    InvitationReadInternal,
)
from ...schemas.invite_request import (
    AdminInviteRequestDeleteRequest,
    AdminInviteRequestDeleteResponse,
    AdminInviteRequestRead,
)
from ...services.email_service import send_invitation_email

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(get_current_superuser)])

logger = logging.getLogger(__name__)


@router.get("/invite-requests", response_model=PaginatedListResponse[AdminInviteRequestRead])
async def read_invite_requests(
    db: Annotated[AsyncSession, Depends(async_get_db)], page: int = 1, items_per_page: int = 10
) -> dict:
    """The queue: every pending invite request, newest first.

    Every row is pending - a request that was invited, declined or swept is gone rather
    than flagged - so there is no filter and no status column to read.

    `has_account` is a `LEFT JOIN` on `lower(User.email)`, and both halves of that matter.
    *This* route may consult the `user` table where `POST /invite-requests` structurally
    may not, because its caller is the operator rather than an anonymous stranger; and the
    comparison is on the lowered column because `POST /auth/complete` stores the onboarding
    token's address verbatim, so an account created through Google may hold capitals while
    every row in this table is lowercase.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    account_exists = (
        select(func.count()).select_from(User).where(func.lower(User.email) == InviteRequest.email).scalar_subquery()
    )
    rows = (
        await db.execute(
            select(InviteRequest.email, InviteRequest.created_at, (account_exists > 0).label("has_account"))
            .order_by(InviteRequest.created_at.desc())
            .offset(compute_offset(page, items_per_page))
            .limit(items_per_page)
        )
    ).all()
    total = await db.scalar(select(func.count()).select_from(InviteRequest))

    data: dict[str, Any] = {
        "data": [
            AdminInviteRequestRead(
                email=row.email, created_at=row.created_at, has_account=row.has_account
            ).model_dump()
            for row in rows
        ],
        "total_count": int(total or 0),
    }
    return paginated_response(crud_data=data, page=page, items_per_page=items_per_page)


@router.post("/invitations", response_model=AdminInvitationBatchResponse)
async def invite_batch(
    request: Request,
    body: AdminInvitationBatchRequest,
    current_user: Annotated[dict, Depends(get_current_superuser)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AdminInvitationBatchResponse:
    """Invite a batch of addresses, and report what happened to each of them.

    Addresses that never requested are accepted too: the route takes addresses, and a
    pending request row is optional context rather than a precondition - the operator
    inviting a colleague is the same act as inviting somebody off the queue.

    **A per-address outcome rather than a 5xx on the first problem.** `invited`,
    `already_registered`, `already_invited`, `mail_failed`. A partial SMTP failure is then
    visible as *which* addresses got through, instead of an error that hides them; and the
    `mail_failed` rows are real invitations whose invitees simply have not been told, which
    is a thing the operator can act on.

    Sends are sequential and inline. The worker runs crons only (`DECISIONS.md` §"The Arq
    worker now does one real thing"), and a one-off operator action of at most
    `MAX_ADDRESSES_PER_BATCH` addresses does not justify this app's first queued job.

    No quota check: superusers are exempt from `INVITATIONS_PER_USER`, and this route
    cannot be reached by anybody else.
    """
    context = RequestContext.from_request(request)
    results: list[AdminInvitationOutcome] = []

    for raw in body.emails:
        email = raw.lower()

        if await account_exists_for(db, email=email):
            results.append(AdminInvitationOutcome(email=email, outcome="already_registered"))
            continue
        if await live_invitation_from(db, email=email, user_id=current_user["id"]):
            results.append(AdminInvitationOutcome(email=email, outcome="already_invited"))
            continue

        await crud_invitations.create(
            db=db,
            object=InvitationCreateInternal(email=email, user_id=current_user["id"]),
            commit=False,
            schema_to_select=InvitationReadInternal,
            return_as_model=True,
        )
        # Same transaction as the insert, for the same reason the member's route does it:
        # an address with a live invitation must not also be sitting in this queue.
        await delete_invite_requests(db, emails=[email], commit=False)
        await record_auth_event(
            db,
            event_type=AuthEventType.INVITATION_CREATED,
            context=context,
            user_id=current_user["id"],
            email=email,
            commit=False,
        )
        await db.commit()

        try:
            await send_invitation_email(email=email, inviter_name=current_user["name"])
        except Exception:
            # After the commit, so the invitation stands and the address is admitted. The
            # broad catch is the point of the per-address report: one unreachable relay or
            # one rejected recipient must not take the rest of the batch with it, and the
            # operator needs to see which ones to follow up by hand.
            logger.exception("Invitation for %s was created but could not be emailed", email)
            results.append(AdminInvitationOutcome(email=email, outcome="mail_failed"))
            continue

        results.append(AdminInvitationOutcome(email=email, outcome="invited"))

    return AdminInvitationBatchResponse(results=results)


@router.delete("/invite-requests", response_model=AdminInviteRequestDeleteResponse)
async def remove_invite_requests(
    body: AdminInviteRequestDeleteRequest, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> AdminInviteRequestDeleteResponse:
    """Drop addresses from the queue - spam, or a request the operator declines.

    Body-keyed rather than a `{uuid}` route because an `invite_request` row has no public
    identifier: the address is the only thing anybody knows about a request, which is also
    why the model carries no `uuid` at all.

    Nothing is written anywhere else. A declined request is not a refusal the address can
    be told about, and there is no state to keep - the person may ask again, and the rate
    limits on `POST /invite-requests` are what bound that.

    The count is what actually went, which may be fewer than were asked for: the sweep or
    an invitation may have taken a row between the operator reading the queue and acting on
    it.
    """
    removed = await delete_invite_requests(db, emails=[email.lower() for email in body.emails])
    return AdminInviteRequestDeleteResponse(removed=removed)
