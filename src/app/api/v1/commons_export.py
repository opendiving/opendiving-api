from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.utils.binary_responses import not_modified
from ...core.utils.client_ip import client_ip_bucket
from ...core.utils.rate_limit import enforce_rate_limit
from ...schemas.dive_site_search import CommonsDiveSitePoints
from ...services.commons_points import commons_points_body, commons_points_etag, commons_points_version
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


# Revalidated on every use: the version is one aggregate over the Commons rows, and a held copy
# answered by a 304 costs the client nothing but that.
_POINTS_CACHE_CONTROL = "private, no-cache"


@router.get(
    "/commons/dive-sites/points",
    response_model=CommonsDiveSitePoints,
    responses={304: {"description": "The copy the client holds under this `ETag` is current."}},
)
async def read_commons_dive_site_points(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> Response:
    """Every OpenDiving Commons record with a pin, as `[longitude, latitude, uuid, name, source]`,
    with `sources` giving the credit each source asks for, to show wherever a record is shown. A
    record with no pin is left out.

    One document, the same bytes for every account on the instance, on any instance. `version`
    moves whenever a record is added, changed or reviewed, and a change reaches the document by
    the next request. The `ETag` names the version: a request sending it in `If-None-Match` is
    answered 304 until the version moves or the instance is upgraded.
    """
    version = await commons_points_version(db)
    etag = commons_points_etag(version)
    if request.headers.get("if-none-match") == etag:
        return not_modified(etag, cache_control=_POINTS_CACHE_CONTROL)
    body = await commons_points_body(db, version)
    return Response(
        content=body, media_type="application/json", headers={"ETag": etag, "Cache-Control": _POINTS_CACHE_CONTROL}
    )
