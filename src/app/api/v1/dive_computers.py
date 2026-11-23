from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import UnprocessableEntityException
from ...schemas.dive_computer import DiveComputerListing, DiveComputerOrder, DiveComputerRead, KnownDive
from ...services.dive_computers import RepeatedSerialError, known_dives, list_dive_computers, set_order

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


@router.post("/dive-computers/known-dives", response_model=list[KnownDive])
async def read_dive_computer_known_dives(
    request: Request,
    listing: DiveComputerListing,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[KnownDive]:
    """Which of the dives a dive computer lists the caller's logbook already holds, so a
    client can leave those out before it downloads any.

    `serial` is the computer's, and `dives` the dives it lists, each with the uuid the client
    would give it and the start the computer lists for it. A listed dive is held when its uuid
    is a dive of the caller's that is not deleted, or one merged into such a dive, which then
    holds it; or when a dive of the caller's that is not deleted has a recording that names the
    serial, compared trimmed and case-folded, and that recording either started within two
    seconds of the listed start, or is one computer's two records of a dive merged into one
    and the listed start falls from its start through the end of its samples. Two starts are
    compared as instants where both carry a UTC offset, and as wall clocks where either does
    not. Nothing else holds a dive: not the same start under another serial, a recording that
    names no serial or no start, a deleted dive, or another account's. A dive the answer holds
    is one an import of its download adds as no new dive.

    Answers one row for each listed dive the logbook holds, in the order listed and each uuid
    once: the listed uuid, and the uuid of the dive that holds it. A listed dive that is not
    held has no row, so where none is the answer is an empty list. Writes nothing, and is not
    cached.

    An empty serial or one longer than 64 characters, more than 2,000 dives, a member the
    body does not declare, or a start that is a date alone is a 422.
    """
    # A POST because 2,000 uuids and starts do not fit a query string; it writes nothing. Not
    # cached: nothing embeds the answer, and every write that creates a recording would owe it
    # an invalidation.
    return await known_dives(db, user_id=current_user["id"], serial=listing.serial, dives=listing.dives)
