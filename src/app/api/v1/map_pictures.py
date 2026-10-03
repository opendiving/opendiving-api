"""A dive card's, a trip card's and a dive site card's map picture, drawn by the map renderer
the first time a card asks and served from storage after that (`services/map_pictures.py`).

Never `@cache`d, as no binary read is: Redis holds serialized responses, and the `ETag` does
this job in the browser.
"""

import uuid as uuid_pkg
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.utils.uploads import content_disposition_attachment
from ...crud.crud_dive_dive_sites import get_dive_sites_for_dive
from ...crud.crud_dive_sites import crud_dive_sites
from ...crud.crud_dives import crud_dives
from ...crud.crud_trip_parts import get_parts_for_trip
from ...crud.crud_trips import crud_trips
from ...schemas.dive import DiveReadInternal
from ...schemas.dive_site import DiveSiteReadInternal
from ...schemas.map_picture import MapTheme
from ...schemas.trip import TripReadInternal
from ...services.map_pictures import (
    CONTENT_TYPE,
    FIX_FIELDS,
    MapPicturesOff,
    MapPictureUnavailable,
    dive_payload,
    dive_site_payload,
    etag,
    find_or_draw,
    trip_payload,
)

router = APIRouter()

# One diver's places, so no shared cache keeps a copy; the browser keeps it for five minutes,
# as it keeps a profile picture, and only under the URL naming the picture's current digest.
_KEPT = "private, max-age=300"
_NOT_KEPT = "private, no-store"

_THEME = Query(description="The basemap to draw in: the web app's resolved colour scheme")
_VERSION = Query(
    description="The record's `map_picture`. It decides only whether the browser may keep the answer: the picture "
    "served is always the record's current one"
)
_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {CONTENT_TYPE: {}}, "description": "The picture, 2048x1024"},
    304: {"description": "The `If-None-Match` picture is still the current one"},
    404: {"description": "No such record of the caller's, nothing in it to draw, or no renderer on this instance"},
    429: {"description": "This account has started too many draws in the window"},
    503: {"description": "The renderer failed, refused, or ran out of time; nothing was stored"},
}


async def _respond(
    request: Request, db: AsyncSession, *, user_id: int, payload: dict[str, Any], theme: MapTheme, v: str | None
) -> Response:
    try:
        served = await find_or_draw(
            db, user_id=user_id, payload=payload, theme=theme, if_none_match=request.headers.get("if-none-match")
        )
    except MapPicturesOff as exc:
        raise NotFoundException("This instance draws no map pictures") from exc
    except MapPictureUnavailable as exc:
        # A raw `HTTPException`, as in `api/v1/support.py`: there is no 503 class to raise.
        raise HTTPException(status_code=503, detail="The map picture could not be drawn. Try again later.") from exc

    headers = {"ETag": etag(served.sha256), "Cache-Control": _KEPT if v == served.digest else _NOT_KEPT}
    if served.data is None:
        return Response(status_code=304, headers=headers)
    return Response(
        content=served.data,
        media_type=CONTENT_TYPE,
        headers={
            **headers,
            "Content-Disposition": content_disposition_attachment("map.webp", default="map"),
            "X-Content-Type-Options": "nosniff",
            # As `_serve_picture`'s, `frame-ancestors` included: a response with its own policy
            # opts out of `SecurityHeadersMiddleware`'s default.
            "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
        },
    )


@router.get("/dive/{uuid}/map-picture", tags=["dives"], response_class=Response, responses=_RESPONSES)
async def read_dive_map_picture(
    request: Request,
    uuid: uuid_pkg.UUID,
    theme: Annotated[MapTheme, _THEME],
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[str | None, _VERSION] = None,
) -> Response:
    """The map behind the dive's card: its sites and its entry and exit fixes, pinned, in
    `theme`, as a 2048x1024 WebP.

    Drawn the first time it is asked for and stored, so the first request waits for the draw
    and every later one is the stored picture. The picture served is named by the dive's
    places as they are now, never by `v`: pass the list's `map_picture` there and the browser
    keeps the answer for five minutes; anything else is served the current picture uncached.
    404 for a dive that is not the caller's, as for one that does not exist, and for one with
    no site with a position and no fix - its list row says `map_picture: null`.
    """
    dive = await fetch_owned_or_raise(
        db=db,
        crud=crud_dives,
        uuid=uuid,
        current_user=current_user,
        schema=DiveReadInternal,
        not_found_message="Dive not found",
    )
    sites = await get_dive_sites_for_dive(db=db, dive_id=dive.id)
    payload = dive_payload(
        {**dive.model_dump(include=set(FIX_FIELDS)), "dive_sites": [site.model_dump() for site in sites]}
    )
    if payload is None:
        raise NotFoundException("This dive has no position to draw")
    return await _respond(request, db, user_id=current_user["id"], payload=payload, theme=theme, v=v)


@router.get("/trip/{uuid}/map-picture", tags=["trips"], response_class=Response, responses=_RESPONSES)
async def read_trip_map_picture(
    request: Request,
    uuid: uuid_pkg.UUID,
    theme: Annotated[MapTheme, _THEME],
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[str | None, _VERSION] = None,
) -> Response:
    """The map behind the trip's card: its parts' places, pinned, in `theme`, as a 2048x1024
    WebP; the whole world for a trip with no place.

    Drawn and kept as `GET /dive/{uuid}/map-picture` is, with the same answer to `v`. 404 for
    a trip that is not the caller's, as for one that does not exist.
    """
    trip = await fetch_owned_or_raise(
        db=db,
        crud=crud_trips,
        uuid=uuid,
        current_user=current_user,
        schema=TripReadInternal,
        not_found_message="Trip not found",
    )
    parts = await get_parts_for_trip(db=db, trip_id=trip.id)
    payload = trip_payload({"parts": [part.model_dump() for part in parts]})
    return await _respond(request, db, user_id=current_user["id"], payload=payload, theme=theme, v=v)


@router.get("/dive-site/{uuid}/map-picture", tags=["dive-sites"], response_class=Response, responses=_RESPONSES)
async def read_dive_site_map_picture(
    request: Request,
    uuid: uuid_pkg.UUID,
    theme: Annotated[MapTheme, _THEME],
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[str | None, _VERSION] = None,
) -> Response:
    """The map behind the dive site's card: the site, pinned, in `theme`, as a 2048x1024 WebP.

    Drawn as a dive at that one site recording no fix, so the two share one stored picture,
    and kept as `GET /dive/{uuid}/map-picture` is, with the same answer to `v`. 404 for a
    site that is not the caller's, as for one that does not exist, and for one with no
    position - its list row says `map_picture: null`.
    """
    site = await fetch_owned_or_raise(
        db=db,
        crud=crud_dive_sites,
        uuid=uuid,
        current_user=current_user,
        schema=DiveSiteReadInternal,
        not_found_message="Dive site not found",
    )
    payload = dive_site_payload(site.model_dump(include={"latitude", "longitude"}))
    if payload is None:
        raise NotFoundException("This dive site has no position to draw")
    return await _respond(request, db, user_id=current_user["id"], payload=payload, theme=theme, v=v)
