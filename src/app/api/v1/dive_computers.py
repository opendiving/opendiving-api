from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import UnprocessableEntityException
from ...schemas.dive_computer import DiveComputerOrder, DiveComputerRead
from ...services.dive_computers import RepeatedSerialError, list_dive_computers, set_order

router = APIRouter(tags=["dive-computers"])


@router.get("/dive-computers", response_model=list[DiveComputerRead])
async def read_dive_computers(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[DiveComputerRead]:
    """The caller's dive computers, first to last.

    A dive computer is a serial: each one a recording of the caller's dives names, and each
    one the caller has placed in the order before any recording of it arrived. Two serials
    equal once trimmed and case-folded are one computer, and a recording that names no serial
    is in no row. A row is labelled from the latest recording of its serial; one no recording
    names yet has only its serial.

    The order is the one in force: the computers the caller placed with
    `PUT /dive-computers/order`, in that order, then every other, in the order the account
    first received a recording from each. Where two of them recorded one dive, the recording
    of the one higher in this list is shown by default when the second arrives. `position` is
    the row's index, 0 for the first. An account with no computer answers an empty list. Not
    cached and not paged.
    """
    # Not cached: nothing embeds the list, and caching it would owe an invalidation from every
    # write that creates a recording.
    return await list_dive_computers(db, user_id=current_user["id"])


@router.put("/dive-computers/order", response_model=list[DiveComputerRead])
async def write_dive_computer_order(
    request: Request,
    order: DiveComputerOrder,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[DiveComputerRead]:
    """Replace the caller's order of their dive computers, answering the list as
    `GET /dive-computers` then reads it.

    `serials` is the whole order, first to last; a computer it leaves out comes after the ones
    it names, as one never placed does. A serial no recording of the caller's names yet is
    kept, so a computer can be placed before its first dive is uploaded. A serial that is
    blank, or that repeats another once both are trimmed and case-folded, is a 422.

    Changing the order changes no dive already stored: it decides which recording is shown by
    default only when a later upload adds a second computer's recording to a dive.
    """
    try:
        await set_order(db, user_id=current_user["id"], serials=order.serials)
    except RepeatedSerialError as exc:
        raise UnprocessableEntityException(str(exc)) from exc
    await db.commit()
    return await list_dive_computers(db, user_id=current_user["id"])
