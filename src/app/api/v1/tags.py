"""The diver's tags: `GET /tags`, `PATCH /tag/{uuid}` and `DELETE /tag/{uuid}`.

No create route: a dive write names its tags and creates the ones the diver lacks. The list
is not cached, for the people list's reason - one query over tens of rows. A dive read carries
its tags' names, so a rename and a delete drop the diver's dive caches.
"""

import uuid as uuid_pkg
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_tags import crud_tags, get_tags_page, tag_name_exists
from ...schemas.tag import TAG_NAME_MAX, TagRead, TagReadInternal, TagUpdate
from ...services.cache_invalidation import invalidate_dive_caches

router = APIRouter(tags=["tags"])

_NAME_TAKEN = "A tag with this name already exists"


async def _get_owned_tag(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> TagReadInternal:
    """Fetch a tag by public uuid and assert the caller owns it - see `fetch_owned_or_raise`
    for why someone else's row reads as a 404."""
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_tags,
        uuid=uuid,
        current_user=current_user,
        schema=TagReadInternal,
        not_found_message="Tag not found",
    )


@router.get("/tags", response_model=PaginatedListResponse[TagRead])
async def read_tags(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    search: Annotated[
        str | None, Query(max_length=TAG_NAME_MAX, description="Case-insensitive substring match on the name")
    ] = None,
) -> dict[str, Any]:
    """List the caller's tags by name, each with how many live dives carry it - zero for a
    tag no dive carries any more, which stays until it is deleted.

    `search` narrows the list as a picker is typed into. Out-of-range pagination is clamped
    rather than rejected.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    data = await get_tags_page(
        db,
        user_id=current_user["id"],
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        search=(search or "").strip() or None,
    )
    data["data"] = [tag.model_dump() for tag in data["data"]]
    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.patch("/tag/{uuid}")
async def patch_tag(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: TagUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Rename a tag; every dive carrying it carries the new name.

    404 unless the caller owns it. The name is trimmed, and one another of the caller's tags
    has once both are case-folded - `Night` beside `night`, `GROSSES RIFF` beside `Großes
    Riff` - is a 422. A change of case alone is a rename like any other.
    """
    db_tag = await _get_owned_tag(db, uuid, current_user)

    if values.name is None:
        return {"message": "Tag updated"}

    if await tag_name_exists(db, user_id=db_tag.user_id, name=values.name, exclude_id=db_tag.id):
        raise DuplicateValueException(_NAME_TAKEN)

    try:
        await crud_tags.update(db=db, object={"name": values.name}, uuid=uuid)
    except IntegrityError as e:
        # A concurrent rename or dive write took the name between the check and the write.
        await db.rollback()
        raise DuplicateValueException(_NAME_TAKEN) from e

    await invalidate_dive_caches(db_tag.user_id)
    return {"message": "Tag updated"}


@router.delete("/tag/{uuid}")
async def erase_tag(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Delete a tag. It leaves every dive that carried it; nothing else about them changes.

    404 unless the caller owns it, and a second `DELETE` is a 404 too. There is no way back.
    """
    db_tag = await _get_owned_tag(db, uuid, current_user)

    await crud_tags.delete(db=db, uuid=uuid)

    await invalidate_dive_caches(db_tag.user_id)
    return {"message": "Tag deleted"}
