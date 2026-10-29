"""The dive form's preview of a dive's surface conditions (`services.dive_conditions`).

Nothing stored and nothing owned is answered here - only the caller's dive site is read, for its
pin and water - so there is no `@cache`: the provider's answers are cached in Redis under a
global key, as the geocoder's are, and a per-user limit bounds what one account can spend of
the instance's budget.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from ...core.utils.rate_limit import enforce_rate_limit
from ...crud.crud_dive_sites import crud_dive_sites
from ...schemas.dive import DiveLocalStartTime, WaterType
from ...schemas.dive_conditions import DiveConditionsLookupRead
from ...schemas.dive_site import DiveSiteReadInternal
from ...services.dive_conditions import preview_conditions

router = APIRouter(tags=["dive-conditions"])


def _fetching_conditions() -> None:
    """Runs ahead of the sign-in check, so an instance that fetches no conditions answers every
    caller as a build without the route does."""
    if not settings.dive_conditions:
        raise NotFoundException("Not Found")


@router.get(
    "/dive-conditions/lookup",
    response_model=DiveConditionsLookupRead,
    dependencies=[Depends(_fetching_conditions)],
    responses={
        404: {"description": "This instance fetches no conditions, or the dive site is not the caller's."},
        503: {"description": "The provider could not be asked now; nothing is known about the inputs."},
    },
)
async def read_dive_conditions_lookup(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    start_time: Annotated[
        DiveLocalStartTime,
        Query(description="The dive's start as the form holds it: with an offset, without one, or a date alone"),
    ],
    dive_site_uuid: Annotated[
        uuid_pkg.UUID | None, Query(description="The primary site, for its pin and its water type")
    ] = None,
    latitude: Annotated[
        float | None, Query(ge=-90, le=90, description="A position that wins over the site's pin")
    ] = None,
    longitude: Annotated[float | None, Query(ge=-180, le=180, description="Longitude of that position")] = None,
    water_type: Annotated[WaterType | None, Query(description="Wins over the site's")] = None,
) -> DiveConditionsLookupRead:
    """What saving a dive with these inputs would fill into its empty air temperature, weather
    and waves - the values the instance fetches after a save and marks as fetched, derived the
    same way, for the dive form to show while the diver edits.

    A preview only: nothing is written, and the form sends the previewed fields blank on save so
    the instance fills and marks them itself. The position is `latitude` and `longitude` where
    given - a dive's own entry fix - else the site's pin; the two are given together or not at
    all, else a 422. Fresh water asks nothing of the sea. Rate limited per account.

    `200` with `at`, `utc_offset_minutes` and every `filled` member null where the inputs cannot
    be answered - no position, a date alone, a day after today or before 1940, a request the
    provider refused for them - so the form empties what it filled. `503` when the provider could not be asked,
    which says nothing about the inputs: over the instance's budget, cooling down, slow or down.
    404 when this instance fetches no conditions, or when `dive_site_uuid` is not the caller's.
    """
    if (latitude is None) != (longitude is None):
        raise UnprocessableEntityException("latitude and longitude are given together or not at all")
    await enforce_rate_limit(
        f"dive-conditions-lookup:user:{current_user['id']}",
        settings.DIVE_CONDITIONS_LOOKUP_RATE_LIMIT_PER_USER,
        settings.DIVE_CONDITIONS_LOOKUP_RATE_LIMIT_WINDOW_SECONDS,
    )
    site_water: str | None = None
    if dive_site_uuid is not None:
        site = await fetch_owned_or_raise(
            db=db,
            crud=crud_dive_sites,
            uuid=dive_site_uuid,
            current_user=current_user,
            schema=DiveSiteReadInternal,
            not_found_message="Dive site not found",
        )
        site_water = site.water_type
        if latitude is None and site.latitude is not None and site.longitude is not None:
            latitude, longitude = site.latitude, site.longitude

    preview = await preview_conditions(
        db,
        start_time=start_time,
        latitude=latitude,
        longitude=longitude,
        water_type=water_type or site_water,
        now=datetime.now(UTC),
    )
    if preview is None:
        # Raw `HTTPException`, as `api/v1/health.py` does: there is no 503 class to raise.
        raise HTTPException(status_code=503, detail="Conditions could not be looked up now. Try again later.")
    return preview
