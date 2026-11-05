import uuid as uuid_pkg
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from uuid6 import uuid7

from ..models.dive_site import DiveSite
from ..models.dive_site_proposal import DiveSiteProposal
from ..models.user import User
from ..schemas.dive_site import DiveSiteProposalKind, DiveSiteProposalState, DiveSiteProposalStatus
from ..schemas.dive_site_proposal import DiveSiteProposalRead, DiveSiteProposalSite
from ..services.commons_records import SHARED_MEMBER_COLUMNS

_WAITING = DiveSiteProposalStatus.WAITING.value
_READ_ON_THE_SITE = (DiveSiteProposalStatus.WAITING.value, DiveSiteProposalStatus.DECLINED.value)


async def get_latest_proposals_for_sites(
    db: AsyncSession, *, user_id: int, dive_site_ids: Sequence[int]
) -> dict[int, DiveSiteProposalState]:
    """This diver's latest proposal on each of these sites, where it waits or was declined. One
    that was accepted or withdrawn has nothing left to say on the site, so neither has an older
    decline behind it."""
    if not dive_site_ids:
        return {}
    rows = await db.execute(
        select(
            DiveSiteProposal.dive_site_id,
            DiveSiteProposal.uuid,
            DiveSiteProposal.kind,
            DiveSiteProposal.status,
            DiveSiteProposal.reason,
            DiveSiteProposal.created_at,
        )
        .where(DiveSiteProposal.user_id == user_id, DiveSiteProposal.dive_site_id.in_(set(dive_site_ids)))
        .ext(distinct_on(DiveSiteProposal.dive_site_id))
        .order_by(DiveSiteProposal.dive_site_id, DiveSiteProposal.id.desc())
    )
    return {
        row.dive_site_id: DiveSiteProposalState(
            uuid=row.uuid, kind=row.kind, status=row.status, reason=row.reason, created_at=row.created_at
        )
        for row in rows
        if row.status in _READ_ON_THE_SITE
    }


async def write_waiting_proposal(
    db: AsyncSession,
    *,
    user_id: int,
    dive_site_id: int,
    kind: DiveSiteProposalKind,
    members: dict[str, Any],
    attestation: str,
    message: str,
    now: datetime,
) -> tuple[uuid_pkg.UUID, datetime]:
    """Write this diver's waiting proposal on the site, replacing the one that waits if there is
    one, which keeps its place in the queue. Two at once leave one row. Does not commit."""
    replaced = {"members": members, "attestation": attestation, "attested_at": now, "message": message}
    statement = (
        insert(DiveSiteProposal)
        .values(
            user_id=user_id,
            dive_site_id=dive_site_id,
            kind=kind.value,
            status=_WAITING,
            uuid=uuid7(),
            created_at=now,
            **replaced,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "dive_site_id"],
            index_where=DiveSiteProposal.status == _WAITING,
            set_={**replaced, "updated_at": now},
        )
        .returning(DiveSiteProposal.uuid, DiveSiteProposal.created_at)
    )
    row = (await db.execute(statement)).one()
    return row.uuid, row.created_at


async def get_proposals_page(db: AsyncSession, *, user_id: int, offset: int, limit: int) -> dict[str, Any]:
    """One page of a diver's own proposals, newest first, in `get_multi`'s shape."""
    total_count = await db.scalar(
        select(func.count()).select_from(DiveSiteProposal).where(DiveSiteProposal.user_id == user_id)
    )
    rows = await db.execute(
        select(
            DiveSiteProposal.uuid,
            DiveSiteProposal.kind,
            DiveSiteProposal.status,
            DiveSiteProposal.reason,
            DiveSiteProposal.created_at,
            DiveSiteProposal.decided_at,
            DiveSite.uuid.label("site_uuid"),
            DiveSite.name.label("site_name"),
        )
        .join(DiveSite, DiveSite.id == DiveSiteProposal.dive_site_id)
        .where(DiveSiteProposal.user_id == user_id)
        .order_by(DiveSiteProposal.created_at.desc(), DiveSiteProposal.id.desc())
        .offset(offset)
        .limit(limit)
    )
    data = [
        DiveSiteProposalRead(
            uuid=row.uuid,
            kind=row.kind,
            status=row.status,
            reason=row.reason,
            site=DiveSiteProposalSite(uuid=row.site_uuid, name=row.site_name),
            created_at=row.created_at,
            decided_at=row.decided_at,
        ).model_dump()
        for row in rows
    ]
    return {"data": data, "total_count": total_count or 0}


def _decided(status: DiveSiteProposalStatus) -> Any:
    """How many of the proposer's proposals ended this way, for one row of the queue."""
    theirs = aliased(DiveSiteProposal)
    return (
        select(func.count())
        .select_from(theirs)
        .where(theirs.user_id == DiveSiteProposal.user_id, theirs.status == status.value)
        .correlate(DiveSiteProposal)
        .scalar_subquery()
    )


async def get_waiting_proposals_page(db: AsyncSession, *, offset: int, limit: int) -> dict[str, Any]:
    """One page of the queue, oldest first, in `get_multi`'s shape, its rows plain dicts.

    Of the site this selects the shared members and its uuid, and of the proposer the name: the
    queue reads a diver's private site, so a member not on that list cannot reach a reviewer.
    Nothing here joins the tables holding a diver's notes or tags. An account being deleted has
    gone dark, so its proposals leave the queue, and come back if it is restored.
    """
    waiting = (DiveSiteProposal.status == _WAITING, User.is_deleted.is_(False))
    total_count = await db.scalar(
        select(func.count())
        .select_from(DiveSiteProposal)
        .join(User, User.id == DiveSiteProposal.user_id)
        .where(*waiting)
    )
    rows = await db.execute(
        select(
            DiveSiteProposal.uuid,
            DiveSiteProposal.kind,
            DiveSiteProposal.message,
            DiveSiteProposal.created_at,
            DiveSiteProposal.members,
            DiveSiteProposal.dive_site_id,
            User.name.label("proposer_name"),
            _decided(DiveSiteProposalStatus.ACCEPTED).label("accepted_count"),
            _decided(DiveSiteProposalStatus.DECLINED).label("declined_count"),
            DiveSite.uuid.label("site_uuid"),
            *(getattr(DiveSite, column).label(f"site_{column}") for column in SHARED_MEMBER_COLUMNS),
        )
        .join(User, User.id == DiveSiteProposal.user_id)
        .join(DiveSite, DiveSite.id == DiveSiteProposal.dive_site_id)
        .where(*waiting)
        .order_by(DiveSiteProposal.created_at, DiveSiteProposal.id)
        .offset(offset)
        .limit(limit)
    )
    return {"data": [dict(row) for row in rows.mappings()], "total_count": total_count or 0}
