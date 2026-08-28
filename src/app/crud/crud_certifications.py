from typing import Any

from fastcrud import FastCRUD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.certification import Certification
from ..schemas.certification import (
    CertificationCreateInternal,
    CertificationDelete,
    CertificationExpiringItem,
    CertificationReadInternal,
    CertificationUpdate,
    CertificationUpdateInternal,
)

CRUDCertification = FastCRUD[
    Certification,
    CertificationCreateInternal,
    CertificationUpdate,
    CertificationUpdateInternal,
    CertificationDelete,
    CertificationReadInternal,
]
crud_certifications = CRUDCertification(Certification)

# No `resolve_*_ids_for_user` / `get_*_uuids_by_id` helpers here, unlike
# `crud_gear_items`. Those exist because dives and gear sets reference gear by uuid and
# have to translate in both directions; nothing references a certification, so every
# lookup is the plain `crud_certifications.get(uuid=...)` the routes already do. The
# file rows that *do* hang off a certification are reached by its internal `id`, which
# the route already holds from that same lookup - see `services/certification_files.py`.
#
# A certification now references something itself - `certification.course_id` - but that
# is the other direction and needs no helper here: `crud_courses` owns both translations,
# and the certification routes call them.


# The certification list's order: newest card first, cards with no date at all last.
# Matches `ix_certification_user_id_certified_on`, whose `certified_on DESC NULLS LAST`
# can only serve a query that asks for the same null placement.
#
# `NULLS LAST` has to be spelled out because Postgres's default for `DESC` is `NULLS
# FIRST` - a bare `desc()` floats every dateless card above the diver's most recent one,
# which is both the wrong answer and unservable by that index. It is written here rather
# than passed to `get_multi`, whose `sort_orders` is 'asc'/'desc' and cannot express null
# placement at all. Same shape as `_INFO_ORDER` in `crud_gear_service_schedules`.
#
# `uuid` breaks ties: it is uuid7, so it orders by creation time, which keeps pagination
# stable across pages when several cards share a date (or have none).
_LIST_ORDER = (Certification.certified_on.desc().nulls_last(), Certification.uuid.desc())


async def get_certifications_page(
    db: AsyncSession, *, user_id: int, offset: int, limit: int, course_id: int | None = None
) -> dict[str, Any]:
    """One page of a diver's certifications, newest first, in the same
    `{"data": [...], "total_count": n}` shape `crud.get_multi` returns.

    Hand-written for `_LIST_ORDER` alone - `get_multi` cannot ask for `NULLS LAST`. Rows
    come back as plain dicts of every table column, matching `get_multi` called without a
    `schema_to_select`, so the caller still reads the internal `id` it needs to batch its
    card-file and course lookups. Mirrors `search_multi` in `core/utils/search.py`, the
    other place a list query outgrew `get_multi`.

    `course_id` narrows the page to the cards one training course issued, which is what a
    course's own page reads. It is the internal id rather than the public uuid because the
    route resolves that once and puts the same value in the cache key.
    """
    conditions = (Certification.user_id == user_id, Certification.is_deleted.is_(False))
    if course_id is not None:
        conditions += (Certification.course_id == course_id,)

    total_count = await db.scalar(select(func.count()).select_from(Certification).where(*conditions))
    rows = (
        await db.execute(
            select(*Certification.__table__.columns)
            .where(*conditions)
            .order_by(*_LIST_ORDER)
            .offset(offset)
            .limit(limit)
        )
    ).mappings()

    return {"data": [dict(row) for row in rows], "total_count": total_count or 0}


async def get_expiring_overview_for_user(
    db: AsyncSession, user_id: int, limit: int
) -> tuple[list[CertificationExpiringItem], bool]:
    """Every certification a user owns that carries an expiry date, soonest first.
    Returns `(rows, truncated)`.

    The gear twin is `get_due_overview_for_user`, and this follows it deliberately:
    unfiltered by any date horizon, because baking "today" into the query bakes it into
    the cached response too, which then goes quietly wrong at midnight. The caller
    buckets into expiring-soon/expired itself (see `CertificationExpiringResponse`).

    Cards with no `expires_on` are excluded outright rather than sorted last - most
    recreational certifications never expire, so for a typical diver that is most of the
    list, and none of them can ever appear on a renewals card.

    Selects one row past `limit` so `truncated` is exact; the extra row is dropped.
    """
    result = await db.execute(
        select(
            Certification.uuid,
            Certification.agency,
            Certification.agency_other,
            Certification.name,
            Certification.expires_on,
        )
        .where(
            Certification.user_id == user_id,
            Certification.is_deleted.is_(False),
            Certification.expires_on.is_not(None),
        )
        .order_by(Certification.expires_on.asc(), Certification.name)
        .limit(limit + 1)
    )
    rows = [CertificationExpiringItem.model_validate(row, from_attributes=True) for row in result]
    return rows[:limit], len(rows) > limit
