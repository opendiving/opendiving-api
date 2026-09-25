"""Resolving a `contact_uuid` on a write to a record that references a contact."""

import uuid as uuid_pkg

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions.http_exceptions import UnprocessableEntityException
from ..crud.crud_contacts import resolve_contact_id_for_user

CONTACT_NOT_FOUND = "Contact not found."


async def resolve_contact_reference(db: AsyncSession, *, contact_uuid: uuid_pkg.UUID, user_id: int) -> int:
    """A contact's internal id, or the 422 a reference to someone else's - or to nothing -
    gets everywhere in this API."""
    contact_id = await resolve_contact_id_for_user(db=db, contact_uuid=contact_uuid, user_id=user_id)
    if contact_id is None:
        raise UnprocessableEntityException(CONTACT_NOT_FOUND)
    return contact_id
