import uuid as uuid_pkg
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_dive_drafts import crud_dive_drafts
from ...models.dive_draft import DiveDraft
from ...schemas.dive_draft import (
    DIVE_DRAFT_NOT_FOUND,
    DiveDraftCreate,
    DiveDraftRead,
    DiveDraftReadInternal,
    DiveDraftUpdate,
)
from ...services.dive_drafts import create_dive_draft, read_dive_draft, read_dive_drafts_page, update_dive_draft
from ..idempotency import IdempotentCreate

# Uncached, for the reason `OwnedResourceCache`'s docstring gives under `dive_drafts.py`.
router = APIRouter(tags=["dive-drafts"])


async def _get_owned_dive_draft(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> DiveDraftReadInternal:
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_dive_drafts,
        uuid=uuid,
        current_user=current_user,
        schema=DiveDraftReadInternal,
        not_found_message=DIVE_DRAFT_NOT_FOUND,
    )


@router.post(
    "/dive-draft",
    response_model=DiveDraftRead,
    status_code=201,
    dependencies=[Depends(IdempotentCreate(DiveDraft, read="/dive-draft/{uuid}"))],
)
async def write_dive_draft(
    request: Request,
    draft: DiveDraftCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveDraftRead:
    """Save a dive not yet logged: the dive form's values, with nothing required.

    `values` takes `POST /dive`'s members, every one optional, with their types and
    vocabularies and none of a dive's bounds or rules between members - a pressure of 400 bar
    or an average deeper than the maximum is saved as typed, and logging the draft is where
    the dive is validated. A wrong type or a word outside a vocabulary is a 422. References
    are stored as given and read back only while they name a record of the caller's (or, for
    a species, one that exists); a `gear_set_uuid` that is not the caller's is stored as none.
    """
    draft_id = await create_dive_draft(db, draft, user_id=current_user["id"])
    await db.commit()
    return await read_dive_draft(db, draft_id, user_id=current_user["id"])


@router.get("/dive-drafts", response_model=PaginatedListResponse[DiveDraftRead])
async def read_dive_drafts(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
) -> dict:
    """List the caller's dive drafts, the most recently changed first.

    Out-of-range pagination is clamped, not rejected.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)
    data = await read_dive_drafts_page(
        db, user_id=current_user["id"], offset=compute_offset(page, items_per_page), limit=items_per_page
    )
    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get("/dive-draft/{uuid}", response_model=DiveDraftRead)
async def read_one_dive_draft(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveDraftRead:
    """Return a single dive draft.

    404 when no such draft exists - and the same 404 when it belongs to another account.
    """
    draft = await _get_owned_dive_draft(db, uuid, current_user)
    return await read_dive_draft(db, draft.id, user_id=current_user["id"])


@router.patch("/dive-draft/{uuid}")
async def patch_dive_draft(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: DiveDraftUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Replace a draft's state with the form's.

    `values`, when present, replaces the stored values wholesale - a member it leaves out is
    dropped from the draft, so a field cleared on one device is cleared on the other - and
    `gear_set_uuid`, when present, sets or clears the loaded set. `values: null` is a 422.
    404 when the draft is not the caller's.
    """
    draft = await _get_owned_dive_draft(db, uuid, current_user)
    await update_dive_draft(db, draft.id, values, user_id=current_user["id"])
    await db.commit()
    return {"message": "Dive draft updated"}


@router.delete("/dive-draft/{uuid}")
async def erase_dive_draft(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Discard a dive draft. 404 unless the caller owns it."""
    draft = await _get_owned_dive_draft(db, uuid, current_user)
    await db.execute(delete(DiveDraft).where(DiveDraft.id == draft.id))
    await db.commit()
    return {"message": "Dive draft deleted"}
