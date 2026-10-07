"""The check-in details: one object per diver, read and written on its own route.

Neither route is `@cache`d, as `GET /user` is not: nothing cached embeds the object, so a write
has nothing to invalidate.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...crud.crud_checkin_details import read_checkin_details, write_checkin_details
from ...schemas.checkin_details import CheckinDetailsRead, CheckinDetailsUpdate

router = APIRouter(tags=["user"])


@router.get("/user/checkin-details", response_model=CheckinDetailsRead)
async def read_own_checkin_details(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CheckinDetailsRead:
    """What the diver gives a dive shop's desk: the email they give out, a phone, a date of
    birth, their emergency contacts in call order and their insurance policies.

    An account that has saved nothing answers the empty object - nulls and empty lists -
    rather than a 404. The sign-in address is never here, and nothing defaults the object's
    `email` from it.
    """
    return await read_checkin_details(db, user_id=current_user["id"])


@router.patch("/user/checkin-details", response_model=CheckinDetailsRead)
async def patch_own_checkin_details(
    values: CheckinDetailsUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CheckinDetailsRead:
    """Replace the members the body carries, and answer the whole object as it now stands.

    A key present replaces its member: a scalar with its value, `null` clearing it; a list
    as a unit, in the order sent, `[]` clearing it. A key absent leaves its member alone, so
    a surface sends the group it edits and nothing else. A contact without a name or a policy
    without a provider is a 422 naming the row, and an optional text member sent blank is
    stored as null. Each list holds at most five rows.

    Saving the policies keeps a renewal reminder already sent for a policy whose provider
    and expiry are unchanged, and re-arms it for one whose expiry moved.
    """
    await write_checkin_details(db, user_id=current_user["id"], values=values)
    return await read_checkin_details(db, user_id=current_user["id"])
