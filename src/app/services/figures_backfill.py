"""The data path for dives whose `duration` and `avg_depth` hold the whole recording's figures.

A dive's figures are its time in the water and the mean depth over that time. A stored row can
hold the recording's whole span and its whole-recording mean instead, the end-of-dive delay at
the surface included, and nothing records whether a diver typed either. So the test is on the
value: a `duration` within a second of the primary
recording's span, and an `avg_depth` within 0.1 m of the time-weighted mean over its stored
samples, read as untouched; anything else is the diver's and stays.

Where an untouched figure gets its new value from follows what can still yield it:

- **The recording's files**, where it holds some and its profile was read from them: each read
  through the reader in attach order, the first file's figure taken unless the reader derived
  it and a later file states it - the dive form's rule for files of one computer.
- **The stored samples**, where it holds no file, or a merge or a document supplied its
  profile: `in_water_of` over the depth channel, the only thing on the instance that can yield
  them. A document's dive only where its primary device is one the operator names, because
  nothing stored tells a span a reader wrote from a figure a document stated that happens to
  equal the span.

Driven by `src/scripts/backfill_dive_figures.py`, after `backfill_dive_profiles`.
"""

import logging
import uuid as uuid_pkg
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive import Dive
from ..models.dive_profile import DiveProfile
from ..models.dive_recording import DiveRecording
from ..schemas.dive_profile import DEPTH_SCALE, MILLISECONDS_PER_SECOND
from ..schemas.parsed_dive import ParsedDiveSchema
from .blob_store import BlobMissingError
from .dive_files import LoadedDiveFile, extract_file, load_recording_files
from .dive_profiles import IMPORT_PARSER_KEY, MERGE_PARSER_KEY, NormalizedProfile, load_stored_profile
from .dive_stats import recalculate_dive_stats
from .recording_shape import in_water_of

logger = logging.getLogger(__name__)

# How far an untouched figure sits from what the whole recording gives. A FIT session's
# elapsed time rounds up past the last whole-second sample, so most sit exactly one second
# over and the bound is inclusive. A session's mean is the watch's own over every second,
# against the stored samples' over every ten.
SPAN_TOLERANCE_MS = 1000
MEAN_TOLERANCE = Decimal("0.1")

_BATCH_SIZE = 50


@dataclass(frozen=True, slots=True)
class Device:
    """A primary recording's device as `dive_recording` stores it, `model` `None` where the
    format names none."""

    brand: str
    model: str | None = None

    def __str__(self) -> str:
        return f"{self.brand} / {self.model if self.model is not None else '(no model)'}"


@dataclass(frozen=True, slots=True)
class FigureRewrite:
    """One dive's rewritten figures, each `(stored, new)` or `None` where it stays."""

    dive_uuid: uuid_pkg.UUID
    user_id: int
    # Where the new figures came from: `files`, `merge`, `document` or `samples`.
    source: str
    duration: tuple[int, int] | None
    avg_depth: tuple[float, float] | None


@dataclass(frozen=True, slots=True)
class FiguresBackfillReport:
    """What one run did or, dry, would do: the devices it was given before any dive, so a run
    that rewrites nothing still says what it was scoped to."""

    devices: tuple[Device, ...]
    dry_run: bool
    examined: int = 0
    failed: int = 0
    rewrites: list[FigureRewrite] = field(default_factory=list)


# ---------------------------------------------------------------- pure, DB-free


def whole_recording_mean(profile: NormalizedProfile) -> Decimal | None:
    """The time-weighted mean depth over every interval of the depth channel, in metres -
    what a reader that took the whole recording wrote. `None` with under two samples."""
    depth = profile.depth
    if depth is None or len(depth.t) < 2 or depth.t[-1] == depth.t[0]:
        return None
    weighted = sum(
        (later - earlier) * (shallow + deep)
        for earlier, later, shallow, deep in zip(depth.t, depth.t[1:], depth.v, depth.v[1:], strict=False)
    )
    return Decimal(weighted) / 2 / (depth.t[-1] - depth.t[0]) / DEPTH_SCALE


def stated_over_derived(parsed: Sequence[ParsedDiveSchema]) -> tuple[int | None, float | None]:
    """A recording's figures from its files in attach order, by the dive form's rule.

    A blank takes the next file's figure; a figure the reader derived gives way to a later
    file's stated one; a stated figure stays. One recording is one computer, so the form's
    same-device condition always holds here.
    """
    figures: dict[str, tuple[int | float | None, bool]] = {"duration": (None, False), "avg_depth": (None, False)}
    for file in parsed:
        for name, (value, derived) in figures.items():
            incoming = getattr(file, name)
            if incoming is None:
                continue
            stated = name not in file.inferred
            if value is None or (derived and stated):
                figures[name] = (incoming, not stated)
    duration, avg_depth = figures["duration"][0], figures["avg_depth"][0]
    return (None if duration is None else int(duration)), (None if avg_depth is None else float(avg_depth))


def untouched_duration(stored: int, span_ms: int | None) -> bool:
    """Whether a stored duration is the whole-recording one the old readers wrote."""
    return span_ms is not None and abs(stored * MILLISECONDS_PER_SECOND - span_ms) <= SPAN_TOLERANCE_MS


def untouched_avg_depth(stored: float | None, whole_mean: Decimal | None) -> bool:
    """Whether a stored average depth is the whole-recording mean, to within `MEAN_TOLERANCE`."""
    return stored is not None and whole_mean is not None and abs(Decimal(repr(stored)) - whole_mean) <= MEAN_TOLERANCE


# ---------------------------------------------------------------- persistence


async def backfill_dive_figures(
    db: AsyncSession, *, devices: Sequence[Device] = (), user_id: int | None = None, dry_run: bool = False
) -> FiguresBackfillReport:
    """Rewrite every live dive's untouched `duration` and `avg_depth` to its time in the water.

    A dive is examined when its primary recording's stored profile carries depth samples. A
    figure is rewritten when it is untouched (`untouched_duration`, `untouched_avg_depth`) and
    differs from the new one, and an average only where it stays within the dive's maximum.
    `devices` names the primary devices whose document-supplied dives may be rewritten; any
    other document-supplied dive stays as stored. `user_id` confines the run to one account.

    Every touched user's stats are recalculated and dive caches invalidated, as the profile
    backfill invalidates them. See the script for why that needs a live Redis pool.
    """
    named = set(devices)
    stmt = (
        select(
            Dive.id,
            Dive.uuid,
            Dive.user_id,
            Dive.duration,
            Dive.avg_depth,
            Dive.max_depth,
            DiveRecording.id.label("recording_id"),
            DiveRecording.device_brand,
            DiveRecording.device_model,
        )
        .join(DiveRecording, (DiveRecording.dive_id == Dive.id) & (DiveRecording.ordinal == 0))
        .join(DiveProfile, DiveProfile.recording_id == DiveRecording.id)
        .where(Dive.is_deleted.is_(False), DiveProfile.max_depth_cm.is_not(None))
        .order_by(Dive.id)
    )
    if user_id is not None:
        stmt = stmt.where(Dive.user_id == user_id)
    examined = failed = pending = 0
    rewrites: list[FigureRewrite] = []
    touched_user_ids: set[int] = set()

    for row in list(await db.execute(stmt)):
        stored = await load_stored_profile(db, recording_id=row.recording_id)
        if stored is None or stored.profile.depth is None or len(stored.profile.depth.t) < 2:
            continue
        examined += 1

        duration_open = untouched_duration(row.duration, stored.duration)
        avg_open = untouched_avg_depth(row.avg_depth, whole_recording_mean(stored.profile))
        if not (duration_open or avg_open):
            continue

        new_duration: int | None
        new_avg: float | None
        if stored.parser_key == IMPORT_PARSER_KEY:
            if Device(row.device_brand, row.device_model) not in named:
                continue
            source = "document"
        elif stored.parser_key == MERGE_PARSER_KEY:
            source = "merge"
        else:
            source = "files"

        files: list[LoadedDiveFile] = []
        if source == "files":
            try:
                files = await load_recording_files(db, recording_id=row.recording_id)
            except BlobMissingError:
                logger.error("Skipping dive %s: a stored file is missing from the volume", row.uuid)
                failed += 1
                continue
            if not files:
                source = "samples"

        if source == "files":
            parsed = [extract_file(file.data, file.parser_key).parsed for file in files]
            if any(each is None for each in parsed):
                logger.warning("Skipping dive %s: a stored file could not be re-read", row.uuid)
                failed += 1
                continue
            new_duration, new_avg = stated_over_derived([each for each in parsed if each is not None])
        else:
            in_water = in_water_of(stored.profile)
            new_duration = None if in_water is None else in_water.duration
            new_avg = None if in_water is None else float(in_water.avg_depth)

        rewrite = FigureRewrite(
            dive_uuid=row.uuid,
            user_id=row.user_id,
            source=source,
            duration=(row.duration, new_duration)
            if duration_open and new_duration is not None and new_duration > 0 and new_duration != row.duration
            else None,
            avg_depth=(row.avg_depth, new_avg)
            if avg_open
            and new_avg is not None
            and new_avg != row.avg_depth
            and (row.max_depth is None or new_avg <= row.max_depth)
            else None,
        )
        if rewrite.duration is None and rewrite.avg_depth is None:
            continue

        if not dry_run:
            values: dict[str, object] = {}
            if rewrite.duration is not None:
                values["duration"] = rewrite.duration[1]
            if rewrite.avg_depth is not None:
                values["avg_depth"] = rewrite.avg_depth[1]
            try:
                async with db.begin_nested():
                    await db.execute(update(Dive).where(Dive.id == row.id).values(**values))
            except IntegrityError:
                logger.warning("Skipping dive %s: its new figures violate a constraint", row.uuid, exc_info=True)
                failed += 1
                continue
            touched_user_ids.add(row.user_id)
            pending += 1
            if pending >= _BATCH_SIZE:
                await db.commit()
                pending = 0
        rewrites.append(rewrite)

    if not dry_run:
        for user_id in sorted(touched_user_ids):
            await recalculate_dive_stats(db=db, user_id=user_id, commit=False)
        await db.commit()
        from .cache_invalidation import invalidate_dive_caches

        for user_id in touched_user_ids:
            await invalidate_dive_caches(user_id)

    return FiguresBackfillReport(
        devices=tuple(devices), dry_run=dry_run, examined=examined, failed=failed, rewrites=rewrites
    )
