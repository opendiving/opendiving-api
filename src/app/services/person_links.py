"""Linking a person to an account on this instance, and resolving the people a write names.

A link names another account by its exact username and stores the account, never the
string, so it survives a rename. It answers whether that username exists - the answer the
availability check already gives, an account in its deletion grace period included - and is
throttled like it. See *"Linking a person confirms an account exists, and nothing else"* in
DECISIONS.md.
"""

import uuid as uuid_pkg
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi.exceptions import RequestValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.exceptions.http_exceptions import UnprocessableEntityException
from ..core.utils.rate_limit import claim_rate_limit_slot, enforce_rate_limit, remaining_in_window
from ..crud.crud_people import StoredReference, person_linking, resolve_person_ids_for_user
from ..models.user import User
from ..schemas.person import PERSON_NOT_FOUND, PersonReference

NO_SUCH_ACCOUNT = "No account has that username."
OWN_ACCOUNT = "That is your own account."


def _refuse_username(message: str, username: str) -> RequestValidationError:
    """A refusal on the `username` field, where a form shows it."""
    return RequestValidationError(
        [{"type": "value_error", "loc": ("body", "username"), "msg": message, "input": username}]
    )


def _link_key(user_id: int) -> str:
    return f"person-link:user:{user_id}"


async def spend_link_attempt(user_id: int) -> None:
    """Count one link against the caller's window, or 429."""
    await enforce_rate_limit(
        _link_key(user_id), settings.PERSON_LINK_RATE_LIMIT_PER_USER, settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS
    )


async def claim_link_slot(user_id: int) -> bool:
    """One link from an import's apply, answered rather than raised: an exhausted window
    drops that link and not the import."""
    return await claim_rate_limit_slot(
        _link_key(user_id), settings.PERSON_LINK_RATE_LIMIT_PER_USER, settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS
    )


async def link_budget_remaining(user_id: int) -> int:
    """What the window has left, unspent - the preview's half of `claim_link_slot`."""
    return await remaining_in_window(_link_key(user_id), settings.PERSON_LINK_RATE_LIMIT_PER_USER)


async def resolve_linked_account(
    db: AsyncSession, *, owner_id: int, username: str, person_id: int | None = None
) -> int:
    """The account `username` names, for the caller's person `person_id` to link to.

    Found for exactly the usernames the availability check calls taken. Refused, each on the
    `username` field: no such account; the caller's own; one another of the caller's people
    already links, which the refusal names.
    """
    account_id = await db.scalar(select(User.id).where(User.username == username))
    if account_id is None:
        raise _refuse_username(NO_SUCH_ACCOUNT, username)
    if account_id == owner_id:
        raise _refuse_username(OWN_ACCOUNT, username)
    holder = await person_linking(db, user_id=owner_id, linked_user_id=account_id)
    if holder is not None and holder[0] != person_id:
        raise _refuse_username(f"{holder[1]} is already linked to that account.", username)
    return account_id


@dataclass(frozen=True, slots=True)
class Account:
    id: int
    username: str


async def accounts_by_uuid(db: AsyncSession, uuids: Sequence[uuid_pkg.UUID]) -> dict[uuid_pkg.UUID, Account]:
    """The accounts these public ids name, as the availability check counts accounts - a
    deleted one in its grace period included, a purged one not."""
    if not uuids:
        return {}
    rows = await db.execute(select(User.uuid, User.id, User.username).where(User.uuid.in_(set(uuids))))
    return {row.uuid: Account(id=row.id, username=row.username) for row in rows}


async def resolve_people_references(
    db: AsyncSession, references: Sequence[PersonReference], *, user_id: int
) -> list[StoredReference]:
    """A write's people as the join tables store them, or the 422 a person that isn't the
    caller's own - or doesn't exist - gets."""
    ids = await resolve_person_ids_for_user(db, [reference.person_uuid for reference in references], user_id)
    if ids is None:
        raise UnprocessableEntityException(PERSON_NOT_FOUND)
    return [
        (ids[reference.person_uuid], None if reference.role is None else reference.role.value)
        for reference in references
    ]


async def resolve_person_reference(db: AsyncSession, *, person_uuid: uuid_pkg.UUID, user_id: int) -> int:
    """One person's id, or the same 422 - a certification's `instructor_uuid`."""
    ids = await resolve_person_ids_for_user(db, [person_uuid], user_id)
    if ids is None:
        raise UnprocessableEntityException(PERSON_NOT_FOUND)
    return ids[person_uuid]
