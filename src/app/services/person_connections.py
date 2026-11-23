"""Connecting a person to an account on this instance, and resolving the people a write names.

A tie names another account and stores the account, never a string, so it survives a rename.
A person write makes one from the account's live connect link, or from the account's public
id where that account already has a person connected to the caller - adding back somebody
under *Added you*. Neither answers a guess: a token has 128 bits, and an id needs the other
side's tie. So nothing throttles them; an import, which names accounts by id from a file, is
throttled per user. See *"A connection shows each side the other, and only consent or an
import makes one"* in DECISIONS.md.
"""

import uuid as uuid_pkg
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi.exceptions import RequestValidationError
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.exceptions.http_exceptions import UnprocessableEntityException
from ..core.utils.rate_limit import claim_rate_limit_slot, remaining_in_window
from ..crud.crud_people import StoredReference, person_connected_to, resolve_person_ids_for_user
from ..models.account_disconnect import AccountDisconnect
from ..models.person import Person
from ..models.user import User
from ..schemas.person import PERSON_NOT_FOUND, ConnectsAPerson, PersonReadInternal, PersonReference
from .connect_links import live_connect_link

NO_SUCH_LINK = "This connect link is no longer active."
NO_SUCH_TIE = "No account with that id has you in its people."
OWN_ACCOUNT = "That is your own account."
ALREADY_CONNECTED = "This person is already connected to an account. Disconnect them first."


async def connect_to_the_link_owner(db: AsyncSession, *, user_id: int, via: str | None) -> None:
    """Give an account being created through a live connect link its first person: the link's
    owner, named as they are, connected. Inside the creating transaction; commits nothing.

    Read at the completion, as the gate reads the link, so a link reset or an owner asking to
    be deleted since the sign-in began connects nothing."""
    link = await live_connect_link(db, token=via) if via is not None else None
    if link is None:
        return
    db.add(
        Person(user_id=user_id, name=link.owner_name.strip() or link.owner_username, connected_user_id=link.owner_id)
    )
    await db.flush()


def _refuse(member: str, message: str, value: object) -> RequestValidationError:
    """A refusal on one member of the body, where a form shows it."""
    return RequestValidationError([{"type": "value_error", "loc": ("body", member), "msg": message, "input": value}])


def _connect_key(user_id: int) -> str:
    return f"person-connect:user:{user_id}"


async def claim_connect_slot(user_id: int) -> bool:
    """One tie from an import's apply, answered rather than raised: an exhausted window
    drops that tie and not the import."""
    return await claim_rate_limit_slot(
        _connect_key(user_id),
        settings.PERSON_CONNECT_RATE_LIMIT_PER_USER,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )


async def connect_budget_remaining(user_id: int) -> int:
    """What the window has left, unspent - the upload's half of `claim_connect_slot`."""
    return await remaining_in_window(_connect_key(user_id), settings.PERSON_CONNECT_RATE_LIMIT_PER_USER)


async def has_a_tie_to(db: AsyncSession, *, account_id: int, diver_id: int) -> bool:
    """Whether `account_id` has a person connected to `diver_id`: what makes a row under the
    diver's *Added you*, deletion aside."""
    return bool(
        await db.scalar(select(exists().where(Person.user_id == account_id, Person.connected_user_id == diver_id)))
    )


async def resolve_connection(
    db: AsyncSession, body: ConnectsAPerson, *, owner_id: int, person_id: int | None = None
) -> int | None:
    """The account a person write connects the caller's person to, or `None` when it names
    none.

    Refused on the member that named it: a link that is not live - unknown, reset, removed, or
    its owner asking to be deleted; an id naming no account, or one with no person connected to
    the caller, or one asking to be deleted, all alike; the caller's own account; one another
    of the caller's people is connected to, which the refusal names.
    """
    if body.connect_link is not None:
        member, value = "connect_link", body.connect_link
        link = await live_connect_link(db, token=body.connect_link)
        if link is None:
            raise _refuse(member, NO_SUCH_LINK, value)
        account_id = link.owner_id
    elif body.connect_account is not None:
        member, value = "connect_account", str(body.connect_account)
        found = await db.scalar(select(User.id).where(User.uuid == body.connect_account, User.is_deleted.is_(False)))
        if found == owner_id:
            raise _refuse(member, OWN_ACCOUNT, value)
        if found is None or not await has_a_tie_to(db, account_id=found, diver_id=owner_id):
            raise _refuse(member, NO_SUCH_TIE, value)
        account_id = found
    else:
        return None
    if account_id == owner_id:
        raise _refuse(member, OWN_ACCOUNT, value)
    holder = await person_connected_to(db, user_id=owner_id, connected_user_id=account_id)
    if holder is not None and holder[0] != person_id:
        raise _refuse(member, f"{holder[1]} is already connected to that account.", value)
    return account_id


def refuse_a_second_tie(body: ConnectsAPerson, person: PersonReadInternal) -> None:
    """An update connects only a person who is not connected yet."""
    if person.connected_user_id is None:
        return
    for member in ("connect_link", "connect_account"):
        if getattr(body, member) is not None:
            raise _refuse(member, ALREADY_CONNECTED, str(getattr(body, member)))


async def forget_disconnect(db: AsyncSession, *, account_id: int, diver_id: int) -> None:
    """Drop `account_id`'s record of having disconnected `diver_id`: the diver has connected to
    it again by its link or by adding it back, either of which is that account's consent.
    Commits nothing."""
    await db.execute(
        delete(AccountDisconnect).where(
            AccountDisconnect.user_id == account_id, AccountDisconnect.disconnected_user_id == diver_id
        )
    )


async def disconnected_by(db: AsyncSession, *, diver_id: int) -> set[int]:
    """The accounts that have disconnected `diver_id`, which an import does not connect to."""
    rows = await db.scalars(select(AccountDisconnect.user_id).where(AccountDisconnect.disconnected_user_id == diver_id))
    return set(rows)


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
