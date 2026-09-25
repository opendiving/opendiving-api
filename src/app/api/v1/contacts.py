import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, NotFoundException
from ...core.utils.cache import cache
from ...core.utils.owned_resource_cache import OwnedResourceCache
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_contacts import CONTACT_SEARCH_COLUMNS, contact_name_exists, crud_contacts
from ...crud.crud_trip_parts import get_trip_uuids_staying_at
from ...schemas.contact import (
    ADDRESS_FIELDS,
    CONTACT_ADDRESS_PREFIX,
    ContactCreate,
    ContactCreateInternal,
    ContactRead,
    ContactReadInternal,
    ContactUpdateRequest,
    address_columns,
    address_from_row,
)
from ...services.cache_invalidation import (
    invalidate_certification_caches,
    invalidate_contact_caches,
    invalidate_course_caches,
    invalidate_dive_caches,
    invalidate_gear_caches,
    invalidate_trip_caches,
    invalidate_trip_items,
)

router = APIRouter(tags=["contacts"])

_ADDRESS_COLUMNS = frozenset(f"{CONTACT_ADDRESS_PREFIX}{field}" for field in ADDRESS_FIELDS)


def _to_public_contact(db_contact: ContactReadInternal | dict[str, Any], *, user_uuid: uuid_pkg.UUID) -> ContactRead:
    """Convert an internal contact representation (integer FKs, flat address) into its
    public shape (owning user referenced by `uuid`, address nested)."""
    data = db_contact if isinstance(db_contact, dict) else db_contact.model_dump()
    return ContactRead(
        **{k: v for k, v in data.items() if k not in {"id", "user_id"} | _ADDRESS_COLUMNS},
        address=address_from_row(data),
        user_uuid=user_uuid,
    )


# Serves the list through `read_list`. The single read is hand-written below instead of
# going through `read_item`, whose key cannot carry the user: every contact key sits under
# `user_{id}_contact` so one pattern drops both, as courses do.
_contact_cache: OwnedResourceCache[ContactReadInternal, ContactRead] = OwnedResourceCache(
    resource_name="contacts",
    resource_label="Contact",
    item_cache_prefix="user_{user_id}_contact",
    crud=crud_contacts,
    schema_to_select=ContactReadInternal,
    to_public=lambda db_contact, user_uuid: _to_public_contact(db_contact, user_uuid=user_uuid),
    sort_columns="name",
    sort_orders="asc",
    search_columns=CONTACT_SEARCH_COLUMNS,
)


async def _get_owned_contact(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> ContactReadInternal:
    """Fetch a contact by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for why someone else's row reads
    as a 404 and, in particular, why this must run before any `@cache`-wrapped read helper.
    """
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_contacts,
        uuid=uuid,
        current_user=current_user,
        schema=ContactReadInternal,
        not_found_message="Contact not found",
    )


@router.post("/contact", response_model=ContactRead, status_code=201)
async def write_contact(
    request: Request,
    contact: ContactCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> ContactRead:
    """Create a contact - a dive center, a school, a shop, a place stayed at - for the
    authenticated user.

    Names are unique per user, case-insensitively, so a second "Blue Ocean" is a 422.
    `roles` is a set in any order and comes back in vocabulary order with duplicates
    folded; it may be empty. `address` is optional, and when present needs its `country`.
    """
    if await contact_name_exists(db=db, user_id=current_user["id"], name=contact.name):
        raise DuplicateValueException("A contact with this name already exists")

    contact_internal = ContactCreateInternal(
        **contact.model_dump(exclude={"address"}),
        **address_columns(contact.address),
        user_id=current_user["id"],
    )
    created = await crud_contacts.create(
        db=db, object=contact_internal, schema_to_select=ContactReadInternal, return_as_model=True
    )
    await invalidate_contact_caches(current_user["id"])

    return _to_public_contact(cast(ContactReadInternal, created), user_uuid=current_user["uuid"])


@router.get("/contacts", response_model=PaginatedListResponse[ContactRead])
async def read_contacts(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive substring match on the name or the address's city"),
    ] = None,
) -> dict:
    """List the caller's contacts by name.

    `search` narrows the list server-side as a picker is typed into, matching the name and
    the city. Out-of-range pagination is clamped rather than rejected.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    return await _contact_cache.read_list(
        request,
        user_id=current_user["id"],
        user_uuid=current_user["uuid"],
        db=db,
        page=page,
        items_per_page=items_per_page,
        # Normalized here rather than in the cache layer so " Blue " and "blue" share one
        # cache entry.
        search=(search or "").strip().lower() or None,
    )


@cache(key_prefix="user_{user_id}_contact", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_contact(
    request: Request, user_id: int, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> ContactRead:
    """Fetches (and caches) a single contact by uuid. Authorization is checked by the route
    before this is ever reached."""
    db_contact = await crud_contacts.get(db=db, uuid=uuid, schema_to_select=ContactReadInternal, return_as_model=True)
    if db_contact is None:
        raise NotFoundException("Contact not found")

    return _to_public_contact(cast(ContactReadInternal, db_contact), user_uuid=owner_uuid)


@router.get("/contact/{uuid}", response_model=ContactRead)
async def read_contact(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> ContactRead:
    """Return a single contact by its public uuid.

    404 when no such contact exists - and the same 404 when it belongs to another user, so
    someone else's uuid stays unprobeable.
    """
    # Authorize before the cached read: `@cache` replays a hit without re-checking.
    await _get_owned_contact(db, uuid, current_user)

    return await _cached_read_contact(
        request, user_id=current_user["id"], uuid=uuid, owner_uuid=current_user["uuid"], db=db
    )


@router.patch("/contact/{uuid}")
async def patch_contact(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: ContactUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update a contact; omitted fields are left untouched.

    404 unless the caller owns it. Renaming to a name the caller already has on another
    contact is a 422. An explicit `null` clears the phone, the email, the website or the
    address; naming `address` replaces the stored one whole. `roles` replaces the set.
    """
    db_contact = await _get_owned_contact(db, uuid, current_user)

    if values.name is not None and await contact_name_exists(
        db=db, user_id=db_contact.user_id, name=values.name, exclude_id=db_contact.id
    ):
        raise DuplicateValueException("A contact with this name already exists")

    update_data = values.model_dump(exclude_unset=True, exclude={"address"})
    if "address" in values.model_fields_set:
        update_data |= address_columns(values.address)

    if update_data:
        await crud_contacts.update(db=db, object=update_data, uuid=uuid)
        await invalidate_contact_caches(db_contact.user_id)

    return {"message": "Contact updated"}


@router.delete("/contact/{uuid}")
async def erase_contact(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Delete a contact. Everything that named it survives, unlinked.

    404 unless the caller owns it, and a second `DELETE` on the same uuid is a 404 too.
    The dives, courses, certifications, service records and trip parts that referenced it
    keep every other field: all five references are `ON DELETE SET NULL`, so the database
    unlinks them, and there is no way back after.
    """
    db_contact = await _get_owned_contact(db, uuid, current_user)
    owner_id = db_contact.user_id
    # Before the row goes: afterwards no part names it, and a single trip's cache key
    # carries no user for a pattern to find.
    stayed_on = await get_trip_uuids_staying_at(db=db, contact_id=db_contact.id)

    await crud_contacts.delete(db=db, uuid=uuid)

    await invalidate_contact_caches(owner_id)
    # Five families, not one: every read that carried this contact's uuid now reads null.
    # See *"Deleting a contact invalidates five cache families"* in DECISIONS.md.
    await invalidate_dive_caches(owner_id)
    await invalidate_course_caches(owner_id)
    await invalidate_certification_caches(owner_id)
    await invalidate_gear_caches(owner_id)
    await invalidate_trip_caches(owner_id)
    await invalidate_trip_items(stayed_on)

    return {"message": "Contact deleted"}
