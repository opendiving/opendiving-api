"""`GET /dive-sites/search`: the site tiers from the database, the places from the geocoder, and
where on the pages the places go.

The geocoder is asked only by the page that carries the places, which the database's counts
decide, so a page deep in the Commons tier spends neither a provider call nor the diver's
geocoder budget. It is asked as the place-search route asks it - the same call, cache and
per-provider slot - and charged to the same per-account budget, which a spent budget answers by
costing the places rather than the search. The connection goes back to the pool before the wait
and the place counts take a transaction of their own after it (*"The read transaction is released
before either endpoint goes outbound"* in `DECISIONS.md`).
"""

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db.database import release_read_transaction
from ..core.utils.rate_limit import claim_rate_limit_slot
from ..crud.crud_dive_site_search import Box, count_sites_near, get_site_search_page
from ..crud.crud_dive_sites import EARTH_RADIUS_M
from ..schemas.dive_site import DiveSiteListSort
from ..schemas.geocoding import GeocodeResult
from .geocoding_service import (
    SEARCH_QUERY_MAX_LENGTH,
    SEARCH_QUERY_MIN_LENGTH,
    caller_budget_key,
    place_search_enabled,
    search_places,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SiteSearch:
    term: str | None
    camera: tuple[float, float] | None
    box: Box | None
    my: bool
    until: datetime | None
    tag_id: int | None
    commons_match: bool
    sort: DiveSiteListSort | None
    page: int
    items_per_page: int


@dataclass(frozen=True)
class FoundPlace:
    place: GeocodeResult
    distance_m: float | None
    sites_nearby: int


@dataclass
class SiteSearchResult:
    sites: list[dict[str, Any]]
    # The places, and the index in `sites` they go before.
    places: list[FoundPlace] = field(default_factory=list)
    places_at: int = 0
    has_more: bool = False
    total_count: int = 0


def _metres_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat_a, lon_a, lat_b, lon_b = map(math.radians, (*a, *b))
    half_chord = (
        math.sin((lat_b - lat_a) / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin((lon_b - lon_a) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(half_chord)))


def _box_of(place: GeocodeResult) -> Box | None:
    if place.bbox_south is None or place.bbox_north is None or place.bbox_west is None or place.bbox_east is None:
        return None
    return Box(place.bbox_south, place.bbox_north, place.bbox_west, place.bbox_east)


async def _places_within(task: asyncio.Task[list[GeocodeResult]], seconds: float) -> list[GeocodeResult]:
    """What the geocoder answered within `seconds`, or nothing. Never raises: the places are the
    one part of the answer that may go missing."""
    try:
        return await asyncio.wait_for(task, timeout=max(0.0, seconds))
    except TimeoutError:
        logger.info("A dive-site search answered without places: the geocoder took longer than its budget.")
        return []
    except Exception:
        logger.warning("A dive-site search answered without places: the place search failed.", exc_info=True)
        return []


async def search_dive_sites(db: AsyncSession, *, user_id: int, search: SiteSearch) -> SiteSearchResult:
    offset = (search.page - 1) * search.items_per_page
    fuzzy = search.term is not None and len(search.term) >= SEARCH_QUERY_MIN_LENGTH
    found = await get_site_search_page(
        db,
        user_id=user_id,
        term=search.term,
        camera=search.camera,
        box=search.box,
        my=search.my,
        until=search.until,
        tag_id=search.tag_id,
        commons_match=search.commons_match,
        sort=search.sort,
        fuzzy=fuzzy,
        offset=offset,
        limit=search.items_per_page,
    )
    result = SiteSearchResult(
        sites=found.rows,
        has_more=offset + len(found.rows) < found.total_count,
        total_count=found.total_count,
    )
    if not fuzzy or search.term is None or len(search.term) > SEARCH_QUERY_MAX_LENGTH or not place_search_enabled():
        return result

    # The places follow the rows before the typo tier - every site row, with `sort` - on the page
    # where those end, or on the first page where there are none.
    boundary = found.total_count if search.sort is not None else found.matched_count
    if search.page != max(1, math.ceil(boundary / search.items_per_page)):
        return result
    if not await claim_rate_limit_slot(
        caller_budget_key(user_id), settings.GEOCODER_RATE_LIMIT_PER_USER, settings.GEOCODER_RATE_LIMIT_WINDOW_SECONDS
    ):
        return result

    # Released before the task exists: a task starts at the next await, which inside the release
    # would be with the connection still out.
    await release_read_transaction(db)
    asking = asyncio.create_task(search_places(search.term))
    try:
        places = await _places_within(asking, settings.DIVE_SITE_SEARCH_GEOCODER_BUDGET_SECONDS)
    finally:
        asking.cancel()

    counted = [
        FoundPlace(
            place=place,
            distance_m=None
            if search.camera is None
            else _metres_between(search.camera, (place.latitude, place.longitude)),
            sites_nearby=await count_sites_near(
                db, user_id=user_id, latitude=place.latitude, longitude=place.longitude, box=_box_of(place)
            ),
        )
        for place in places
    ]
    # Stable, so without a camera the geocoder's own order stands within each group.
    counted.sort(key=lambda found_place: (found_place.sites_nearby == 0, found_place.distance_m or 0.0))
    result.places = counted
    result.places_at = min(max(0, boundary - offset), len(found.rows))
    return result
