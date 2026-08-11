from fastcrud import FastCRUD
from sqlalchemy import select
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
