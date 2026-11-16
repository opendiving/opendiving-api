"""Proposing to OpenDiving Commons, and the queue that answers it.

Only on the instance where the Commons is edited: everywhere else these routes are a 404, at
the routers, so a route added here cannot arrive without that gate. The queue's second gate is
the superuser's, on its router for the same reason, as `admin.py` does it.
"""

import uuid as uuid_pkg
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_superuser, get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.utils.pagination import ItemsPerPageQuery, PageQuery, clamp_pagination
from ...crud.crud_dive_site_proposals import get_proposals_page
from ...crud.crud_dive_sites import crud_dive_sites
from ...models.dive_site import DiveSite
from ...schemas.dive_site import DiveSiteReadInternal
from ...schemas.dive_site_history import AdminDiveSiteHistoryEntryRead
from ...schemas.dive_site_proposal import (
    AdminDiveSiteProposalRead,
    DiveSiteProposalAccept,
    DiveSiteProposalCreate,
    DiveSiteProposalDecline,
    DiveSiteProposalRead,
    DiveSiteProposalUpdate,
)
from ...services import commons_history, commons_proposals


def require_commons_primary() -> None:
    if not settings.COMMONS_PRIMARY:
        raise NotFoundException("Not found")


router = APIRouter(tags=["commons"], dependencies=[Depends(require_commons_primary)])
admin_router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_commons_primary), Depends(get_current_superuser)],
)


@router.post("/dive-site/{uuid}/proposals", response_model=DiveSiteProposalRead, status_code=201)
async def propose_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: DiveSiteProposalCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteProposalRead:
    """Propose the caller's own site for OpenDiving Commons, or a change to a Commons record,
    answering 201 with the proposal.

    Nothing changes until a reviewer accepts: everyone goes on reading the record as it is, and
    the proposer's read of the site carries `proposal`. The caller's own site is proposed as it
    stands, so `members` is not sent; it needs a pin, a 422 otherwise, and one sharing a registry
    entry with a Commons record is a 409. A change sends in `members` the shared members it wants
    different: one that changes nothing, clears the pin or takes from an OpenStreetMap or Wikidata
    seed the registry entry it was seeded from is a 422.

    `attestation` is the text the diver ticked, word for word, and any other text is a 422. It is
    kept with the proposal, with the time, and goes when the account does.

    `show_proposer`, false unless sent, shows the diver's display name to every account here beside
    what the proposal changes in the site's history once it is accepted. Who proposed is otherwise
    read by the reviewer alone.

    One proposal per account and site waits at a time, and proposing again replaces it. Proposals
    are limited per account per day: 429 past it. 404 on another user's private site, exactly as
    for a site that doesn't exist, on the uuid of a site the caller replaced with a Commons record,
    and on an instance that does not edit the Commons.
    """
    site = await fetch_owned_or_raise(
        db=db,
        crud=crud_dive_sites,
        uuid=uuid,
        current_user=current_user,
        schema=DiveSiteReadInternal,
        not_found_message="Dive site not found",
        unowned_is_shared=True,
    )
    return await commons_proposals.propose(db, site_id=site.id, user_id=current_user["id"], values=values)


@router.get("/dive-site-proposals", response_model=PaginatedListResponse[DiveSiteProposalRead])
async def read_dive_site_proposals(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 10,
) -> dict:
    """The caller's own proposals, newest first, each with its status, any reason it was declined
    and the site it is on: for an accepted one, the Commons record it became or joined.
    Out-of-range pagination is clamped. 404 on an instance that does not edit the Commons.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)
    data = await get_proposals_page(
        db, user_id=current_user["id"], offset=compute_offset(page, items_per_page), limit=items_per_page
    )
    response: dict = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.patch("/dive-site-proposal/{uuid}", response_model=DiveSiteProposalRead)
async def update_dive_site_proposal(
    uuid: uuid_pkg.UUID,
    values: DiveSiteProposalUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteProposalRead:
    """Turn on or off whether other divers read the caller's display name beside what one of their
    proposals changes in the site's history, answering the proposal.

    It takes effect on the next read of the history, and on an accepted proposal turns the name on
    or off where it already shows. 409 on a declined or withdrawn proposal, which makes no entry.
    404 on another user's proposal, exactly as for one that doesn't exist, and on an instance that
    does not edit the Commons.
    """
    return await commons_proposals.choose(db, uuid=uuid, user_id=current_user["id"], values=values)


@router.delete("/dive-site-proposal/{uuid}")
async def withdraw_dive_site_proposal(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Withdraw one of the caller's proposals while it waits. It stays on their list as
    `withdrawn`.

    409 on one already decided or withdrawn. 404 on another user's proposal, exactly as for one
    that doesn't exist, and on an instance that does not edit the Commons.
    """
    await commons_proposals.withdraw(db, uuid=uuid, user_id=current_user["id"])
    return {"message": "Proposal withdrawn"}


@admin_router.get("/dive-site-proposals", response_model=PaginatedListResponse[AdminDiveSiteProposalRead])
async def read_dive_site_proposal_queue(
    db: Annotated[AsyncSession, Depends(async_get_db)], page: PageQuery = 1, items_per_page: ItemsPerPageQuery = 10
) -> dict:
    """The queue: every waiting proposal, oldest first. Superuser only.

    Each row carries the proposal and the proposer's message, their name with their counts of
    accepted and declined proposals, the site's shared members as they stand in `current`, as
    proposed in `proposed`, the members the proposer named in `changed`, and in `matches` the
    Commons records the proposed site may be. Of a private site a row carries only what a Commons
    record would. A proposal whose account is being deleted is left out, and comes back if the
    account is restored. Out-of-range pagination is clamped. 404 on an instance that does not edit the
    Commons.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)
    data = await commons_proposals.read_queue(db, page=page, items_per_page=items_per_page)
    response: dict = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@admin_router.post("/dive-site-proposal/{uuid}/accept", response_model=DiveSiteProposalRead)
async def accept_dive_site_proposal(
    uuid: uuid_pkg.UUID,
    values: DiveSiteProposalAccept,
    current_user: Annotated[dict, Depends(get_current_superuser)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteProposalRead:
    """Accept a waiting proposal, answering it with the record it now names. Superuser only.

    `members` are the record's final shared members, every one of them, and they are written
    exactly as sent, whatever the diver edited meanwhile, with the pin rounded to six decimals.
    For a new site the row then belongs to the Commons, under CC0, with the same uuid, and the
    proposer keeps their notes, tags and dives on it.

    `into` instead folds a new site into the Commons record it duplicates: the proposer's dives,
    notes and tags move to that record and their site is deleted. `members` may then carry
    changes to the record.

    The record's history gains the diver's change as they asked it and, where `members` differ
    from that, the reviewer's correction above it, both keeping who accepted. A fold gains only
    the correction, where `members` change the record.

    A body breaking a record rule is a 422 naming it: a pin at 0, 0 or none at all, a registry
    entry another Commons record carries, an `opendivingcommons` entry naming a record that
    exists, an OpenStreetMap or Wikidata seed losing the entry it was seeded from, an entry under
    the registry of a curated dataset, whose identifier stays in a record's provenance, or a record
    that is not valid DiveJSON.
    409 on one already decided or withdrawn; 404 on no such proposal, on one whose account is being
    deleted, and on an instance that does not edit the Commons.
    """
    return await commons_proposals.accept(db, uuid=uuid, values=values, reviewer_id=current_user["id"])


@admin_router.post("/dive-site-proposal/{uuid}/decline", response_model=DiveSiteProposalRead)
async def decline_dive_site_proposal(
    uuid: uuid_pkg.UUID,
    values: DiveSiteProposalDecline,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteProposalRead:
    """Decline a waiting proposal with a reason, which the proposer reads on the site and in their
    list until they propose again. Superuser only.

    The site and the record are unchanged. 409 on one already decided or withdrawn; 404 on no such
    proposal, on one whose account is being deleted, and on an instance that does not edit the
    Commons.
    """
    return await commons_proposals.decline(db, uuid=uuid, reason=values.reason)


@admin_router.get("/dive-site/{uuid}/history", response_model=PaginatedListResponse[AdminDiveSiteHistoryEntryRead])
async def read_dive_site_history_for_review(
    uuid: uuid_pkg.UUID,
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 10,
) -> dict:
    """A site's history as a reviewer reads it, newest first. Superuser only.

    The entries `GET /dive-site/{uuid}/history` answers, with the proposer's name in `made_by` on a
    diver's entry whatever they chose. A diver's entry also carries `show_proposer`, their choice,
    null once the proposal has gone with their account; `reviewed_by`, who accepted it; and
    `corrected`, whether that review also wrote a correction. On a reviewer's correction all
    three are null. A name is null once its account is deleted or asked to be. Out-of-range
    pagination is clamped. 404 on no such site and on an instance that does not edit the Commons.
    """
    site_id = await db.scalar(select(DiveSite.id).where(DiveSite.uuid == uuid))
    if site_id is None:
        raise NotFoundException("Dive site not found")
    page, items_per_page = clamp_pagination(page, items_per_page)
    data = await commons_history.read_history(
        db, site_id=site_id, offset=compute_offset(page, items_per_page), limit=items_per_page, as_reviewer=True
    )
    response: dict = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response
