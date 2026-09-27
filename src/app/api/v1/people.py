"""The people a diver was with: `POST /person`, `GET /people` and the single-person routes.

Neither read is cached. A person read carries the linked account's current username and a
dive count, and both change on writes this diver never makes - the linked account renaming
or leaving - so a per-owner cache would serve a stale answer for its TTL, and nothing reaches
another account's keys. The list is one query over tens of rows. What a person's *delete*
changes is its hosts' cached reads, and those are dropped here.
"""

import uuid as uuid_pkg
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, NotFoundException
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_people import (
    crud_people,
    get_people_page,
    get_person_read,
    get_trip_uuids_with_person,
    person_name_exists,
)
from ...models.user import User
from ...schemas.person import (
    PersonCreate,
    PersonCreateInternal,
    PersonRead,
    PersonReadInternal,
    PersonUpdateRequest,
)
from ...services.cache_invalidation import (
    invalidate_certification_caches,
    invalidate_course_caches,
    invalidate_dive_caches,
    invalidate_trip_caches,
    invalidate_trip_items,
)
from ...services.person_links import resolve_linked_account, spend_link_attempt

router = APIRouter(tags=["people"])

_NAME_TAKEN = "A person with this name already exists"


async def _get_owned_person(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> PersonReadInternal:
    """Fetch a person by public uuid and assert the caller owns it - see
    `fetch_owned_or_raise` for why someone else's row reads as a 404."""
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_people,
        uuid=uuid,
        current_user=current_user,
        schema=PersonReadInternal,
        not_found_message="Person not found",
    )


async def _read(db: AsyncSession, person_id: int) -> PersonRead:
    person = await get_person_read(db, person_id=person_id)
    if person is None:
        raise NotFoundException("Person not found")
    return person


@router.post("/person", response_model=PersonRead, status_code=201)
async def write_person(
    request: Request,
    person: PersonCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> PersonRead:
    """Create a person - a buddy, a guide, an instructor, a companion - for the caller.

    Names are trimmed and unique per diver, case-insensitively, so a second "Alex" is a 422.
    `username` links the person to the account on this instance with exactly that username;
    the link shows you that account's current username and nothing else of it, and tells
    that account nothing. No such account, your own, or one another of your people already
    links is a 422 on the field. Linking is rate limited per user, like choosing a username.
    """
    if await person_name_exists(db=db, user_id=current_user["id"], name=person.name):
        raise DuplicateValueException(_NAME_TAKEN)

    linked_user_id: int | None = None
    if person.username is not None:
        await spend_link_attempt(current_user["id"])
        linked_user_id = await resolve_linked_account(db, owner_id=current_user["id"], username=person.username)

    created = await crud_people.create(
        db=db,
        object=PersonCreateInternal(
            **person.model_dump(exclude={"username"}), user_id=current_user["id"], linked_user_id=linked_user_id
        ),
        schema_to_select=PersonReadInternal,
        return_as_model=True,
    )
    return await _read(db, created.id)


@router.get("/people", response_model=PaginatedListResponse[PersonRead])
async def read_people(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive substring match on the name or the linked username"),
    ] = None,
) -> dict[str, Any]:
    """List the caller's people by name, each with its linked account's current username and
    how many live dives name it.

    `search` narrows the list as a picker is typed into, matching the name and the linked
    username. Out-of-range pagination is clamped rather than rejected.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    data = await get_people_page(
        db,
        user_id=current_user["id"],
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        search=(search or "").strip().lower() or None,
    )
    data["data"] = [person.model_dump() for person in data["data"]]
    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get("/person/{uuid}", response_model=PersonRead)
async def read_person(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> PersonRead:
    """Return one of the caller's people. 404 when it doesn't exist - and the same 404 when
    it belongs to another user."""
    person = await _get_owned_person(db, uuid, current_user)
    return await _read(db, person.id)


@router.patch("/person/{uuid}")
async def patch_person(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: PersonUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update a person; omitted fields are left untouched.

    404 unless the caller owns it. Renaming onto another of the caller's people is a 422. An
    explicit `null` clears the email or the phone, and `null` for `username` unlinks the
    person. A `username` naming a different account than the linked one is refused as on
    create, and counts against the same limit; the one already linked changes nothing.
    """
    db_person = await _get_owned_person(db, uuid, current_user)

    if values.name is not None and await person_name_exists(
        db=db, user_id=db_person.user_id, name=values.name, exclude_id=db_person.id
    ):
        raise DuplicateValueException(_NAME_TAKEN)

    update_data = values.model_dump(exclude={"username"}, exclude_unset=True)
    if "username" in values.model_fields_set:
        if values.username is None:
            update_data["linked_user_id"] = None
        else:
            current = (
                None
                if db_person.linked_user_id is None
                else await db.scalar(select(User.username).where(User.id == db_person.linked_user_id))
            )
            if values.username != current:
                await spend_link_attempt(db_person.user_id)
                update_data["linked_user_id"] = await resolve_linked_account(
                    db, owner_id=db_person.user_id, username=values.username, person_id=db_person.id
                )

    if update_data:
        await crud_people.update(db=db, object=update_data, uuid=uuid)

    return {"message": "Person updated"}


@router.delete("/person/{uuid}")
async def erase_person(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Delete a person. It leaves every dive, trip and course it was on, and every card it
    signed stops naming an instructor; nothing else about them changes.

    404 unless the caller owns it, and a second `DELETE` is a 404 too. There is no way back.
    """
    db_person = await _get_owned_person(db, uuid, current_user)
    owner_id = db_person.user_id
    # Before the row goes: a single trip's cache key carries no user for a pattern to find.
    trips_on = await get_trip_uuids_with_person(db, db_person.id)

    await crud_people.delete(db=db, uuid=uuid)

    # Every host read that carried this person's uuid now reads without it.
    await invalidate_dive_caches(owner_id)
    await invalidate_trip_caches(owner_id)
    await invalidate_trip_items(trips_on)
    await invalidate_course_caches(owner_id)
    await invalidate_certification_caches(owner_id)

    return {"message": "Person deleted"}
