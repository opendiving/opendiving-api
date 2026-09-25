import uuid as uuid_pkg
from typing import NamedTuple

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


class ContactRef(NamedTuple):
    """What a host read needs of the contact it references: the public id, and - for as long
    as the course and certification reads carry a training-center name for the web build
    that still prints one - the name."""

    uuid: uuid_pkg.UUID
    name: str


async def _contact_id_named(db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None) -> int | None:
    stmt = select(Contact.id).where(Contact.user_id == user_id, func.lower(Contact.name) == name.strip().lower())
    if exclude_id is not None:
        stmt = stmt.where(Contact.id != exclude_id)
    result = await db.execute(stmt.limit(1))
    row = result.first()
    return row[0] if row is not None else None


async def contact_name_exists(db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None) -> bool:
    """Case-insensitive check for whether this diver already has a contact of this name.

    Mirrors `ux_contact_user_id_name_lower`, which enforces the same rule at the database
    level as the safety net.
    """
    return await _contact_id_named(db, user_id, name, exclude_id) is not None


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


async def get_contact_refs_by_ids(
    db: AsyncSession, contact_ids: list[int | None], user_id: int
) -> dict[int | None, ContactRef]:
    """Batched lookup of contact `id` -> uuid and name, for enriching a page of dives,
    courses, certifications or service records without a query per row.

    Takes the nullable ids straight off the rows and drops the `None`s here; the widened key
    lets a caller `.get()` its row's own nullable id and read `None` for an unlinked one, as
    `_schedule_uuids_by_id` in `gear_service.py` does. The `user_id` scope is defence in
    depth, as in `get_course_uuids_by_ids`.
    """
    wanted = {contact_id for contact_id in contact_ids if contact_id is not None}
    if not wanted:
        return {}

    result = await db.execute(
        select(Contact.id, Contact.uuid, Contact.name).where(Contact.id.in_(wanted), Contact.user_id == user_id)
    )
    return {row.id: ContactRef(uuid=row.uuid, name=row.name) for row in result}


async def resolve_or_create_contact(db: AsyncSession, *, user_id: int, name: str) -> tuple[int, bool]:
    """The contact a training-center name from the previous web build means: the diver's
    contact of that name, case-insensitively, or a new one with the `school` role.

    Exists for the deploy-skew shim on the course and certification writes alone, and goes
    with it. The logbook importer resolves the same legacy strings its own way, in the
    planner, because a preview must predict rather than create.

    Flushes rather than commits, so the new row and the write that links it land in the
    route's one commit. Returns the id and whether it made the row, so the caller knows to
    drop the contact list it just grew.
    """
    existing = await _contact_id_named(db, user_id, name)
    if existing is not None:
        return existing, False

    contact = Contact(user_id=user_id, name=name.strip(), roles=["school"])
    db.add(contact)
    await db.flush()
    return contact.id, True
