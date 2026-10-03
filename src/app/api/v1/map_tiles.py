"""The map tiles every dive, trip and dive site card and page head is composed from, drawn by
the map renderer the first time any account asks for one and served from storage after that
(`services/map_tiles.py`).

Never `@cache`d, as no binary read is: Redis holds serialized responses, and the `ETag` does
this job in the browser.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.utils.uploads import content_disposition_attachment
from ...services.map_tiles import (
    CONTENT_TYPE,
    MAX_ZOOM,
    MapTilesOff,
    MapTileUnavailable,
    etag,
    find_or_draw,
    tile_at,
)

router = APIRouter(tags=["map-tiles"])

# `private` because the route answers only a signed-in caller, so no shared cache may answer
# it for anyone. Five minutes because the URL names no signature: a renderer redeployed is
# seen by a browser within that.
_KEPT = "private, max-age=300"

_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {CONTENT_TYPE: {}}, "description": "The tile, 1024x1024"},
    304: {"description": "The `If-None-Match` tile is still the current one"},
    404: {
        "description": f"No such tile - a theme other than `light` or `dark`, a zoom past {MAX_ZOOM}, a square "
        "outside the grid - or no renderer on this instance"
    },
    429: {"description": "This account has made too many tile requests, or started too many draws, in the window"},
    503: {"description": "The renderer failed, refused, or ran out of time; nothing was stored"},
}


@router.get("/map-tiles/{theme}/{z}/{x}/{y}", response_class=Response, responses=_RESPONSES)
async def read_map_tile(
    request: Request,
    theme: Annotated[str, Path(description="The basemap to draw in: the web app's resolved colour scheme")],
    z: Annotated[int, Path(description=f"The zoom, 0 to {MAX_ZOOM}")],
    x: Annotated[int, Path(description="The column, 0 to 2^z - 1, west to east")],
    y: Annotated[int, Path(description="The row, 0 to 2^z - 1, north to south")],
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> Response:
    """Square `z/x/y` of the Web Mercator grid in `theme`, 512 CSS px at pixel ratio 2, as a
    1024x1024 WebP with nothing of any record in it.

    For any signed-in account, with no owner to check: a tile is drawn from the basemap alone
    and stored once for the whole instance, so every account whose map covers it is served the
    same bytes. Drawn the first time anyone asks for it, so that request waits for the draw and
    every later one is the stored tile. The zoom stops where the web stops fitting a map.
    """
    tile = tile_at(theme, z, x, y)
    if tile is None:
        raise NotFoundException("No such map tile")
    try:
        served = await find_or_draw(
            db, user_id=current_user["id"], tile=tile, if_none_match=request.headers.get("if-none-match")
        )
    except MapTilesOff as exc:
        raise NotFoundException("This instance draws no map tiles") from exc
    except MapTileUnavailable as exc:
        # A raw `HTTPException`, as in `api/v1/support.py`: there is no 503 class to raise.
        raise HTTPException(status_code=503, detail="The map tile could not be drawn. Try again later.") from exc

    headers = {"ETag": etag(served.sha256), "Cache-Control": _KEPT}
    if served.data is None:
        return Response(status_code=304, headers=headers)
    return Response(
        content=served.data,
        media_type=CONTENT_TYPE,
        headers={
            **headers,
            "Content-Disposition": content_disposition_attachment("map-tile.webp", default="map-tile"),
            "X-Content-Type-Options": "nosniff",
            # As `_serve_picture`'s, `frame-ancestors` included: a response with its own policy
            # opts out of `SecurityHeadersMiddleware`'s default.
            "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
        },
    )
