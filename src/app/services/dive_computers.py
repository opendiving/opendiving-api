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

**Which of a computer's listed dives the logbook holds**, and whether the holding recording
keeps that computer's original download of it, is answered here and not by a client, by the
same clock rule and tolerance the import's same-recording gate uses (`known_dives`): a dive
held by mistake is one the diver is never offered, and an original claimed by mistake is one
never sent, so the rule has one home and errs toward an original missing.
"""

import uuid as uuid_pkg
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import split_local_start_time
from ..models.dive import Dive
from ..models.dive_file import DiveFile
from ..models.dive_profile import DiveProfile
from ..models.dive_recording import DiveRecording
from ..models.user import User
from ..schemas.dive_computer import DiveComputerRead, KnownDive, ListedDive
from .dive_profiles import MERGE_PARSER_KEY
from .dive_recordings import (
    CANDIDATE_WINDOW,
    SAME_RECORDING_START_TOLERANCE,
    DeviceIdentity,
    RecordingFacts,
    folded_serial,
    holds_listed_start,
    make_primary,
    signed_delta_seconds,
)
from .libdivecomputer import LIBDIVECOMPUTER
from .merged_dives import resolve_merges


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


@dataclass(frozen=True, slots=True)
class _Stored:
    """A recording of the listed computer, on a live dive of the caller's, with its stored
    start column, which the bisect orders by, and the fallback start of each computer
    original it holds - `None` for one that carries none, and no entry where it holds none."""

    start: datetime | None
    facts: RecordingFacts
    dive_uuid: uuid_pkg.UUID
    originals: tuple[datetime | None, ...]


async def _recordings_of(db: AsyncSession, *, user_id: int, serial: str) -> list[_Stored]:
    """The caller's recordings that name this serial, folded, on live dives, ordered by the
    stored start column with those that have none last.

    The serial's spellings come first, folded here for `_recorded`'s reason, so the second
    query matches them exactly. Two queries whatever the number of dives listed.
    """
    folded = folded_serial(serial)
    if folded is None:
        return []
    spellings = [
        spelled
        for spelled in (
            await db.execute(
                select(DiveRecording.device_serial)
                .where(DiveRecording.user_id == user_id, DiveRecording.device_serial.is_not(None))
                .distinct()
            )
        ).scalars()
        if folded_serial(spelled) == folded
    ]
    if not spellings:
        return []
    originals = (
        select(func.array_agg(DiveFile.fallback_start))
        .where(
            DiveFile.recording_id == DiveRecording.id,
            DiveFile.user_id == user_id,
            DiveFile.parser_key == LIBDIVECOMPUTER,
        )
        .scalar_subquery()
    )
    rows = await db.execute(
        select(
            DiveRecording.start_time,
            DiveRecording.utc_offset_minutes,
            DiveProfile.duration.label("sampled_span"),
            DiveProfile.parser_key,
            Dive.uuid.label("dive_uuid"),
            originals.label("originals"),
        )
        .join(Dive, Dive.id == DiveRecording.dive_id)
        .outerjoin(DiveProfile, DiveProfile.recording_id == DiveRecording.id)
        .where(
            DiveRecording.user_id == user_id,
            DiveRecording.device_serial.in_(spellings),
            Dive.is_deleted.is_(False),
        )
        .order_by(DiveRecording.start_time.nulls_last(), DiveRecording.id)
    )
    return [
        _Stored(
            start=row.start_time,
            facts=RecordingFacts(
                device=DeviceIdentity(serial=serial),
                start_time=row.start_time,
                utc_offset_minutes=row.utc_offset_minutes,
                sampled_span=row.sampled_span,
                merged=row.parser_key == MERGE_PARSER_KEY,
            ),
            dive_uuid=row.dive_uuid,
            originals=tuple(row.originals or ()),
        )
        for row in rows
    ]


async def known_dives(db: AsyncSession, *, user_id: int, serial: str, dives: Sequence[ListedDive]) -> list[KnownDive]:
    """The listed dives the caller's logbook holds, in the order listed and each uuid once,
    and whether each holder keeps the computer's original of it.

    A dive is held by a live dive its uuid resolves to - the dive of that uuid, or the one it
    was merged into, as an import resolves it - or by a recording of the serial whose start
    `holds_listed_start` accepts, the predicate an import asks of a merge's recording. Three
    queries for one listed dive or for 2,000: each listed start is compared only with the
    recordings whose stored start lies within the gates' candidate window of it, widened by the
    longest merge's span, found by bisecting the recordings' starts.

    The original is told by `_keeps_original` from the recordings' rows alone, read in the same
    query, so no stored file is read. A dive held by its uuid is told by its recordings of the
    serial too, rather than answered yes: an import under that uuid links and writes no file,
    so a no there costs one download that adds nothing.
    """
    if not dives:
        return []
    by_uuid = {
        uuid: target.uuid
        for uuid, target in (await resolve_merges(db, user_id=user_id, uuids={dive.uuid for dive in dives})).items()
        if not target.is_deleted
    }
    recorded = await _recordings_of(db, user_id=user_id, serial=serial)
    by_dive: dict[uuid_pkg.UUID, list[_Stored]] = {}
    for one in recorded:
        by_dive.setdefault(one.dive_uuid, []).append(one)
    stored = [one for one in recorded if one.start is not None]
    starts = [one.start for one in stored if one.start is not None]
    longest_merge = timedelta(
        milliseconds=max(
            (one.facts.sampled_span or 0 for one in stored if one.facts.merged),
            default=0,
        )
    )

    known: list[KnownDive] = []
    seen: set[uuid_pkg.UUID] = set()
    for dive in dives:
        if dive.uuid in seen:
            continue
        start, offset = split_local_start_time(dive.started_at)
        holder = by_uuid.get(dive.uuid)
        if holder is not None:
            has_original = any(_keeps_original(one, start, offset) for one in by_dive.get(holder, ()))
        else:
            holding = _holding(start, offset, stored, starts, longest_merge)
            if holding is None:
                continue
            holder, has_original = holding.dive_uuid, _keeps_original(holding, start, offset)
        seen.add(dive.uuid)
        known.append(KnownDive(uuid=dive.uuid, dive_uuid=holder, has_original=has_original))
    return known


def _holding(
    start: datetime, offset: int | None, stored: list[_Stored], starts: list[datetime], longest_merge: timedelta
) -> _Stored | None:
    """The first recording, by stored start, that holds this listed start."""
    low = bisect_left(starts, start - CANDIDATE_WINDOW - longest_merge)
    high = bisect_right(starts, start + CANDIDATE_WINDOW)
    for one in stored[low:high]:
        if holds_listed_start(one.facts, start, offset):
            return one
    return None


def _keeps_original(one: _Stored, start: datetime, offset: int | None) -> bool:
    """Whether this recording keeps the computer's original of the dive listed at this start.

    A recording that is one record holds only that record's original. A merge's holds two
    records' files, and only an original's fallback start says which record it is: it must
    lie within the same-recording tolerance of the listed start, compared as instants, so a
    listed wall clock, which no instant can be put beside without inventing an offset, and a
    file with no fallback start answer no. A wrong yes leaves an original off its dive for
    good, and a wrong no costs one download whose upload changes nothing.
    """
    if not one.originals:
        return False
    if not one.facts.merged:
        return True
    if offset is None:
        return False
    return any(
        fallback is not None and abs(signed_delta_seconds(start, offset, fallback, 0)) <= SAME_RECORDING_START_TOLERANCE
        for fallback in one.originals
    )
