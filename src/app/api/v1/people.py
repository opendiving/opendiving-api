"""The people a diver was with: `POST /person`, `GET /people`, `GET /people/lookup`, the
single-person routes, and *Added you* - the accounts that have the diver in their people.

No read is cached. A person read carries the connected account's current username, its
picture version and a dive count, and all three change on writes this diver never makes -
the connected account renaming, replacing its picture, asking to be deleted, or cutting the
tie from its *Added you* - so a per-owner cache would serve a stale answer for its TTL, and
nothing reaches another account's keys. *Added you* changes on other divers' writes alone.
The lists are one query over tens of rows. What a person's *delete* changes is its hosts'
cached reads, and those are dropped here.

The pictures are served by `api.account_avatar`, each route deciding whether the caller may
see that account's: while the tie stands, and never once it has asked to be deleted.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy import ColumnElement, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.account_avatar import serve_account_avatar
from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, NotFoundException
from ...core.utils.pagination import ItemsPerPageQuery, PageQuery, clamp_pagination
from ...crud.crud_lookups import get_lookup_page
from ...crud.crud_people import (
    PERSON_NAME_KEY,
    crud_people,
    get_added_you_page,
    get_people_page,
    get_person_read,
    person_name_exists,
    person_search_condition,
    tie_from,
)
from ...crud.unique_keys import write_or_duplicate
from ...models.account_disconnect import AccountDisconnect
from ...models.dive_person import DivePerson
from ...models.person import Person
from ...models.user import User
from ...schemas.lookup import LookupUntil, lookup_bound
from ...schemas.person import (
    AddedYouRow,
    PersonCreate,
    PersonCreateInternal,
    PersonLookupItem,
    PersonRead,
    PersonReadInternal,
    PersonUpdateRequest,
)
from ...services.cache_invalidation import (
    invalidate_certification_caches,
    invalidate_course_caches,
    invalidate_dive_caches,
    invalidate_trip_caches,
)
from ...services.person_connections import (
    forget_disconnect,
    refuse_a_second_tie,
    resolve_connection,
)
from ..idempotency import IdempotentCreate

router = APIRouter(tags=["people"])

_NAME_TAKEN = "A person with this name already exists"
_NO_TIE = "No account with that id has you in its people"
_VERSION_DESCRIPTION = "Opaque cache-busting version token; ignored by the server"


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


@router.post(
    "/person",
    response_model=PersonRead,
    status_code=201,
    dependencies=[Depends(IdempotentCreate(Person, read="/person/{uuid}"))],
)
async def write_person(
    request: Request,
    person: PersonCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> PersonRead:
    """Create a person - a buddy, a guide, an instructor, a companion - for the caller.

    Names are trimmed and unique per diver, case-insensitively, so a second "Alex" is a 422.
    `links` are the person's own pages, in the diver's order, at most 20, an address named
    twice kept once; each is an absolute http or https address with no whitespace.

    `connect_link`, a live connect link's token, or `connect_account`, the public id of an
    account that has a person connected to you, connects the new person to that account: you
    see its picture and current username on the person, and it sees your name, username and
    picture under its *Added you* until either of you disconnects. At most one of them. A link
    that is not live, an id naming no account with a person connected to you, your own
    account, and an account another of your people is connected to are each a 422 on the
    member.
    """
    connected_user_id = await resolve_connection(db, person, owner_id=current_user["id"])

    if await person_name_exists(db=db, user_id=current_user["id"], name=person.name):
        raise DuplicateValueException(_NAME_TAKEN)

    if connected_user_id is not None:
        await forget_disconnect(db, account_id=connected_user_id, diver_id=current_user["id"])
    created = await write_or_duplicate(
        db,
        crud_people.create(
            db=db,
            object=PersonCreateInternal(
                **person.model_dump(exclude={"connect_link", "connect_account"}),
                user_id=current_user["id"],
                connected_user_id=connected_user_id,
            ),
            schema_to_select=PersonReadInternal,
            return_as_model=True,
        ),
        PERSON_NAME_KEY,
        _NAME_TAKEN,
    )
    return await _read(db, created.id)


@router.get("/people", response_model=PaginatedListResponse[PersonRead])
async def read_people(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive substring match on the name or the connected username"),
    ] = None,
) -> dict[str, Any]:
    """List the caller's people by name, each with its connected account's current username
    and picture version, and how many live dives name it.

    `search` matches a case-insensitive substring of the name or the connected username.
    Out-of-range pagination is clamped, not rejected.
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


@router.get("/people/lookup", response_model=PaginatedListResponse[PersonLookupItem])
async def lookup_people(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    until: LookupUntil = None,
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive substring match on the name or the connected username"),
    ] = None,
) -> dict[str, Any]:
    """The caller's people as a picker lists them: uuid, name and the connected account's current
    username, the person on a dive at or before `until` most recently first, then people no
    such dive names, newest created first.

    `search` matches what `GET /people` matches. Out-of-range pagination is clamped.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    user_id = current_user["id"]
    term = (search or "").strip().lower()
    conditions: tuple[ColumnElement[bool], ...] = (Person.user_id == user_id,)
    if term:
        conditions += (person_search_condition(term),)
    data = await get_lookup_page(
        db,
        model=Person,
        columns=(Person.uuid, Person.name, User.username),
        conditions=conditions,
        used_by=DivePerson.person_id,
        user_id=user_id,
        bound=lookup_bound(until),
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        from_clause=Person.__table__.outerjoin(User.__table__, User.id == Person.connected_user_id),
    )
    data["data"] = [PersonLookupItem.model_validate(row).model_dump() for row in data["data"]]
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
    explicit `null` clears the email or the phone. `links` replaces the list whole.

    `connect_link` or `connect_account` connects a person who is not connected yet, as on
    create, and is refused alike; a person already connected is a 422 on the member.
    """
    db_person = await _get_owned_person(db, uuid, current_user)

    refuse_a_second_tie(values, db_person)
    connected_user_id = await resolve_connection(db, values, owner_id=db_person.user_id, person_id=db_person.id)

    if values.name is not None and await person_name_exists(
        db=db, user_id=db_person.user_id, name=values.name, exclude_id=db_person.id
    ):
        raise DuplicateValueException(_NAME_TAKEN)

    update_data = values.model_dump(exclude={"connect_link", "connect_account"}, exclude_unset=True)
    if connected_user_id is not None:
        await forget_disconnect(db, account_id=connected_user_id, diver_id=db_person.user_id)
        update_data |= {"connected_user_id": connected_user_id, "connection_hidden_at": None}

    if update_data:
        await write_or_duplicate(
            db, crud_people.update(db=db, object=update_data, uuid=uuid), PERSON_NAME_KEY, _NAME_TAKEN
        )

    return {"message": "Person updated"}


@router.post("/person/{uuid}/disconnect", response_model=PersonRead)
async def disconnect_person(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> PersonRead:
    """Disconnect one of the caller's people from the account it is connected to, keeping the
    person: it loses that account's picture and username, and the caller leaves the account's
    *Added you*. The account is not told.

    Answers the person, and the same for one that was not connected. 404 unless the caller
    owns it.
    """
    # Records no disconnect: this is the diver dropping their own tie, and an import of their
    # own file may make it again.
    db_person = await _get_owned_person(db, uuid, current_user)
    if db_person.connected_user_id is not None:
        await db.execute(
            update(Person)
            .where(Person.id == db_person.id)
            .values(connected_user_id=None, connection_hidden_at=None, updated_at=datetime.now(UTC))
        )
        await db.commit()
    return await _read(db, db_person.id)


@router.get("/person/{uuid}/avatar", responses={404: {"description": "Not connected, no picture, or not yours"}})
async def read_person_avatar(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[str | None, Query(description=_VERSION_DESCRIPTION)] = None,
) -> Response:
    """The picture of the account one of the caller's people is connected to: its rendition,
    one WebP.

    404 when the person is not the caller's, is not connected, or the account has no picture
    or has asked to be deleted. Never stored by a browser or a proxy
    (`Cache-Control: private, no-store`), with an `ETag`; `If-None-Match` answers 304. Pass
    `PersonRead.avatar_sha256` as `v`, which the server ignores.
    """
    db_person = await _get_owned_person(db, uuid, current_user)
    account_id = None
    if db_person.connected_user_id is not None and not await db.scalar(
        select(User.is_deleted).where(User.id == db_person.connected_user_id)
    ):
        account_id = db_person.connected_user_id
    return await serve_account_avatar(request, db, account_id=account_id)


@router.get("/people/added-you", response_model=PaginatedListResponse[AddedYouRow])
async def read_added_you(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 10,
) -> dict[str, Any]:
    """The accounts that have the caller in their people and that the caller has not added:
    each by its public id, current name, username and picture version, ordered by name.

    Leaves out an account the caller has a person connected to, one the caller has hidden,
    and one that has asked to be deleted. Each of these accounts sees the caller's name,
    username and picture. Out-of-range pagination is clamped.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)
    data = await get_added_you_page(
        db, user_id=current_user["id"], offset=compute_offset(page, items_per_page), limit=items_per_page
    )
    data["data"] = [row.model_dump() for row in data["data"]]
    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get(
    "/people/added-you/{uuid}/avatar", responses={404: {"description": "No tie to you, or no picture: one answer"}}
)
async def read_added_you_avatar(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[str | None, Query(description=_VERSION_DESCRIPTION)] = None,
) -> Response:
    """The picture of an account that has a person connected to the caller.

    404 when the account has no person connected to the caller, has no picture, or has asked
    to be deleted. Never stored by a browser or a proxy (`Cache-Control: private, no-store`),
    the 404 included, with an `ETag`; `If-None-Match` answers 304. Pass the row's
    `avatar_sha256` as `v`, which the server ignores.
    """
    tie = await tie_from(db, account_uuid=uuid, user_id=current_user["id"])
    account_id = None
    if tie is not None and not await db.scalar(select(User.is_deleted).where(User.id == tie[0])):
        account_id = tie[0]
    return await serve_account_avatar(request, db, account_id=account_id)


@router.post("/people/added-you/{uuid}/hide")
async def hide_added_you(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Hide an account from the caller's *Added you*. Nothing else changes: the account keeps
    its person connected to the caller, with the caller's picture.

    The account comes back only when its tie is cut and made again. A second hide succeeds and
    changes nothing. 404 when the account has no person connected to the caller.
    """
    tie = await tie_from(db, account_uuid=uuid, user_id=current_user["id"])
    if tie is None:
        raise NotFoundException(_NO_TIE)
    await db.execute(
        update(Person)
        .where(Person.id == tie[1], Person.connection_hidden_at.is_(None))
        .values(connection_hidden_at=datetime.now(UTC))
    )
    await db.commit()
    return {"message": "Hidden"}


@router.post("/people/added-you/{uuid}/disconnect")
async def disconnect_added_you(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Cut an account's tie to the caller: its person stays, as a name with no picture and no
    username, and it is not told. It can connect again through the caller's connect link while
    that link stands; an import of a file naming the caller does not connect it again.

    404 when the account has no person connected to the caller.
    """
    # The record and the cut are one commit, so an import never sees one without the other.
    tie = await tie_from(db, account_uuid=uuid, user_id=current_user["id"])
    if tie is None:
        raise NotFoundException(_NO_TIE)
    account_id, person_id = tie
    await db.execute(
        update(Person)
        .where(Person.id == person_id)
        .values(connected_user_id=None, connection_hidden_at=None, updated_at=datetime.now(UTC))
    )
    await db.execute(
        insert(AccountDisconnect)
        .values(user_id=current_user["id"], disconnected_user_id=account_id, created_at=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=[AccountDisconnect.user_id, AccountDisconnect.disconnected_user_id])
    )
    await db.commit()
    return {"message": "Disconnected"}


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

    await crud_people.delete(db=db, uuid=uuid)

    # Every host read that carried this person's uuid now reads without it.
    await invalidate_dive_caches(owner_id)
    await invalidate_trip_caches(owner_id)
    await invalidate_course_caches(owner_id)
    await invalidate_certification_caches(owner_id)

    return {"message": "Person deleted"}
