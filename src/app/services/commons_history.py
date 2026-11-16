"""A Commons record's history: what each accepted proposal changed on it, and when.

An acceptance writes up to two entries, in the transaction that writes the record. The diver's,
from the record to what they asked; then the reviewer's correction, from what the diver asked to
what the reviewer wrote, wherever the two differ. Nothing else writes one, and nothing rewrites
one.

An entry stores no name. A name is read through the proposal, which goes with the proposer's
account, and through the reviewer's link, which empties when theirs is purged, so a rename, the
proposer's choice and a deletion request all show on the next read.
"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from uuid6 import uuid7

from ..models.dive_site_history import DiveSiteHistory
from ..models.dive_site_proposal import DiveSiteProposal
from ..models.user import User
from ..schemas.dive_site import DiveSiteMembersRead
from ..schemas.dive_site_history import AdminDiveSiteHistoryEntryRead, DiveSiteHistoryEntryRead
from .commons_records import SHARED_MEMBER_COLUMNS

# The decimals acceptance rounds a pin to, so a pin differing by less is the same pin.
PIN_DECIMALS = 6
_PIN = ("latitude", "longitude")


class DiveSiteHistoryKind(StrEnum):
    ADDED = "added"
    CHANGED = "changed"


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, PIN_DECIMALS)


def with_rounded_pin(members: Mapping[str, Any]) -> dict[str, Any]:
    """The members with the pin as acceptance writes it."""
    return {**members, **{coordinate: _rounded(members[coordinate]) for coordinate in _PIN}}


def differences(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each shared member whose value differs, in `SHARED_MEMBER_COLUMNS` order, its values as
    the shared-members read serialises them. The pin is one member: where either coordinate
    differs past the rounding, both are named."""
    old = DiveSiteMembersRead(**{column: before[column] for column in SHARED_MEMBER_COLUMNS}).model_dump(mode="json")
    new = DiveSiteMembersRead(**{column: after[column] for column in SHARED_MEMBER_COLUMNS}).model_dump(mode="json")
    pin_moved = any(_rounded(old[coordinate]) != _rounded(new[coordinate]) for coordinate in _PIN)
    return [
        {"field": column, "old": old[column], "new": new[column]}
        for column in SHARED_MEMBER_COLUMNS
        if (pin_moved if column in _PIN else old[column] != new[column])
    ]


async def record_acceptance(
    db: AsyncSession,
    *,
    site_id: int,
    reviewer_id: int,
    now: datetime,
    asked: Mapping[str, Any],
    written: Mapping[str, Any],
    proposal_id: int | None = None,
    proposer_kind: DiveSiteHistoryKind | None = None,
    before: Mapping[str, Any] | None = None,
) -> None:
    """Write the entries of one acceptance. `asked` is what the diver asked for and `written`
    what the reviewer wrote; for a fold into a record, where the diver proposed nothing on it,
    `asked` is the record and no `proposer_kind` is given. A `changed` entry of the diver's
    runs from `before`, the record as it stood. Does not commit.

    Two statements, in order, so the correction's id is the higher and sorts above the diver's
    entry it shares a moment with."""
    correction = differences(asked, written)
    entries: list[dict[str, Any]] = []
    if proposer_kind is DiveSiteHistoryKind.ADDED:
        entries.append({"kind": proposer_kind.value, "details": [], "proposal_id": proposal_id})
    elif proposer_kind is DiveSiteHistoryKind.CHANGED:
        assert before is not None
        if details := differences(before, asked):
            entries.append({"kind": proposer_kind.value, "details": details, "proposal_id": proposal_id})
    for entry in entries:
        entry["corrected"] = bool(correction)
    if correction:
        entries.append({"kind": DiveSiteHistoryKind.CHANGED.value, "details": correction, "corrected": None})

    for entry in entries:
        await db.execute(
            insert(DiveSiteHistory).values(
                uuid=uuid7(), dive_site_id=site_id, created_at=now, reviewer_id=reviewer_id, **entry
            )
        )


def _live_name(name: str | None, is_deleted: bool | None) -> str | None:
    """A name to show while its account stands: not once it is purged or asked to be deleted."""
    return name if name is not None and not is_deleted else None


async def read_history(db: AsyncSession, *, site_id: int, offset: int, limit: int, as_reviewer: bool) -> dict[str, Any]:
    """One page of a record's history, newest first, in `get_multi`'s shape. A reviewer reads
    the proposer's name whatever their choice, and who reviewed each diver's entry."""
    total_count = await db.scalar(
        select(func.count()).select_from(DiveSiteHistory).where(DiveSiteHistory.dive_site_id == site_id)
    )
    proposer = aliased(User)
    reviewer = aliased(User)
    rows = await db.execute(
        select(
            DiveSiteHistory.uuid,
            DiveSiteHistory.kind,
            DiveSiteHistory.created_at,
            DiveSiteHistory.details,
            DiveSiteHistory.corrected,
            DiveSiteProposal.show_proposer,
            proposer.name.label("proposer_name"),
            proposer.is_deleted.label("proposer_deleted"),
            reviewer.name.label("reviewer_name"),
            reviewer.is_deleted.label("reviewer_deleted"),
        )
        .outerjoin(DiveSiteProposal, DiveSiteProposal.id == DiveSiteHistory.proposal_id)
        .outerjoin(proposer, proposer.id == DiveSiteProposal.user_id)
        .outerjoin(reviewer, reviewer.id == DiveSiteHistory.reviewer_id)
        .where(DiveSiteHistory.dive_site_id == site_id)
        .order_by(DiveSiteHistory.created_at.desc(), DiveSiteHistory.id.desc())
        .offset(offset)
        .limit(limit)
    )
    data = []
    for row in rows:
        divers = row.corrected is not None
        proposer_name = _live_name(row.proposer_name, row.proposer_deleted)
        reviewer_name = _live_name(row.reviewer_name, row.reviewer_deleted)
        entry = {"uuid": row.uuid, "kind": row.kind, "created_at": row.created_at, "details": row.details}
        if not as_reviewer:
            made_by = (proposer_name if row.show_proposer else None) if divers else reviewer_name
            data.append(DiveSiteHistoryEntryRead(**entry, made_by=made_by).model_dump())
        elif divers:
            data.append(
                AdminDiveSiteHistoryEntryRead(
                    **entry,
                    made_by=proposer_name,
                    show_proposer=row.show_proposer,
                    reviewed_by=reviewer_name,
                    corrected=row.corrected,
                ).model_dump()
            )
        else:
            data.append(
                AdminDiveSiteHistoryEntryRead(
                    **entry, made_by=reviewer_name, show_proposer=None, reviewed_by=None, corrected=None
                ).model_dump()
            )
    return {"data": data, "total_count": total_count or 0}
