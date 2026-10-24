"""Replay a fold's by-side rule over dives merged before it existed.

`POST /dives/merge` once filled every value a fold's two records share from the surviving one
first, so a dive merged then ends on the readings at the computer's restart: its end pressure,
`cns_end` and `otu_end` are the first half's while its time and depth cover both.
This rewrites those values the way `merge_dives` now writes them.

**The inputs are what a merge leaves behind.** The absorbed dive is soft-deleted, never purged,
and keeps its cylinders; it is found by its `deleted_at`, written in the same request
as the folded profile's `created_at`. Its recording row is gone, so the absorbed record's CNS and
OTU come from its files, which moved onto the folded recording and are told apart from the
surviving record's by the start each one states.

A fold whose profile is no longer `merge` - replaced before `_keeps_stored_profile` existed, or
never stored because neither half had samples - is not found. One whose absorbed dive the window
cannot single out, or whose dive holds another recording, has only its readouts repaired, and the
run names its dive.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.crud_dive_mixtures import get_mixtures_for_dive
from ..models.dive import Dive
from ..models.dive_mixture import DiveMixture
from ..models.dive_profile import DiveProfile
from ..models.dive_recording import DiveRecording
from . import blob_store
from .cache_invalidation import invalidate_dive_caches
from .dive_files import LoadedDiveFile, extract_file, extract_recording, fold_tank_pressures, load_recording_files
from .dive_merge import END_READOUTS, START_READOUTS
from .dive_profiles import MERGE_PARSER_KEY
from .dive_recordings import SAME_RECORDING_START_TOLERANCE, delta_seconds

logger = logging.getLogger(__name__)

# How far apart a merge's two writes may be: the folded profile's `created_at` and the absorbed
# dive's `deleted_at` come from one request, a fraction of a second apart.
MERGE_WINDOW = timedelta(seconds=60)


@dataclass
class FoldBackfillReport:
    examined: int = 0
    dives_updated: int = 0
    # Folds whose cylinders are left, each logged by dive: their readouts are still repaired
    # from the files.
    absorbed_not_found: int = 0
    beside_another_recording: int = 0
    failed: int = 0
    changes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A folded recording, with the dive it is on and when the merge stored its profile."""

    id: int
    dive_id: int
    start_time: datetime | None
    utc_offset_minutes: int | None
    created_at: datetime
    user_id: int


class _FoldedDive(BaseModel):
    """A dive's logged start: what the replay reads off either half."""

    start_time: datetime
    utc_offset_minutes: int | None = None


async def backfill_folds(db: AsyncSession, *, user_id: int | None = None, dry_run: bool = False) -> FoldBackfillReport:
    """Rewrite every folded dive's start and end values by side. Commits unless `dry_run`.

    Every change is logged and listed in the report as `before -> after`, so a dry run is the
    review of what an apply writes. A second run finds nothing to change.
    """
    report = FoldBackfillReport()
    stmt = (
        select(
            DiveRecording.id,
            DiveRecording.dive_id,
            DiveRecording.start_time,
            DiveRecording.utc_offset_minutes,
            DiveProfile.created_at,
            Dive.user_id,
        )
        .join(DiveProfile, DiveProfile.recording_id == DiveRecording.id)
        .join(Dive, Dive.id == DiveRecording.dive_id)
        .where(DiveProfile.parser_key == MERGE_PARSER_KEY, Dive.is_deleted.is_(False))
        .order_by(DiveRecording.id)
    )
    if user_id is not None:
        stmt = stmt.where(Dive.user_id == user_id)

    touched_user_ids: set[int] = set()
    for row in (await db.execute(stmt)).all():
        candidate = _Candidate(**row._asdict())
        report.examined += 1
        try:
            changes = await _repair(db, candidate, report)
        except blob_store.BlobMissingError:
            logger.warning("Skipping recording %s: one of its files is missing", candidate.id)
            report.failed += 1
            continue
        if not changes:
            continue
        report.dives_updated += 1
        for change in changes:
            logger.info("Dive %s: %s", candidate.dive_id, change)
            report.changes.append(f"dive {candidate.dive_id}: {change}")
        touched_user_ids.add(candidate.user_id)

    if dry_run:
        await db.rollback()
        return report
    await db.commit()
    # Cached dive reads embed the readouts and the cylinders.
    for touched in touched_user_ids:
        await invalidate_dive_caches(touched)
    return report


async def _repair(db: AsyncSession, row: _Candidate, report: FoldBackfillReport) -> list[str]:
    """Write one fold's by-side values, returning what changed."""
    changes = (
        []
        if row.start_time is None
        else await _repair_readouts(db, recording_id=row.id, start_time=row.start_time, offset=row.utc_offset_minutes)
    )

    # Beside another recording, the dive's cylinders may be that recording's - a
    # second computer's, or a later merge's that moved the fold here - and nothing stored says
    # which, so they are left for the diver. Alone, the fold is the dive's whole account.
    recordings = (
        await db.execute(select(func.count()).select_from(DiveRecording).where(DiveRecording.dive_id == row.dive_id))
    ).scalar_one()
    if recordings > 1:
        logger.warning(
            "Leaving dive %s's cylinders: it holds another recording beside its fold (recording %s)",
            row.dive_id,
            row.id,
        )
        report.beside_another_recording += 1
        return changes
    absorbed_id = await _absorbed_dive_id(db, user_id=row.user_id, merged_at=row.created_at)
    if absorbed_id is None:
        logger.warning(
            "Leaving dive %s's cylinders: no single deleted dive matches its fold (recording %s)",
            row.dive_id,
            row.id,
        )
        report.absorbed_not_found += 1
        return changes
    survivor, absorbed = await _dive(db, row.dive_id), await _dive(db, absorbed_id)
    survivor_first = (
        True
        if row.start_time is None
        else _survivor_first(survivor, absorbed, start_time=row.start_time, offset=row.utc_offset_minutes)
    )

    survivor_mixtures = await get_mixtures_for_dive(db, row.dive_id)
    before = {mixture.id: mixture for mixture in survivor_mixtures}
    pressures = fold_tank_pressures(
        await get_mixtures_for_dive(db, absorbed_id), survivor_mixtures, survivor_first=survivor_first
    )
    for mixture_id, values in pressures.items():
        await db.execute(update(DiveMixture).where(DiveMixture.id == mixture_id).values(**values))
        changes.extend(
            f"cylinder {mixture_id} {column}: {getattr(before[mixture_id], column)} -> {value}"
            for column, value in values.items()
            if getattr(before[mixture_id], column) != value
        )
    return changes


async def _repair_readouts(
    db: AsyncSession, *, recording_id: int, start_time: datetime, offset: int | None
) -> list[str]:
    """The folded recording's CNS and OTU, start-side from the files of the record it starts
    on and end-side from the files of the last record after it. A side no file yields stays."""
    files = await load_recording_files(db, recording_id=recording_id)
    extractions = {file.sha256: extract_file(file.data, file.parser_key) for file in files}
    placed = [(file, start) for file in files if (start := extractions[file.sha256].start) is not None]

    def apart(at: tuple[datetime, int | None], other: tuple[datetime, int | None]) -> float:
        return delta_seconds(at[0], at[1], other[0], other[1])

    origin = (start_time, offset)
    first = [file for file, at in placed if apart(at, origin) <= SAME_RECORDING_START_TOLERANCE]
    rest = [(file, at) for file, at in placed if apart(at, origin) > SAME_RECORDING_START_TOLERANCE]
    last: list[LoadedDiveFile] = []
    if rest:
        latest = max((at for _, at in rest), key=lambda at: apart(at, origin))
        last = [file for file, at in rest if apart(at, latest) <= SAME_RECORDING_START_TOLERANCE]

    readouts: dict[str, float] = {}
    for columns, group in ((START_READOUTS, first), (END_READOUTS, last)):
        if group:
            scalars = extract_recording(group, extractions, start_time=start_time, utc_offset_minutes=offset).scalars
            readouts |= {column: value for column in columns if (value := scalars.get(column)) is not None}
    if not readouts:
        return []

    stored = (
        await db.execute(
            select(*(getattr(DiveRecording, column) for column in readouts)).where(DiveRecording.id == recording_id)
        )
    ).one()
    changed = {
        column: (before, value)
        for (column, value), before in zip(readouts.items(), stored, strict=True)
        if before != value
    }
    if changed:
        await db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording_id)
            .values(**{column: value for column, (_, value) in changed.items()})
        )
    return [f"recording {recording_id} {column}: {before} -> {value}" for column, (before, value) in changed.items()]


async def _dive(db: AsyncSession, dive_id: int) -> _FoldedDive:
    row = (
        await db.execute(
            select(
                Dive.start_time,
                Dive.utc_offset_minutes,
            ).where(Dive.id == dive_id)
        )
    ).one()
    return _FoldedDive(**row._asdict())


async def _absorbed_dive_id(db: AsyncSession, *, user_id: int, merged_at: datetime) -> int | None:
    """The soft-deleted dive this fold absorbed, or `None` where the window holds none or several."""
    ids = (
        (
            await db.execute(
                select(Dive.id).where(
                    Dive.user_id == user_id,
                    Dive.is_deleted.is_(True),
                    Dive.deleted_at.between(merged_at - MERGE_WINDOW, merged_at + MERGE_WINDOW),
                )
            )
        )
        .scalars()
        .all()
    )
    return ids[0] if len(ids) == 1 else None


def _survivor_first(survivor: _FoldedDive, absorbed: _FoldedDive, *, start_time: datetime, offset: int | None) -> bool:
    """Whether the surviving dive held the earlier record.

    The fold moved the recording's start to the earlier record's and kept no other trace of
    which dive held which, so the dive whose logged start is the nearer one is taken to have
    held it - the surviving dive on a tie, as `_plan_fold` decides one.
    """
    to_survivor = delta_seconds(survivor.start_time, survivor.utc_offset_minutes, start_time, offset)
    to_absorbed = delta_seconds(absorbed.start_time, absorbed.utc_offset_minutes, start_time, offset)
    return to_survivor <= to_absorbed
