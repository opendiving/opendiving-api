"""The `contact_id` half of a write to a record that references a contact.

Shared by the course and certification routes, which take the reference two ways for as
long as the web build that posts a training-center string is still deployed - see
`TrainingCenterShim` in `schemas/contact.py`. The dive and service-record routes take
`contact_uuid` alone and resolve it inline, as they do their other references.
"""

import uuid as uuid_pkg
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions.http_exceptions import UnprocessableEntityException
from ..crud.crud_contacts import resolve_contact_id_for_user, resolve_or_create_contact
from ..schemas.contact import training_center_name

CONTACT_NOT_FOUND = "Contact not found."


async def resolve_contact_reference(db: AsyncSession, *, contact_uuid: uuid_pkg.UUID, user_id: int) -> int:
    """A contact's internal id, or the 422 a reference to someone else's - or to nothing -
    gets everywhere in this API."""
    contact_id = await resolve_contact_id_for_user(db=db, contact_uuid=contact_uuid, user_id=user_id)
    if contact_id is None:
        raise UnprocessableEntityException(CONTACT_NOT_FOUND)
    return contact_id


class ContactLink(NamedTuple):
    """What a write body says about `contact_id`: the update to merge, and whether the shim
    created a contact - which the route drops the contact caches for once it has committed,
    the new row being only flushed until then."""

    updates: dict[str, int | None]
    created_contact: bool


async def contact_link_updates(
    db: AsyncSession,
    *,
    user_id: int,
    fields_set: set[str],
    contact_uuid: uuid_pkg.UUID | None,
    training_center: str | None,
) -> ContactLink:
    """What a create or PATCH body says about `contact_id`.

    `contact_uuid` wins whenever it was sent, and an explicit `null` clears the link. Only
    without it does the shim's `training_center` count: a name the diver already has a
    contact for reuses it, any other name creates one with the `school` role, and absent,
    `null` or blank says nothing - so the previous web build, which echoes back the name a
    read gave it, can set a link and can never clear one.
    """
    if "contact_uuid" in fields_set:
        if contact_uuid is None:
            return ContactLink({"contact_id": None}, False)
        contact_id = await resolve_contact_reference(db, contact_uuid=contact_uuid, user_id=user_id)
        return ContactLink({"contact_id": contact_id}, False)

    name = training_center_name(training_center)
    if name is None:
        return ContactLink({}, False)
    contact_id, created = await resolve_or_create_contact(db, user_id=user_id, name=name)
    return ContactLink({"contact_id": contact_id}, created)
