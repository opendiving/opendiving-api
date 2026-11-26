import uuid as uuid_pkg
from collections.abc import Sequence
from typing import Any

from fastcrud import FastCRUD
from sqlalchemy import ColumnElement, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.search import search_clause
from ..models.course_person import CoursePerson
from ..models.dive import Dive
from ..models.dive_person import DivePerson
from ..models.person import Person
from ..models.trip_person import TripPerson
from ..models.user import User
from ..models.user_picture import UserPicture
from ..schemas.person import (
    AddedYouRow,
    PersonCreateInternal,
    PersonRead,
    PersonReadInternal,
    PersonReferenceRead,
    PersonUpdate,
    PersonUpdateInternal,
)
from .crud_users import avatar_of
from .unique_keys import unique_key_taken

CRUDPerson = FastCRUD[
    Person, PersonCreateInternal, PersonUpdate, PersonUpdateInternal, PersonUpdate, PersonReadInternal
]
crud_people = CRUDPerson(Person)

# A reference as the join tables store it: the person's row id and the role, or none.
StoredReference = tuple[int, str | None]
# One join table, as the three hosts share its shape.
PersonJoin = type[DivePerson] | type[TripPerson] | type[CoursePerson]


def _dive_count() -> Any:
    """The live dives naming the person - the rows of `dive_person` whose dive is not
    soft-deleted, which is exactly what `GET /dives?person_uuid=` matches."""
    return (
        select(func.count(DivePerson.id))
        .join(Dive, Dive.id == DivePerson.dive_id)
        .where(DivePerson.person_id == Person.id, Dive.is_deleted.is_(False))
        .correlate(Person)
        .scalar_subquery()
    )


def _select_people() -> Any:
    """What a person read selects: the columns, the connected account's current username and
    picture version, and the dive count. An account in its deletion grace period is still
    named, as a restore would bring the tie back and the purge's `SET NULL` is what clears it;
    its picture is not served from the moment it asks."""
    return (
        select(
            Person.uuid,
            Person.name,
            Person.email,
            Person.phone,
            Person.links,
            Person.notes,
            User.username,
            UserPicture.rendition_sha256.label("avatar_sha256"),
            _dive_count().label("dive_count"),
            Person.created_at,
            Person.updated_at,
        )
        .select_from(Person)
        .outerjoin(User, User.id == Person.connected_user_id)
        .outerjoin(UserPicture, and_(avatar_of(User.id), User.is_deleted.is_(False)))
    )


def person_search_condition(term: str) -> ColumnElement[bool]:
    """A person matches on its own name or on its connected account's username, each a
    case-insensitive substring - the second is what lets a picker find a friend by handle."""
    return or_(search_clause(Person, ("name",), term), search_clause(User, ("username",), term))


async def get_people_page(
    db: AsyncSession, *, user_id: int, offset: int, limit: int, search: str | None
) -> dict[str, Any]:
    """One page of a diver's people by name, in `get_multi`'s `{data, total_count}` shape."""
    conditions: list[ColumnElement[bool]] = [Person.user_id == user_id]
    if search:
        conditions.append(person_search_condition(search))
    total_count = await db.scalar(
        select(func.count()).select_from(Person).outerjoin(User, User.id == Person.connected_user_id).where(*conditions)
    )
    rows = await db.execute(
        _select_people().where(*conditions).order_by(Person.name, Person.id).offset(offset).limit(limit)
    )
    return {"data": [PersonRead.model_validate(dict(row._mapping)) for row in rows], "total_count": total_count or 0}


async def get_person_read(db: AsyncSession, *, person_id: int) -> PersonRead | None:
    row = (await db.execute(_select_people().where(Person.id == person_id))).first()
    return None if row is None else PersonRead.model_validate(dict(row._mapping))


PERSON_NAME_KEY = "ux_person_user_id_name_lower"


async def person_name_exists(db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None) -> bool:
    """Asked on `ux_person_user_id_name_lower`'s own expressions."""
    return await unique_key_taken(
        db, Person, PERSON_NAME_KEY, {"user_id": user_id, "name": name}, exclude_id=exclude_id
    )


async def person_connected_to_uuid(db: AsyncSession, *, user_id: int, connected_user_id: int) -> uuid_pkg.UUID | None:
    """The public id of the diver's person connected to this account, or `None`."""
    return await db.scalar(
        select(Person.uuid).where(Person.user_id == user_id, Person.connected_user_id == connected_user_id)
    )


async def person_connected_to(db: AsyncSession, *, user_id: int, connected_user_id: int) -> tuple[int, str] | None:
    """The diver's person already connected to this account, as its id and name, or `None`."""
    row = (
        await db.execute(
            select(Person.id, Person.name).where(
                Person.user_id == user_id, Person.connected_user_id == connected_user_id
            )
        )
    ).first()
    return None if row is None else (row.id, row.name)


def _added_you(user_id: int) -> Any:
    """The accounts under `user_id`'s *Added you*, with the person each has connected to them:
    not hidden, not an account `user_id` has a person connected to, not one asking to be
    deleted. One row per account, since an account connects at most one person to another."""
    caller_has_them = (
        select(Person.id).where(Person.user_id == user_id, Person.connected_user_id == User.id).correlate(User).exists()
    )
    return (
        select(User.uuid, User.name, User.username, UserPicture.rendition_sha256.label("avatar_sha256"))
        .select_from(Person)
        .join(User, User.id == Person.user_id)
        .outerjoin(UserPicture, avatar_of(User.id))
        .where(
            Person.connected_user_id == user_id,
            Person.connection_hidden_at.is_(None),
            User.is_deleted.is_(False),
            ~caller_has_them,
        )
    )


async def get_added_you_page(db: AsyncSession, *, user_id: int, offset: int, limit: int) -> dict[str, Any]:
    """One page of *Added you* by the account's name, in `get_multi`'s `{data, total_count}`
    shape."""
    listed = _added_you(user_id).subquery()
    total_count = await db.scalar(select(func.count()).select_from(listed))
    rows = await db.execute(select(listed).order_by(listed.c.name, listed.c.uuid).offset(offset).limit(limit))
    return {"data": [AddedYouRow.model_validate(dict(row._mapping)) for row in rows], "total_count": total_count or 0}


async def tie_from(db: AsyncSession, *, account_uuid: uuid_pkg.UUID, user_id: int) -> tuple[int, int] | None:
    """The account `account_uuid` names and its person connected to `user_id`, as their ids,
    or `None` when there is no such tie. Hidden or added back, a tie is still one."""
    row = (
        await db.execute(
            select(User.id.label("account_id"), Person.id.label("person_id"))
            .join(Person, Person.user_id == User.id)
            .where(User.uuid == account_uuid, Person.connected_user_id == user_id)
        )
    ).first()
    return None if row is None else (row.account_id, row.person_id)


async def resolve_person_ids_for_user(
    db: AsyncSession, person_uuids: Sequence[uuid_pkg.UUID], user_id: int
) -> dict[uuid_pkg.UUID, int] | None:
    """Batched uuid -> id for the caller's own people. `None` when any uuid is not one of
    them, so someone else's person never resolves - the answer `resolve_contact_ids_for_user`
    gives."""
    unique_uuids = set(person_uuids)
    if not unique_uuids:
        return {}
    result = await db.execute(
        select(Person.uuid, Person.id).where(Person.uuid.in_(unique_uuids), Person.user_id == user_id)
    )
    mapping = {row.uuid: row.id for row in result}
    if mapping.keys() != unique_uuids:
        return None
    return mapping


async def get_person_uuids_by_ids(
    db: AsyncSession, person_ids: Sequence[int | None], user_id: int
) -> dict[int | None, uuid_pkg.UUID]:
    """Batched id -> uuid, for a page of certifications' instructors. The `user_id` scope is
    defence in depth, as in `get_contact_uuids_by_ids`."""
    wanted = {person_id for person_id in person_ids if person_id is not None}
    if not wanted:
        return {}
    result = await db.execute(select(Person.id, Person.uuid).where(Person.id.in_(wanted), Person.user_id == user_id))
    return {row.id: row.uuid for row in result}


async def get_person_names_by_ids(
    db: AsyncSession, person_ids: Sequence[int | None], user_id: int
) -> dict[int | None, str]:
    """`get_person_uuids_by_ids`' twin for the name, which the check-in link prints."""
    wanted = {person_id for person_id in person_ids if person_id is not None}
    if not wanted:
        return {}
    result = await db.execute(select(Person.id, Person.name).where(Person.id.in_(wanted), Person.user_id == user_id))
    return {row.id: row.name for row in result}


async def _people_for(
    db: AsyncSession, model: PersonJoin, host_column: Any, host_ids: Sequence[int]
) -> dict[int, list[PersonReferenceRead]]:
    by_host: dict[int, list[PersonReferenceRead]] = {host_id: [] for host_id in host_ids}
    if not host_ids:
        return by_host
    rows = await db.execute(
        select(host_column.label("host_id"), Person.uuid, model.role)
        .join(Person, Person.id == model.person_id)
        .where(host_column.in_(set(host_ids)))
        .order_by(host_column, model.position)
    )
    for row in rows:
        by_host[row.host_id].append(PersonReferenceRead(person_uuid=row.uuid, role=row.role))
    return by_host


async def get_people_for_dives(db: AsyncSession, dive_ids: Sequence[int]) -> dict[int, list[PersonReferenceRead]]:
    return await _people_for(db, DivePerson, DivePerson.dive_id, dive_ids)


async def get_people_for_trips(db: AsyncSession, trip_ids: Sequence[int]) -> dict[int, list[PersonReferenceRead]]:
    return await _people_for(db, TripPerson, TripPerson.trip_id, trip_ids)


async def get_people_for_courses(db: AsyncSession, course_ids: Sequence[int]) -> dict[int, list[PersonReferenceRead]]:
    return await _people_for(db, CoursePerson, CoursePerson.course_id, course_ids)


async def _replace_people(
    db: AsyncSession,
    model: PersonJoin,
    host_column: Any,
    host_id: int,
    references: Sequence[StoredReference],
    commit: bool,
) -> None:
    """Replace a host's people with the given ordered list, as `replace_species_for_dive`
    replaces a dive's species: delete, then insert with positions. A person named twice
    keeps its first position and its first role - the join tables refuse a repeat, and the
    first is what the diver put first."""
    kept: dict[int, str | None] = {}
    for person_id, role in references:
        kept.setdefault(person_id, role)
    await db.execute(delete(model).where(host_column == host_id))
    for position, (person_id, role) in enumerate(kept.items()):
        db.add(model(**{host_column.key: host_id}, person_id=person_id, position=position, role=role))
    if commit:
        await db.commit()


async def replace_people_for_dive(
    db: AsyncSession, dive_id: int, references: Sequence[StoredReference], commit: bool = True
) -> None:
    await _replace_people(db, DivePerson, DivePerson.dive_id, dive_id, references, commit)


async def replace_people_for_trip(
    db: AsyncSession, trip_id: int, references: Sequence[StoredReference], commit: bool = True
) -> None:
    await _replace_people(db, TripPerson, TripPerson.trip_id, trip_id, references, commit)


async def replace_people_for_course(
    db: AsyncSession, course_id: int, references: Sequence[StoredReference], commit: bool = True
) -> None:
    await _replace_people(db, CoursePerson, CoursePerson.course_id, course_id, references, commit)
