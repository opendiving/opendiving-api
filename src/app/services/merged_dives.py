"""Where a dive's uuid stands once dives have been merged.

A merge soft-deletes the later dive and records on its row which dive it became part of
(`dive.merged_into_id`). To every read that row is deleted; to an import and to the
dive-computer list a merge is not a deletion, so its uuid resolves to the dive it joined.
"""

import uuid as uuid_pkg
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..models.dive import Dive


@dataclass(frozen=True, slots=True)
class MergeTarget:
    """The dive a uuid of the caller's resolves to through merges, and whether it is deleted."""

    id: int
    uuid: uuid_pkg.UUID
    is_deleted: bool


# Further than any diver merges one dive into the next; it only bounds the walk.
_MERGE_DEPTH = 64


async def resolve_merges(
    db: AsyncSession, *, user_id: int, uuids: Collection[uuid_pkg.UUID]
) -> dict[uuid_pkg.UUID, MergeTarget]:
    """Where each of these uuids of the caller's dives stands now: the dive itself, or, for one
    merged into another, the dive it was merged into, on through that dive's own merge.

    One query however many uuids are asked. A uuid that names none of the caller's dives has no
    entry; one whose walk ends at a deleted dive resolves to that dive, deleted.
    """
    if not uuids:
        return {}
    chain = (
        select(
            Dive.uuid.label("origin"),
            Dive.id,
            Dive.uuid,
            Dive.is_deleted,
            Dive.merged_into_id,
            literal(0).label("depth"),
        )
        .where(Dive.user_id == user_id, Dive.uuid.in_(set(uuids)))
        .cte("merge_chain", recursive=True)
    )
    joined = aliased(Dive)
    chain = chain.union_all(
        select(
            chain.c.origin,
            joined.id,
            joined.uuid,
            joined.is_deleted,
            joined.merged_into_id,
            chain.c.depth + 1,
        )
        .select_from(chain)
        .join(joined, joined.id == chain.c.merged_into_id)
        .where(chain.c.is_deleted.is_(True), joined.user_id == user_id, chain.c.depth < _MERGE_DEPTH)
    )
    rows = await db.execute(
        select(chain.c.origin, chain.c.id, chain.c.uuid, chain.c.is_deleted).where(
            or_(chain.c.merged_into_id.is_(None), chain.c.is_deleted.is_(False))
        )
    )
    return {row.origin: MergeTarget(id=row.id, uuid=row.uuid, is_deleted=row.is_deleted) for row in rows}
