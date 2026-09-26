import uuid as uuid_pkg

from fastcrud import FastCRUD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.contact import Contact
from ..schemas.contact import ContactCreateInternal, ContactReadInternal, ContactUpdate, ContactUpdateInternal

CRUDContact = FastCRUD[
    Contact, ContactCreateInternal, ContactUpdate, ContactUpdateInternal, ContactUpdate, ContactReadInternal
]
crud_contacts = CRUDContact(Contact)

# What `GET /contacts?search=` matches against: the name, and the town, which is how a
# diver remembers the shop they have not been back to ("that place in Dahab"). Named here
# rather than inline so `test_picker_search.py` can assert the columns exist.
CONTACT_SEARCH_COLUMNS = ("name", "address_city")


async def contact_name_exists(db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None) -> bool:
    """Case-insensitive check for whether this diver already has a contact of this name.

    Mirrors `ux_contact_user_id_name_lower`, which enforces the same rule at the database
    level as the safety net.
    """
    stmt = select(Contact.id).where(Contact.user_id == user_id, func.lower(Contact.name) == name.strip().lower())
    if exclude_id is not None:
        stmt = stmt.where(Contact.id != exclude_id)
    result = await db.execute(stmt.limit(1))
    return result.first() is not None


async def resolve_contact_id_for_user(db: AsyncSession, contact_uuid: uuid_pkg.UUID, user_id: int) -> int | None:
    """Resolve a contact's public `uuid` to its internal `id`, scoped to the given user -
    the twin of `resolve_course_id_for_user`, and what stops a diver linking somebody
    else's contact to their own dive, course, card or service record."""
    result = await db.execute(
        select(Contact.id).where(Contact.uuid == contact_uuid, Contact.user_id == user_id).limit(1)
    )
    row = result.first()
    return row[0] if row is not None else None


async def resolve_contact_ids_for_user(
    db: AsyncSession, contact_uuids: list[uuid_pkg.UUID], user_id: int
) -> dict[uuid_pkg.UUID, int] | None:
    """Batched `resolve_contact_id_for_user`, for a trip's parts. `None` when any uuid is
    not one of the user's contacts, as `resolve_dive_site_ids_for_user` answers."""
    unique_uuids = set(contact_uuids)
    if not unique_uuids:
        return {}

    result = await db.execute(
        select(Contact.uuid, Contact.id).where(Contact.uuid.in_(unique_uuids), Contact.user_id == user_id)
    )
    mapping = {row.uuid: row.id for row in result}
    if mapping.keys() != unique_uuids:
        return None
    return mapping


async def get_contact_uuids_by_ids(
    db: AsyncSession, contact_ids: list[int | None], user_id: int
) -> dict[int | None, uuid_pkg.UUID]:
    """Batched lookup of contact `id` -> `uuid`, for enriching a page of dives, courses,
    certifications or service records without a query per row.

    Takes the nullable ids straight off the rows and drops the `None`s here; the widened key
    lets a caller `.get()` its row's own nullable id and read `None` for an unlinked one. The
    `user_id` scope is defence in depth, as in `get_course_uuids_by_ids`.
    """
    wanted = {contact_id for contact_id in contact_ids if contact_id is not None}
    if not wanted:
        return {}

    result = await db.execute(
        select(Contact.id, Contact.uuid).where(Contact.id.in_(wanted), Contact.user_id == user_id)
    )
    return {row.id: row.uuid for row in result}


async def get_contact_names_by_ids(db: AsyncSession, contact_ids: list[int | None], user_id: int) -> dict[int, str]:
    """`get_contact_uuids_by_ids`' twin for the name, which a check-in link's summary prints in
    place of the uuid."""
    wanted = {contact_id for contact_id in contact_ids if contact_id is not None}
    if not wanted:
        return {}

    result = await db.execute(
        select(Contact.id, Contact.name).where(Contact.id.in_(wanted), Contact.user_id == user_id)
    )
    return {row.id: row.name for row in result}
