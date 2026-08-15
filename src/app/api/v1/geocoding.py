"""Geocoding, proxied through this API rather than called from the browser.

Two endpoints over one provider (see `services.geocoding_service`), both authenticated and
both rate limited per caller. Neither is a resource in the sense the rest of `api/v1` uses
the word - there is nothing stored and nothing owned - so there is no uuid, no ownership
check, and no `@cache` decorator: the service caches in Redis under a global,
deliberately *not* user-scoped key, because the answer is a fact about the world rather
than about the caller.

Both degrade to "no result" rather than an error when the provider is unreachable. A diver
filling in a dive site can always type the location themselves, and a 502 here would make a
form look broken over an optional convenience.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ...api.dependencies import get_current_user
from ...core.config import settings
from ...core.utils.rate_limit import enforce_rate_limit
from ...schemas.geocoding import GeocodeResult
from ...services.geocoding_service import reverse_geocode, search_places

router = APIRouter(tags=["geocoding"])


async def _enforce_geocode_limit(user_id: int) -> None:
    """Per-user budget, shared by both endpoints, and the only thing here that can 429.

    Distinct from the provider's own one-per-second cap enforced inside the service: that
    one bounds what this instance does to a third party and *degrades* when it is hit,
    since it is global and one diver's search must not reject another's. This one bounds
    what a single account can make this instance do, so rejecting the account that spent it
    is exactly right.
    """
    await enforce_rate_limit(
        f"geocode:user:{user_id}",
        settings.GEOCODER_RATE_LIMIT_PER_USER,
        settings.GEOCODER_RATE_LIMIT_WINDOW_SECONDS,
    )


@router.get("/geocode/reverse", response_model=GeocodeResult | None)
async def read_reverse_geocode(
    current_user: Annotated[dict, Depends(get_current_user)],
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude in decimal degrees.")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude in decimal degrees.")],
) -> GeocodeResult | None:
    """Name the place at a position, so a dive site pinned on a map can offer a `location`.

    Answers `null` - not a 404 - when the position resolves to nothing or the provider
    cannot be reached: "we have no suggestion for you" is a normal outcome here, and a
    coordinate in open water is a perfectly valid place to dive.

    The position is rounded to roughly 110 m before being looked up, since the answer is a
    locality name and that is the resolution at which it stops changing.
    """
    await _enforce_geocode_limit(current_user["id"])
    return await reverse_geocode(lat, lon)


@router.get("/geocode/search", response_model=list[GeocodeResult])
async def read_geocode_search(
    current_user: Annotated[dict, Depends(get_current_user)],
    q: Annotated[str, Query(min_length=2, max_length=200, description="Free-text place search.")],
) -> list[GeocodeResult]:
    """Find places by name - "blue hole dahab" is how a diver looks for a site.

    Returns an empty list rather than an error for both "nothing matched" and "the provider
    is unavailable"; from the form's point of view those are the same thing, and the diver
    types the site in by hand either way.
    """
    await _enforce_geocode_limit(current_user["id"])
    return await search_places(q)
