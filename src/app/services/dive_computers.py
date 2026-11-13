"""An account's dive computers, in the diver's order, and the recording that order shows.

**Which computers the account has is what its recordings name**, by serial, folded as the
recording gates fold it; the diver's own choice is one list of serials on the user row. The
order in force is the whole list `GET /dive-computers` answers: the serials the diver placed,
in their order - a serial no recording names yet among them - then every other, in the order
the account first received a recording from each. So a client that shows the list shows what
decides, before the diver has ever set it.

**A write that adds a computer's recording to a dive ends as if the diver had then pressed
*Show by default***, where that computer stands before the one whose recording the dive shows
(`show_first_by_default`). Nothing reads the order afterwards, so changing it rewrites no
dive, and a diver's own choice on a dive stands until a later write adds another recording.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive import Dive
from ..models.dive_recording import DiveRecording
from ..models.user import User
from ..schemas.dive_computer import DiveComputerRead
from .dive_recordings import folded_serial, make_primary


class RepeatedSerialError(ValueError):
    """An order that names one computer twice."""


@dataclass(slots=True)
class _Recorded:
    """One computer as the account's recordings name it, under one of its spellings or all."""

    label: tuple[str, str | None, str | None, str | None]
    latest: tuple[datetime, int]
    first: tuple[datetime, int]


async def _recorded(db: AsyncSession, *, user_id: int) -> dict[str, _Recorded]:
    """Every computer the caller's live dives' recordings name, by folded serial: labelled from
    the latest recording that names it, and placed by the first.

    One row per spelling of a serial from Postgres - a handful - and folded here, since the
    fold is Python's `casefold`, which no SQL function spells the same.
    """
    window: dict[str, Any] = {
        "partition_by": DiveRecording.device_serial,
        "order_by": (DiveRecording.created_at, DiveRecording.id),
    }
    rows = await db.execute(
        select(
            DiveRecording.device_serial,
            DiveRecording.device_brand,
            DiveRecording.device_model,
            DiveRecording.device_name,
            DiveRecording.created_at,
            DiveRecording.id,
            func.first_value(DiveRecording.created_at).over(**window).label("first_at"),
            func.first_value(DiveRecording.id).over(**window).label("first_id"),
        )
        .join(Dive, Dive.id == DiveRecording.dive_id)
        .where(
            DiveRecording.user_id == user_id,
            DiveRecording.device_serial.is_not(None),
            Dive.is_deleted.is_(False),
        )
        .ext(distinct_on(DiveRecording.device_serial))
        .order_by(DiveRecording.device_serial, DiveRecording.created_at.desc(), DiveRecording.id.desc())
    )
    computers: dict[str, _Recorded] = {}
    for row in rows:
        serial = folded_serial(row.device_serial)
        if serial is None:
            continue
        spelling = _Recorded(
            label=(row.device_serial, row.device_brand, row.device_model, row.device_name),
            latest=(row.created_at, row.id),
            first=(row.first_at, row.first_id),
        )
        known = computers.get(serial)
        if known is None:
            computers[serial] = spelling
            continue
        if spelling.latest > known.latest:
            known.label, known.latest = spelling.label, spelling.latest
        known.first = min(known.first, spelling.first)
    return computers


async def stored_order(db: AsyncSession, *, user_id: int) -> list[str]:
    return list((await db.execute(select(User.dive_computer_order).where(User.id == user_id))).scalar_one())


def _in_order(order: list[str], recorded: dict[str, _Recorded]) -> list[str]:
    """The folded serials of the order in force, first to last."""
    placed = set(order)
    rest = sorted((serial for serial in recorded if serial not in placed), key=lambda serial: recorded[serial].first)
    return [*order, *rest]


async def list_dive_computers(db: AsyncSession, *, user_id: int) -> list[DiveComputerRead]:
    """The account's dive computers in the order in force."""
    recorded = await _recorded(db, user_id=user_id)
    computers = []
    for position, serial in enumerate(_in_order(await stored_order(db, user_id=user_id), recorded)):
        one = recorded.get(serial)
        spelled, brand, model, name = (serial, None, None, None) if one is None else one.label
        computers.append(DiveComputerRead(serial=spelled, brand=brand, model=model, name=name, position=position))
    return computers


def folded_order(serials: list[str]) -> list[str]:
    """An order as it is stored: each serial folded, and each once. Raises
    `RepeatedSerialError` for a serial that is blank once folded or named twice."""
    order: list[str] = []
    for serial in serials:
        one = folded_serial(serial)
        if one is None:
            raise RepeatedSerialError("A serial must not be blank.")
        if one in order:
            raise RepeatedSerialError(f"The serial {serial!r} is in the order more than once.")
        order.append(one)
    return order


async def set_order(db: AsyncSession, *, user_id: int, serials: list[str]) -> None:
    """Replace the account's stored order. Does not commit."""
    await db.execute(update(User).where(User.id == user_id).values(dive_computer_order=folded_order(serials)))


async def show_first_by_default(db: AsyncSession, *, user_id: int, dive_id: int, recording_id: int) -> int | None:
    """Move a recording just added to a dive to the front, where its computer stands before the
    one whose recording the dive shows - which is what *Show by default* does, and nothing more.

    Both have to carry a serial, and the order compared is the one in force with this recording
    already written: a serial the diver placed keeps its place whether or not a recording of it
    existed before, and one they did not, first seen now, comes last. Returns the ordinal the
    recording held where it moved, and `None` where it stayed.
    """
    rows = {
        row.ordinal: row
        for row in await db.execute(
            select(DiveRecording.id, DiveRecording.ordinal, DiveRecording.device_serial).where(
                DiveRecording.dive_id == dive_id
            )
        )
    }
    added = next((row for row in rows.values() if row.id == recording_id), None)
    shown = rows.get(0)
    if added is None or shown is None or added.ordinal == 0:
        return None
    incoming, current = folded_serial(added.device_serial), folded_serial(shown.device_serial)
    if incoming is None or current is None or incoming == current:
        return None

    placed = await stored_order(db, user_id=user_id)
    if incoming in placed or current in placed:
        first = incoming in placed and (current not in placed or placed.index(incoming) < placed.index(current))
    else:
        recorded = await _recorded(db, user_id=user_id)
        first = incoming in recorded and current in recorded and recorded[incoming].first < recorded[current].first
    if not first:
        return None
    await make_primary(db, recording_id=recording_id, dive_id=dive_id)
    return int(added.ordinal)
