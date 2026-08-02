from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...crud.crud_user_dive_stats import crud_user_dive_stats
from ...crud.crud_users import crud_users
from ...schemas.user import UserRead
from ...schemas.user_dive_stats import UserDiveStatsRead

router = APIRouter(tags=["dive-stats"])


@router.get("/{username}/dive-stats", response_model=UserDiveStatsRead)
async def read_dive_stats(
        request: Request, username: str, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> UserDiveStatsRead:
    db_user = await crud_users.get(
        db=db, username=username, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if db_user is None:
        raise NotFoundException("User not found")

    db_user = cast(UserRead, db_user)
    stats = await crud_user_dive_stats.get(
        db=db, user_id=db_user.id, schema_to_select=UserDiveStatsRead, return_as_model=True
    )
    if stats is None:
        # No dives logged yet - return zeroed-out stats rather than 404, since
        # every user conceptually has stats, they just haven't been created yet.
        return UserDiveStatsRead(user_id=db_user.id, created_at=datetime.now(UTC))

    return cast(UserDiveStatsRead, stats)
