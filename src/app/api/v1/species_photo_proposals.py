"""Proposing a Commons file as a species' photo, on every instance and from any account.

Proposing is open to any signed-in account, an API key included by the rule keys follow on every
route; deciding is the operator's, under the `/admin` router in `admin.py`, whose gate no key
passes. The rule decides most photos, and the tools that judge photographs run off the server, so
the instance takes the proposal and the operator's answer and does no judging of its own.
"""

import uuid as uuid_pkg
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.utils.pagination import ItemsPerPageQuery, PageQuery, clamp_pagination
from ...crud.crud_species_photo_proposals import get_proposals_page
from ...schemas.species_photo_proposal import (
    SpeciesPhotoProposalCreate,
    SpeciesPhotoProposalRead,
    SpeciesPhotoProposalStatus,
)
from ...services import species_photo_proposals

router = APIRouter(tags=["species"])


@router.post("/species/{uuid}/photo-proposals", response_model=SpeciesPhotoProposalRead, status_code=201)
async def propose_species_photo(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: SpeciesPhotoProposalCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> SpeciesPhotoProposalRead:
    """Propose a Wikimedia Commons file as a better photo for this species, answering 201 with
    the proposal, waiting.

    Nothing changes until whoever runs this instance accepts it; they see the file, your `note`
    and your name. `file` is a file title, with or without `File:`, or its
    `commons.wikimedia.org/wiki/File:` page URL. It is checked for shape only: Wikimedia is asked
    nothing until the proposal is reviewed, and a title that names no raster file is a 422.

    One proposal per account and species waits at a time, and proposing again replaces it with a
    new proposal under a new `uuid`, behind the others waiting for review; the one it replaced is
    gone, and its `uuid` is a 404 from then on.

    409 when the species' photo was already chosen by hand - `accepts_photo_proposals` on the
    species reads `false` - when the file is the species' photo already, and when the file was
    declined for this species before, whoever proposed it. Proposals are limited per account
    per day: 429 past it. 404 for an unknown species.
    """
    return await species_photo_proposals.propose(db, species_uuid=uuid, user_id=current_user["id"], values=values)


@router.get("/species-photo-proposals", response_model=PaginatedListResponse[SpeciesPhotoProposalRead])
async def read_species_photo_proposals(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 10,
    status: Annotated[SpeciesPhotoProposalStatus | None, Query(description="Only the proposals in this status")] = None,
) -> dict:
    """The caller's own species photo proposals, newest first, each with its species, file, note,
    status, any reason it was declined, and when it was made and decided.

    `closed` means the species' photo was chosen another way - a different file pinned, or the
    photo hidden - without a ruling on this file, which may be proposed again once the species
    accepts proposals. Out-of-range pagination is clamped.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)
    data = await get_proposals_page(
        db, user_id=current_user["id"], status=status, offset=compute_offset(page, items_per_page), limit=items_per_page
    )
    response: dict = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.delete("/species-photo-proposal/{uuid}")
async def withdraw_species_photo_proposal(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Withdraw one of the caller's species photo proposals while it waits. It stays on their
    list as `withdrawn`.

    409 on one already decided, withdrawn or closed. 404 on another account's proposal, exactly as
    for one that doesn't exist.
    """
    await species_photo_proposals.withdraw(db, uuid=uuid, user_id=current_user["id"])
    return {"message": "Proposal withdrawn"}
