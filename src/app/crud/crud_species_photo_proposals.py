import uuid as uuid_pkg
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ..models.species import Species
from ..models.species_photo_proposal import SpeciesPhotoProposal
from ..models.user import User
from ..schemas.species_photo_proposal import (
    SpeciesPhotoProposalRead,
    SpeciesPhotoProposalSpecies,
    SpeciesPhotoProposalStatus,
)

_WAITING = SpeciesPhotoProposalStatus.WAITING.value

# A waiting proposal the operator sees: an account being deleted has gone dark, so its proposals
# leave the count, the filter and the review, and come back if it is restored.
_REVIEWABLE = (SpeciesPhotoProposal.status == _WAITING, User.is_deleted.is_(False))


def has_reviewable_proposal() -> ColumnElement[bool]:
    """A predicate on `Species`: a proposal waits on it for the operator."""
    return exists(
        select(SpeciesPhotoProposal.id)
        .join(User, User.id == SpeciesPhotoProposal.user_id)
        .where(SpeciesPhotoProposal.species_id == Species.id, *_REVIEWABLE)
    )


async def reviewable_counts(db: AsyncSession, species_ids: Sequence[int]) -> dict[int, int]:
    """How many proposals wait for the operator on each of these species; absent means none."""
    if not species_ids:
        return {}
    rows = await db.execute(
        select(SpeciesPhotoProposal.species_id, func.count().label("waiting"))
        .join(User, User.id == SpeciesPhotoProposal.user_id)
        .where(SpeciesPhotoProposal.species_id.in_(set(species_ids)), *_REVIEWABLE)
        .group_by(SpeciesPhotoProposal.species_id)
    )
    return {row.species_id: row.waiting for row in rows}


async def reviewable_proposals(db: AsyncSession, *, species_id: int, limit: int) -> list[dict[str, Any]]:
    """The oldest waiting proposals on a species the operator may decide, with the proposer's
    name, as plain dicts."""
    rows = await db.execute(
        select(
            SpeciesPhotoProposal.uuid,
            SpeciesPhotoProposal.file,
            SpeciesPhotoProposal.note,
            SpeciesPhotoProposal.created_at,
            User.name.label("proposer_name"),
        )
        .join(User, User.id == SpeciesPhotoProposal.user_id)
        .where(SpeciesPhotoProposal.species_id == species_id, *_REVIEWABLE)
        .order_by(SpeciesPhotoProposal.created_at, SpeciesPhotoProposal.id)
        .limit(limit)
    )
    return [dict(row) for row in rows.mappings()]


async def reviewable_proposal(
    db: AsyncSession, *, uuid: uuid_pkg.UUID, for_update: bool = False
) -> SpeciesPhotoProposal | None:
    """A proposal the operator may decide, of an account not being deleted, whatever its status."""
    statement = (
        select(SpeciesPhotoProposal)
        .join(User, User.id == SpeciesPhotoProposal.user_id)
        .where(SpeciesPhotoProposal.uuid == uuid, User.is_deleted.is_(False))
    )
    if for_update:
        statement = statement.with_for_update(of=SpeciesPhotoProposal)
    proposal: SpeciesPhotoProposal | None = await db.scalar(statement)
    return proposal


async def was_declined(db: AsyncSession, *, species_id: int, file: str) -> bool:
    declined = await db.scalar(
        select(
            exists().where(
                SpeciesPhotoProposal.species_id == species_id,
                SpeciesPhotoProposal.file == file,
                SpeciesPhotoProposal.status == SpeciesPhotoProposalStatus.DECLINED.value,
            )
        )
    )
    return bool(declined)


async def write_waiting_proposal(
    db: AsyncSession, *, user_id: int, species_id: int, file: str, note: str, now: datetime
) -> tuple[uuid_pkg.UUID, datetime]:
    """Write this account's waiting proposal for the species, replacing the one that waits if
    there is one. Two at once leave one row. Does not commit.

    A replacement is a new proposal, under a new uuid and at the back of the review: accepting
    names a uuid and pins the file its row holds, so a uuid the operator was shown must never
    come to name a file they were not.
    """
    replaced = {"file": file, "note": note, "uuid": uuid7(), "created_at": now}
    statement = (
        insert(SpeciesPhotoProposal)
        .values(user_id=user_id, species_id=species_id, status=_WAITING, **replaced)
        .on_conflict_do_update(
            index_elements=["user_id", "species_id"],
            index_where=SpeciesPhotoProposal.status == _WAITING,
            set_={**replaced, "updated_at": now},
        )
        .returning(SpeciesPhotoProposal.uuid, SpeciesPhotoProposal.created_at)
    )
    row = (await db.execute(statement)).one()
    return row.uuid, row.created_at


async def settle_waiting_proposals(
    db: AsyncSession, *, species_id: int, pinned_file: str | None, now: datetime
) -> None:
    """The operator chose this species' photo: every waiting proposal naming `pinned_file` is
    accepted, and every other one closed - all of them for a hide, which pins nothing. Every
    account's, a deleting one's included, so none is left waiting on a curated species. Does not
    commit."""
    decided = {"decided_at": now, "updated_at": now}
    waiting = (SpeciesPhotoProposal.species_id == species_id, SpeciesPhotoProposal.status == _WAITING)
    if pinned_file is not None:
        await db.execute(
            update(SpeciesPhotoProposal)
            .where(*waiting, SpeciesPhotoProposal.file == pinned_file)
            .values(status=SpeciesPhotoProposalStatus.ACCEPTED.value, **decided)
        )
    await db.execute(
        update(SpeciesPhotoProposal).where(*waiting).values(status=SpeciesPhotoProposalStatus.CLOSED.value, **decided)
    )


async def get_proposals_page(
    db: AsyncSession, *, user_id: int, status: SpeciesPhotoProposalStatus | None, offset: int, limit: int
) -> dict[str, Any]:
    """One page of an account's own proposals, newest first, in `get_multi`'s shape."""
    clauses = [SpeciesPhotoProposal.user_id == user_id]
    if status is not None:
        clauses.append(SpeciesPhotoProposal.status == status.value)
    total_count = await db.scalar(select(func.count()).select_from(SpeciesPhotoProposal).where(*clauses))
    rows = await db.execute(
        select(
            SpeciesPhotoProposal.uuid,
            SpeciesPhotoProposal.file,
            SpeciesPhotoProposal.note,
            SpeciesPhotoProposal.status,
            SpeciesPhotoProposal.reason,
            SpeciesPhotoProposal.created_at,
            SpeciesPhotoProposal.decided_at,
            Species.uuid.label("species_uuid"),
            Species.scientific_name,
            Species.common_name,
        )
        .join(Species, Species.id == SpeciesPhotoProposal.species_id)
        .where(*clauses)
        .order_by(SpeciesPhotoProposal.created_at.desc(), SpeciesPhotoProposal.id.desc())
        .offset(offset)
        .limit(limit)
    )
    data = [
        SpeciesPhotoProposalRead(
            uuid=row.uuid,
            species=SpeciesPhotoProposalSpecies(
                uuid=row.species_uuid, scientific_name=row.scientific_name, common_name=row.common_name
            ),
            file=row.file,
            note=row.note,
            status=row.status,
            reason=row.reason,
            created_at=row.created_at,
            decided_at=row.decided_at,
        ).model_dump()
        for row in rows
    ]
    return {"data": data, "total_count": total_count or 0}
