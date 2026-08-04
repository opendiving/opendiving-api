from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import ForbiddenException
from ...crud.crud_user_dive_stats import crud_user_dive_stats
from ...schemas.user_dive_stats import UserDiveStatsRead

router = APIRouter(tags=["dive-stats"])


@router.get("/dive-stats", response_model=UserDiveStatsRead)
async def read_dive_stats(
    request: Request,
    user_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> UserDiveStatsRead:
    if current_user["id"] != user_id:
        raise ForbiddenException()

    stats = await crud_user_dive_stats.get(
        db=db, user_id=user_id, schema_to_select=UserDiveStatsRead, return_as_model=True
    )
    if stats is None:
        # No dives logged yet - return zeroed-out stats rather than 404, since
        # every user conceptually has stats, they just haven't been created yet.
        # total_dives/max_depth/total_time/species_seen have Pydantic defaults, but mypy's
        # pydantic plugin doesn't recognize defaults declared via `Annotated[..., Field(default=...)]`.
        return UserDiveStatsRead(user_id=user_id, created_at=datetime.now(UTC))  # type: ignore[call-arg]

    return cast(UserDiveStatsRead, stats)
