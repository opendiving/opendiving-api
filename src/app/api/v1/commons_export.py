from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.utils.client_ip import client_ip_bucket
from ...core.utils.rate_limit import enforce_rate_limit
from ...services.commons_records import load_bundle, read_commons_features

router = APIRouter(tags=["commons"])


@router.get("/commons/dive-sites")
async def read_commons_dive_sites(
    request: Request, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> dict[str, Any]:
    """Every OpenDiving Commons record, as a GeoJSON `FeatureCollection` of the records the
    repository publishes: a point, the name, other names, the locality's name, registry entries,
    depths, water type, altitude and entry types, with the record's source and licence under
    `extensions.opendivingcommons`. `licence`, `licence_url` and `notice` beside `features` are
    the terms the data comes on, and a copy of it keeps them.

    Anonymous, and answered only on the instance where the Commons is edited: 404 on any other.
    Rate limited per address, 429 past it. 503 when the instance cannot read the notice.
    """
    # The offer ODbL 4.6 asks of whoever publicly uses a database they alter. It reads the shared
    # members and no table holding a diver's notes or tags, so neither can reach it.
    if not settings.COMMONS_PRIMARY:
        raise NotFoundException("Not found")
    await enforce_rate_limit(
        f"commons-export:ip:{client_ip_bucket(request)}",
        settings.COMMONS_EXPORT_RATE_LIMIT_PER_IP,
        settings.COMMONS_EXPORT_RATE_LIMIT_WINDOW_SECONDS,
    )
    bundle = load_bundle()
    if bundle is None:
        raise HTTPException(status_code=503, detail="The Commons notice could not be read.")
    return {
        "type": "FeatureCollection",
        "licence": bundle.licence,
        "licence_url": bundle.licence_url,
        "notice": bundle.notice,
        "features": await read_commons_features(db),
    }
