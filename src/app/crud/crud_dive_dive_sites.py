from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_site import DiveSite
from ..schemas.dive import DiveSiteInfo

# The `dive_site` columns making up a `DiveSiteInfo` (the site summary embedded in a dive
# read), in the order `dive_site_info_from_row` unpacks them. Both loaders below select the
# same summary - one for a single dive, one batched - so the mapping lives here once
# instead of once per query. Same shape as `GEAR_ITEM_INFO_COLUMNS` in `crud_gear_items`,
# which is shared across modules; this pair has no caller outside this one.
DIVE_SITE_INFO_COLUMNS = (
    DiveSite.uuid,
    DiveSite.name,
    DiveSite.location,
    DiveSite.latitude,
    DiveSite.longitude,
)


def dive_site_info_from_row(row: Any) -> DiveSiteInfo:
    """Build a `DiveSiteInfo` from a result row selecting `DIVE_SITE_INFO_COLUMNS`.

    The one place a column is paired with a field, which matters most for the position:
    `latitude=row.longitude` is a valid float in a valid range, so a transposed pair would
    pass every schema check and place the pin in the wrong hemisphere.
    """
    return DiveSiteInfo(
        uuid=row.uuid,
        name=row.name,
        location=row.location,
        latitude=row.latitude,
        longitude=row.longitude,
    )


async def get_dive_sites_for_dive(db: AsyncSession, dive_id: int) -> list[DiveSiteInfo]:
    """Return the dive sites visited during a dive, in the order they were visited."""
    result = await db.execute(
        select(*DIVE_SITE_INFO_COLUMNS)
        .join(DiveDiveSite, DiveDiveSite.dive_site_id == DiveSite.id)
        .where(DiveDiveSite.dive_id == dive_id)
        .order_by(DiveDiveSite.position)
    )
    return [dive_site_info_from_row(row) for row in result]


async def get_dive_sites_for_dives(db: AsyncSession, dive_ids: list[int]) -> dict[int, list[DiveSiteInfo]]:
    """Batched version of `get_dive_sites_for_dive`, e.g. for a paginated dive listing."""
    sites_by_dive: dict[int, list[DiveSiteInfo]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return sites_by_dive

    result = await db.execute(
        select(DiveDiveSite.dive_id, *DIVE_SITE_INFO_COLUMNS)
        .join(DiveSite, DiveSite.id == DiveDiveSite.dive_site_id)
        .where(DiveDiveSite.dive_id.in_(dive_ids))
        .order_by(DiveDiveSite.dive_id, DiveDiveSite.position)
    )
    for row in result:
        sites_by_dive[row.dive_id].append(dive_site_info_from_row(row))
    return sites_by_dive


async def replace_dive_sites_for_dive(
    db: AsyncSession, dive_id: int, dive_site_ids: list[int], commit: bool = True
) -> None:
    """Replace all dive sites for a dive with the given ordered list.

    Duplicate ids are silently deduplicated (keeping each id's first occurrence,
    which determines its position) to avoid a unique-constraint violation.
    """
    unique_ids = list(dict.fromkeys(dive_site_ids))
    await db.execute(delete(DiveDiveSite).where(DiveDiveSite.dive_id == dive_id))
    for position, dive_site_id in enumerate(unique_ids):
        db.add(DiveDiveSite(dive_id=dive_id, dive_site_id=dive_site_id, position=position))
    if commit:
        await db.commit()
