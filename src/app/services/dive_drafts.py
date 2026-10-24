"""Reading and writing dive drafts.

**A reference a draft holds is checked when it is read, not when it is written.** The uuids
in `values` are not foreign keys: the records they name can be deleted while the draft lives,
and a phone writes a draft from a copy that predates the delete. So a write stores them as
given, and every read keeps only those the account still holds - species, a global catalog,
need only exist - with one query per kind over the rows read. The next whole-state write from
either client then no longer carries what was dropped. Logging the draft validates the dive
as `POST /dive` does, so a reference a phone still holds offline is that route's 422 on the
field. See DECISIONS.md, *"A dive draft's references are checked on the way out"*.
"""

import uuid as uuid_pkg
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Row, Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions.http_exceptions import NotFoundException
from ..models.contact import Contact
from ..models.course import Course
from ..models.dive_draft import DiveDraft
from ..models.dive_site import DiveSite
from ..models.gear_item import GearItem
from ..models.gear_set import GearSet
from ..models.person import Person
from ..models.species import Species
from ..models.trip import Trip
from ..schemas.dive_draft import (
    DIVE_DRAFT_NOT_FOUND,
    DiveDraftCreate,
    DiveDraftRead,
    DiveDraftUpdate,
    DiveDraftValuesRead,
)

type Referenced = type[Trip | Course | Contact | DiveSite | GearItem | Person | Species]
type Held = dict[Referenced, set[uuid_pkg.UUID]]


async def _gear_set_id(db: AsyncSession, gear_set_uuid: uuid_pkg.UUID | None, user_id: int) -> int | None:
    """The caller's set, or none: another account's set, or one deleted since the client
    saw it, is stored as no set, for the reason the references in `values` are kept."""
    if gear_set_uuid is None:
        return None
    return await db.scalar(select(GearSet.id).where(GearSet.uuid == gear_set_uuid, GearSet.user_id == user_id))


async def create_dive_draft(db: AsyncSession, draft: DiveDraftCreate, *, user_id: int) -> int:
    """Add and flush the row, uncommitted, and return its id."""
    now = datetime.now(UTC)
    row = DiveDraft(
        user_id=user_id,
        values=draft.values.stored(),
        gear_set_id=await _gear_set_id(db, draft.gear_set_uuid, user_id),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.flush()
    return row.id


async def update_dive_draft(db: AsyncSession, draft_id: int, changes: DiveDraftUpdate, *, user_id: int) -> None:
    """Apply a `PATCH`, uncommitted. 404 when the draft went between the ownership check and
    here - logged or discarded on the other device - which is the answer a client keeps."""
    columns: dict[str, Any] = {}
    if changes.values is not None:
        columns["values"] = changes.values.stored()
    if "gear_set_uuid" in changes.model_fields_set:
        columns["gear_set_id"] = await _gear_set_id(db, changes.gear_set_uuid, user_id)
    if not columns:
        return
    columns["updated_at"] = datetime.now(UTC)
    updated = await db.execute(
        update(DiveDraft).where(DiveDraft.id == draft_id).values(**columns).returning(DiveDraft.id)
    )
    if updated.first() is None:
        raise NotFoundException(DIVE_DRAFT_NOT_FOUND)


def _drafts(user_id: int) -> Select[Any]:
    return (
        select(DiveDraft.uuid, DiveDraft.created_at, DiveDraft.updated_at, DiveDraft.values, GearSet.uuid.label("set"))
        .outerjoin(GearSet, GearSet.id == DiveDraft.gear_set_id)
        .where(DiveDraft.user_id == user_id)
        .order_by(DiveDraft.updated_at.desc(), DiveDraft.id.desc())
    )


async def read_dive_draft(db: AsyncSession, draft_id: int, *, user_id: int) -> DiveDraftRead:
    rows = (await db.execute(_drafts(user_id).where(DiveDraft.id == draft_id))).all()
    if not rows:
        raise NotFoundException(DIVE_DRAFT_NOT_FOUND)
    (draft,) = await _as_read(db, rows, user_id=user_id)
    return draft


async def read_dive_drafts_page(db: AsyncSession, *, user_id: int, offset: int, limit: int) -> dict[str, Any]:
    """`GET /dive-drafts`' page, in the shape `paginated_response` takes."""
    rows = (await db.execute(_drafts(user_id).offset(offset).limit(limit))).all()
    total = await db.scalar(select(func.count()).select_from(DiveDraft).where(DiveDraft.user_id == user_id))
    return {"data": await _as_read(db, rows, user_id=user_id), "total_count": total or 0}


async def read_all_dive_drafts(db: AsyncSession, *, user_id: int) -> list[DiveDraftRead]:
    """Every draft of the account's, in the list's order, for the export."""
    return await _as_read(db, (await db.execute(_drafts(user_id))).all(), user_id=user_id)


def _references(values: DiveDraftValuesRead) -> Iterator[tuple[Referenced, uuid_pkg.UUID]]:
    for model, uuid in ((Trip, values.trip_uuid), (Course, values.course_uuid), (Contact, values.contact_uuid)):
        if uuid is not None:
            yield model, uuid
    for uuid in values.dive_site_uuids or []:
        yield DiveSite, uuid
    for uuid in values.gear_item_uuids or []:
        yield GearItem, uuid
    for mixture in values.mixtures or []:
        if mixture.gear_item_uuid is not None:
            yield GearItem, mixture.gear_item_uuid
    for sighting in values.sightings or []:
        yield Species, sighting.species_uuid
    for person in values.people or []:
        yield Person, person.person_uuid


async def _held(db: AsyncSession, wanted: Held, *, user_id: int) -> Held:
    held: Held = {}
    for model, uuids in wanted.items():
        statement = select(model.uuid).where(model.uuid.in_(uuids))
        if not issubclass(model, Species):
            statement = statement.where(model.user_id == user_id)
        held[model] = set((await db.execute(statement)).scalars())
    return held


def _scrubbed(values: DiveDraftValuesRead, held: Held) -> DiveDraftValuesRead:
    def one(model: Referenced, uuid: uuid_pkg.UUID | None) -> uuid_pkg.UUID | None:
        return uuid if uuid in held.get(model, set()) else None

    def some(model: Referenced, uuids: list[uuid_pkg.UUID] | None) -> list[uuid_pkg.UUID] | None:
        return None if uuids is None else [uuid for uuid in uuids if uuid in held.get(model, set())]

    return values.model_copy(
        update={
            "trip_uuid": one(Trip, values.trip_uuid),
            "course_uuid": one(Course, values.course_uuid),
            "contact_uuid": one(Contact, values.contact_uuid),
            "dive_site_uuids": some(DiveSite, values.dive_site_uuids),
            "gear_item_uuids": some(GearItem, values.gear_item_uuids),
            "mixtures": None
            if values.mixtures is None
            else [
                mixture.model_copy(update={"gear_item_uuid": one(GearItem, mixture.gear_item_uuid)})
                for mixture in values.mixtures
            ],
            "sightings": None
            if values.sightings is None
            else [sighting for sighting in values.sightings if sighting.species_uuid in held.get(Species, set())],
            "people": None
            if values.people is None
            else [person for person in values.people if person.person_uuid in held.get(Person, set())],
        }
    )


async def _as_read(db: AsyncSession, rows: Sequence[Row[Any]], *, user_id: int) -> list[DiveDraftRead]:
    parsed = [(row, DiveDraftValuesRead.model_validate(row.values)) for row in rows]
    wanted: Held = {}
    for _, values in parsed:
        for model, uuid in _references(values):
            wanted.setdefault(model, set()).add(uuid)
    held = await _held(db, wanted, user_id=user_id)
    return [
        DiveDraftRead(
            uuid=row.uuid,
            created_at=row.created_at,
            updated_at=row.updated_at,
            gear_set_uuid=row.set,
            values=_scrubbed(values, held),
        )
        for row, values in parsed
    ]
