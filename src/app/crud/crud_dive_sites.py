from fastcrud import FastCRUD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive_site import DiveSite
from ..schemas.dive_site import (
    DiveSiteCreateInternal,
    DiveSiteDelete,
    DiveSiteRead,
    DiveSiteUpdate,
    DiveSiteUpdateInternal,
)

CRUDDiveSite = FastCRUD[
    DiveSite, DiveSiteCreateInternal, DiveSiteUpdate, DiveSiteUpdateInternal, DiveSiteDelete, DiveSiteRead
]
crud_dive_sites = CRUDDiveSite(DiveSite)


async def dive_site_ids_belong_to_user(db: AsyncSession, dive_site_ids: list[int], user_id: int) -> bool:
    """Check whether every given (non-deleted) dive site id belongs to the given user.

    Used to prevent a user from linking another user's dive site(s) to their own dive.
    """
    unique_ids = set(dive_site_ids)
    if not unique_ids:
        return True

    stmt = select(DiveSite.id).where(
        DiveSite.id.in_(unique_ids),
        DiveSite.user_id == user_id,
        DiveSite.is_deleted.is_(False),
    )
    result = await db.execute(stmt)
    matched_ids = {row[0] for row in result}
    return matched_ids == unique_ids


async def dive_site_name_exists(
    db: AsyncSession, user_id: int, name: str, location: str | None = None, exclude_id: int | None = None
) -> bool:
    """Case-insensitive check for whether a non-deleted dive site with the same (name, location)
    already exists for the user.

    Mirrors the `ux_dive_site_user_id_name_location_lower` partial unique index. Two sites with
    NULL location and the same name are treated as duplicates.
    """
    stmt = select(DiveSite.id).where(
        DiveSite.user_id == user_id,
        DiveSite.is_deleted.is_(False),
        func.lower(DiveSite.name) == name.strip().lower(),
    )
    if location is None:
        stmt = stmt.where(DiveSite.location.is_(None))
    else:
        stmt = stmt.where(func.lower(DiveSite.location) == location.strip().lower())
    if exclude_id is not None:
        stmt = stmt.where(DiveSite.id != exclude_id)

    result = await db.execute(stmt.limit(1))
    return result.first() is not None
