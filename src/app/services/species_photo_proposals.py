"""Any account proposing a Commons file as a species' photo, and the operator deciding.

**Proposing asks Wikimedia nothing.** The title is held to its shape alone, so a tool cannot make
the instance spend the Commons counter divers' lookups share; Commons is asked about a proposed
file only when the operator's screen shows it or the operator accepts it.

**A curated species has no waiting proposal.** A pin or a hide settles them where it writes the
row (`species_photos.write_curated_photo`), and proposing for a curated species is refused, under
the row's lock, so the two cannot cross.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from ..core.utils.rate_limit import enforce_rate_limit
from ..crud.crud_species_photo_proposals import reviewable_proposal, was_declined, write_waiting_proposal
from ..models.species import Species
from ..models.species_photo_proposal import SpeciesPhotoProposal
from ..schemas.species_photo_proposal import (
    SpeciesPhotoProposalCreate,
    SpeciesPhotoProposalRead,
    SpeciesPhotoProposalSpecies,
    SpeciesPhotoProposalStatus,
)
from .species_photos import normalize_file_title

NOT_FOUND = "Proposal not found"
_NOT_WAITING = "This proposal has already been decided, withdrawn or closed"


async def propose(
    db: AsyncSession, *, species_uuid: uuid_pkg.UUID, user_id: int, values: SpeciesPhotoProposalCreate
) -> SpeciesPhotoProposalRead:
    """Propose a file for a species. Proposing again for the species replaces what waits."""
    title = normalize_file_title(values.file)
    if title is None:
        raise UnprocessableEntityException("That is not the title or page URL of a photograph on Wikimedia Commons.")

    # Locked so a pin or a hide committing meanwhile is either seen here or settles this row.
    species = (
        await db.execute(
            select(
                Species.id,
                Species.uuid,
                Species.scientific_name,
                Species.common_name,
                Species.photo_file,
                Species.photo_curation,
            )
            .where(Species.uuid == species_uuid)
            .with_for_update()
        )
    ).one_or_none()
    if species is None:
        raise NotFoundException("Species not found")
    if species.photo_curation is not None:
        raise HTTPException(status_code=409, detail="The operator has already chosen this species' photo")
    if species.photo_file is not None and normalize_file_title(species.photo_file) == title:
        raise HTTPException(status_code=409, detail="That file is already this species' photo")
    if await was_declined(db, species_id=species.id, file=title):
        raise HTTPException(status_code=409, detail="That file has already been declined for this species")

    await enforce_rate_limit(
        f"species-photo-proposal:user:{user_id}",
        settings.SPECIES_PHOTO_PROPOSAL_RATE_LIMIT_PER_USER,
        settings.SPECIES_PHOTO_PROPOSAL_RATE_LIMIT_WINDOW_SECONDS,
    )
    uuid, created_at = await write_waiting_proposal(
        db, user_id=user_id, species_id=species.id, file=title, note=values.note, now=datetime.now(UTC)
    )
    await db.commit()
    return SpeciesPhotoProposalRead(
        uuid=uuid,
        species=SpeciesPhotoProposalSpecies(
            uuid=species.uuid, scientific_name=species.scientific_name, common_name=species.common_name
        ),
        file=title,
        note=values.note,
        status=SpeciesPhotoProposalStatus.WAITING.value,
        reason=None,
        created_at=created_at,
        decided_at=None,
    )


async def locked_proposal(db: AsyncSession, *, uuid: uuid_pkg.UUID, user_id: int) -> SpeciesPhotoProposal | None:
    """One of this account's proposals, locked so a decision and a withdrawal cannot both find it
    waiting; `None` for one that does not exist or is someone else's."""
    proposal: SpeciesPhotoProposal | None = await db.scalar(
        select(SpeciesPhotoProposal)
        .where(SpeciesPhotoProposal.uuid == uuid, SpeciesPhotoProposal.user_id == user_id)
        .with_for_update()
    )
    return proposal


def _decide(proposal: SpeciesPhotoProposal, status: SpeciesPhotoProposalStatus, *, reason: str | None = None) -> None:
    now = datetime.now(UTC)
    proposal.status = status.value
    proposal.reason = reason
    proposal.decided_at = now
    proposal.updated_at = now


def _refuse_unless_waiting(proposal: SpeciesPhotoProposal) -> None:
    if proposal.status != SpeciesPhotoProposalStatus.WAITING.value:
        raise HTTPException(status_code=409, detail=_NOT_WAITING)


async def withdraw(db: AsyncSession, *, uuid: uuid_pkg.UUID, user_id: int) -> None:
    proposal = await locked_proposal(db, uuid=uuid, user_id=user_id)
    if proposal is None:
        raise NotFoundException(NOT_FOUND)
    _refuse_unless_waiting(proposal)
    _decide(proposal, SpeciesPhotoProposalStatus.WITHDRAWN)
    await db.commit()


async def waiting_for_review(db: AsyncSession, *, uuid: uuid_pkg.UUID) -> SpeciesPhotoProposal:
    """A proposal the operator may accept, read without a lock: accepting goes outbound next."""
    proposal = await reviewable_proposal(db, uuid=uuid)
    if proposal is None:
        raise NotFoundException(NOT_FOUND)
    _refuse_unless_waiting(proposal)
    return proposal


async def decline(db: AsyncSession, *, uuid: uuid_pkg.UUID, reason: str) -> int:
    """Decline a waiting proposal, answering its species' id. An empty reason is none."""
    proposal = await reviewable_proposal(db, uuid=uuid, for_update=True)
    if proposal is None:
        raise NotFoundException(NOT_FOUND)
    _refuse_unless_waiting(proposal)
    species_id = proposal.species_id
    _decide(proposal, SpeciesPhotoProposalStatus.DECLINED, reason=reason or None)
    await db.commit()
    return species_id
