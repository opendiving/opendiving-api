"""Extraction and storage for a dive's per-sample depth/temperature/pressure curves.

The **only** module that reads or writes `dive_profile.data` - the same seam discipline
as `services/dive_files.py`, for the same reason: the payload's encoding is an
implementation detail, and everything above this module deals in `NormalizedProfile`.

Two halves. The top one is pure and DB-free (`normalize`, `downsample`,
`extract_profile`, `should_extract`), following the `reconcile()` idiom in
`dive_files.py`: the decisions worth testing are testable without a database. The bottom
one persists.

**Why JSONB rather than a packed `bytea`.** A packed int16 encoding would be perhaps 3x
smaller on a column Postgres already TOASTs and compresses, and would make
`SELECT data->'depth' FROM dive_profile WHERE ...` from `psql` impossible. The whole
file-retention rationale in this repo (see `models/dive_file.py`) is "develop new
extractions against real data"; being able to read what came out, with the tools already
on the box, is worth more than the bytes. This is the codebase's first JSONB column.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import CursorResult, delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer
from uuid6 import uuid7

from ..models.dive_profile import DiveProfile
from ..schemas.dive_profile import (
    DEPTH_SCALE,
    PRESSURE_SCALE,
    TEMPERATURE_SCALE,
    DiveProfileInfo,
    DiveProfilePressureSeries,
    DiveProfileRead,
    DiveProfileSeries,
    ParsedProfileSchema,
)
from .dive_parsers import DiveParseError, DiveParser

logger = logging.getLogger(__name__)

# Bumped whenever a change to this module or to any `parse_profile` would produce
# different samples from the same bytes. Stored on the row, so `should_extract` can tell
# "already done" from "done by an older extractor", and so the backfill script has
# something to select on.
PROFILE_EXTRACTOR_VERSION = 1

# Per channel, applied server-side at extraction. A 2026 Suunto Ocean export carries
# 3 933 temperature samples on one dive, which is already past this; depth (395) never
# reaches it. See `downsample` for why the cap is enforced by min/max bucketing.
MAX_POINTS_PER_CHANNEL = 1200


@dataclass(frozen=True, slots=True)
class ProfileSeries:
    """One normalized channel: strictly increasing integer seconds, integer-scaled values."""

    t: list[int]
    v: list[int]


@dataclass(frozen=True, slots=True)
class ProfilePressureSeries(ProfileSeries):
    """One cylinder's normalized pressure series, labelled by the gas number it reported as."""

    gas_number: int = 0


@dataclass(frozen=True, slots=True)
class NormalizedProfile:
    """A dive's channels, ready to store: rebased to zero, deduped, sorted, capped."""

    depth: ProfileSeries | None = None
    temperature: ProfileSeries | None = None
    pressure: list[ProfilePressureSeries] = field(default_factory=list)

    @property
    def channels(self) -> list[str]:
        """Which curves a chart would draw, in the order the UI stacks them."""
        present = []
        if self.depth is not None:
            present.append("depth")
        if self.temperature is not None:
            present.append("temperature")
        if self.pressure:
            present.append("pressure")
        return present

    @property
    def duration_seconds(self) -> int:
        """Elapsed seconds covered by the longest channel.

        Not the dive's `duration`: this is the span of what the file actually recorded,
        which is what the chart's x axis has to cover. `dive.duration` is the diver's
        record and may have been hand-edited.
        """
        return max((series.t[-1] for series in self._all_series()), default=0)

    @property
    def depth_sample_count(self) -> int:
        return len(self.depth.t) if self.depth is not None else 0

    def _all_series(self) -> list[ProfileSeries]:
        return [series for series in (self.depth, self.temperature, *self.pressure) if series is not None]

    def to_data(self) -> dict[str, Any]:
        """The JSONB payload. Absent key, never null, for a channel this dive doesn't carry."""
        data: dict[str, Any] = {}
        if self.depth is not None:
            data["depth"] = {"t": self.depth.t, "v": self.depth.v}
        if self.temperature is not None:
            data["temperature"] = {"t": self.temperature.t, "v": self.temperature.v}
        if self.pressure:
            data["pressure"] = [
                {"gas_number": cylinder.gas_number, "t": cylinder.t, "v": cylinder.v} for cylinder in self.pressure
            ]
        return data


@dataclass(frozen=True, slots=True)
class LoadedProfile:
    """A stored profile's series plus the span the chart's x axis has to cover."""

    duration_seconds: int
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExistingProfileRow:
    """The extraction-idempotency lookup's result - summary columns only, never `data`."""

    source_sha256: str
    extractor_version: int


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """What one run of `backfill_profiles` did. All five counts, always - a run that
    reports only successes hides the parser that stopped working."""

    examined: int = 0
    extracted: int = 0
    skipped: int = 0
    no_samples: int = 0
    failed: int = 0


# ---------------------------------------------------------------- pure, DB-free


def _rebase(points: list[tuple[float, int]], origin: float) -> ProfileSeries:
    """Round a channel onto integer seconds from `origin`, keeping the last reading per second.

    Integer seconds because at 720 px across an hour, one second is a fifth of a pixel -
    already finer than the chart can draw. Where two readings round onto the same second
    (1 Hz temperature with sub-second jitter does this constantly), the later one wins:
    an arbitrary but consistent choice, and the alternative - averaging - would invent a
    reading the sensor never took.
    """
    by_second: dict[int, int] = {}
    for seconds, value in points:
        by_second[round(seconds - origin)] = value
    ordered = sorted(by_second)
    return ProfileSeries(t=ordered, v=[by_second[second] for second in ordered])


def normalize(parsed: ParsedProfileSchema) -> NormalizedProfile | None:
    """Turn a parser's raw per-channel arrays into the stored shape, or `None` if empty.

    Format-independent work, done exactly once here rather than in each parser: rebasing
    the axis to zero, rounding to integer seconds, deduping collisions, and dropping
    channels that turned out to carry nothing.

    The origin is the earliest reading across *all* channels, not each channel's own
    first sample: the channels share one x axis on the chart, so shifting them
    independently would slide the temperature curve off the depth curve it is meant to
    line up with.
    """
    channels: list[list[tuple[float, int]]] = []
    depth_points = list(zip(parsed.depth.t, parsed.depth.v, strict=True)) if parsed.depth else []
    temperature_points = (
        list(zip(parsed.temperature.t, parsed.temperature.v, strict=True)) if parsed.temperature else []
    )
    pressure_points = [
        (cylinder.gas_number, list(zip(cylinder.t, cylinder.v, strict=True))) for cylinder in parsed.pressure
    ]
    channels = [points for points in (depth_points, temperature_points, *(p for _, p in pressure_points)) if points]

    if not channels:
        return None

    # Each channel is sorted (the schema validates it), so its first timestamp is its
    # minimum. The union across channels is not sorted, hence the `min`.
    origin = min(points[0][0] for points in channels)

    return NormalizedProfile(
        depth=_rebase(depth_points, origin) if depth_points else None,
        temperature=_rebase(temperature_points, origin) if temperature_points else None,
        pressure=[
            ProfilePressureSeries(gas_number=gas_number, t=series.t, v=series.v)
            for gas_number, series in (
                (number, _rebase(points, origin)) for number, points in pressure_points if points
            )
        ],
    )


def _downsample_series(t: list[int], v: list[int], max_points: int) -> tuple[list[int], list[int]]:
    """Min/max bucketing over time, to at most `max_points` points.

    Not LTTB. LTTB optimizes visual similarity and offers no guarantee about extremes: it
    can drop a one-sample spike, which on a depth profile is the single most important
    sample in the file. Min/max bucketing *guarantees* both extremes of every bucket
    survive, and therefore that the global maximum depth and minimum temperature - the
    two numbers a diver actually reads off this chart - come through exactly. That is a
    property a test can assert.

    Buckets are chosen on time rather than on index, so a channel with an irregular
    cadence isn't unevenly weighted; each bucket emits its min and its max in time order.
    """
    if len(t) <= max_points:
        return t, v

    # Two points per bucket (the min and the max), so the cap is what bounds the bucket
    # count rather than the other way round.
    buckets = max_points // 2
    span = t[-1] - t[0]

    picked: list[int] = []
    start = 0
    for bucket in range(buckets):
        # Index-based fallback when every sample shares one timestamp, which can't be
        # bucketed on time at all.
        if span <= 0:
            end = len(t) * (bucket + 1) // buckets
        else:
            boundary = t[0] + span * (bucket + 1) / buckets
            end = start
            while end < len(t) and (t[end] < boundary or bucket == buckets - 1):
                end += 1
        if end <= start:
            continue

        window = range(start, end)
        lowest = min(window, key=lambda index: v[index])
        highest = max(window, key=lambda index: v[index])
        picked.extend(sorted({lowest, highest}))
        start = end

    return [t[index] for index in picked], [v[index] for index in picked]


def downsample(profile: NormalizedProfile, max_points: int = MAX_POINTS_PER_CHANNEL) -> NormalizedProfile:
    """Cap every channel independently. A channel already under the cap is untouched."""

    def capped(series: ProfileSeries | None) -> ProfileSeries | None:
        if series is None:
            return None
        t, v = _downsample_series(series.t, series.v, max_points)
        return ProfileSeries(t=t, v=v)

    return NormalizedProfile(
        depth=capped(profile.depth),
        temperature=capped(profile.temperature),
        pressure=[
            ProfilePressureSeries(gas_number=cylinder.gas_number, t=t, v=v)
            for cylinder, (t, v) in (
                (cylinder, _downsample_series(cylinder.t, cylinder.v, max_points)) for cylinder in profile.pressure
            )
        ],
    )


def extract_profile(parser: type[DiveParser], content: bytes) -> NormalizedProfile | None:
    """Run a parser's profile extraction over some bytes, normalized and capped.

    **Never raises.** A failed extraction must not fail the upload it rode in on: the
    file is the durable artifact and can be re-extracted after the extractor is fixed,
    whereas refusing the attach would discard the very corpus entry needed to fix it. So
    `DiveParseError` - and anything unexpected - is logged with the parser key and
    swallowed, and the dive simply has no profile until a backfill run picks it up.
    """
    try:
        parsed = parser.parse_profile(content)
        if parsed is None:
            return None
        normalized = normalize(parsed)
        if normalized is None:
            return None
        return downsample(normalized)
    except DiveParseError:
        logger.warning("Profile extraction failed for a %s file: malformed samples", parser.key, exc_info=True)
        return None
    except Exception:
        logger.exception("Unexpected error extracting a profile from a %s file", parser.key)
        return None


def should_extract(
    existing: ExistingProfileRow | None, *, sha256: str, version: int = PROFILE_EXTRACTOR_VERSION
) -> Literal["extract", "skip"]:
    """Decide whether a dive's profile is still current.

    `(source file digest, extractor version)` is the whole test - a profile is a pure
    function of those two things. Split out from its callers so the table of cases is
    testable without a database.
    """
    if existing is None:
        return "extract"
    if existing.source_sha256 != sha256:
        return "extract"
    if existing.extractor_version != version:
        return "extract"
    return "skip"


def _scaled(value: int | None, scale: int) -> float | None:
    """Integer-scaled storage back into display units.

    Division, never multiplication by a reciprocal: `2052 / 10` is the correctly-rounded
    `205.2`, whereas `2052 * 0.1` reintroduces exactly the noise the integer encoding was
    chosen to remove.
    """
    return None if value is None else value / scale


# ---------------------------------------------------------------- persistence


async def store_profile(
    db: AsyncSession,
    *,
    dive_id: int,
    profile: NormalizedProfile,
    source_sha256: str,
    parser_key: str,
    commit: bool = False,
) -> None:
    """Replace this dive's profile with `profile`.

    `commit=False` by default because the caller that matters (`store_dive_file`) has to
    write the file and the profile in one transaction: a dive must never end up with a
    stored file and a profile extracted from a *different* one.

    The delete runs before the insert because `ux_dive_profile_dive_id` is checked per
    statement, so two rows for one dive must not coexist even momentarily - the same
    ordering, for the same reason, as `store_dive_file`.
    """
    depth_values = profile.depth.v if profile.depth else []
    temperature_values = profile.temperature.v if profile.temperature else []
    pressure_values = [value for cylinder in profile.pressure for value in cylinder.v]

    await db.execute(delete(DiveProfile).where(DiveProfile.dive_id == dive_id))
    await db.execute(
        insert(DiveProfile).values(
            dive_id=dive_id,
            source_sha256=source_sha256,
            parser_key=parser_key,
            extractor_version=PROFILE_EXTRACTOR_VERSION,
            duration_seconds=profile.duration_seconds,
            depth_sample_count=profile.depth_sample_count,
            max_depth_cm=max(depth_values) if depth_values else None,
            min_temperature_c10=min(temperature_values) if temperature_values else None,
            max_temperature_c10=max(temperature_values) if temperature_values else None,
            min_pressure_bar10=min(pressure_values) if pressure_values else None,
            max_pressure_bar10=max(pressure_values) if pressure_values else None,
            data=profile.to_data(),
            # Spelled out rather than left to `PublicUUIDMixin`'s `default_factory`: that
            # is a dataclass-level default applied when the ORM constructs an instance,
            # and this Core-level INSERT never constructs one.
            uuid=uuid7(),
            created_at=datetime.now(UTC),
        )
    )
    if commit:
        await db.commit()


async def get_existing_profile(db: AsyncSession, *, dive_id: int) -> ExistingProfileRow | None:
    """The two columns `should_extract` needs. Explicit columns, so `data` can't ride along."""
    stmt = select(DiveProfile.source_sha256, DiveProfile.extractor_version).where(DiveProfile.dive_id == dive_id)
    row = (await db.execute(stmt)).one_or_none()
    return None if row is None else ExistingProfileRow(*row)


async def get_profile_version(db: AsyncSession, *, dive_id: int) -> str | None:
    """The ETag for a dive's profile, or `None` when it has none.

    `"{source_sha256}:{extractor_version}"` because those two things are exactly what the
    payload is a function of. Lets the read route answer a conditional request after one
    narrow query rather than decoding tens of KB of JSONB only to discard it.
    """
    existing = await get_existing_profile(db, dive_id=dive_id)
    return None if existing is None else f"{existing.source_sha256}:{existing.extractor_version}"


async def load_profile(db: AsyncSession, *, dive_id: int) -> LoadedProfile | None:
    """Fetch a dive's full profile. The only place `data` is ever loaded - hence the
    explicit `undefer`, which is what makes every other query here cheap by default."""
    stmt = select(DiveProfile).where(DiveProfile.dive_id == dive_id).options(undefer(DiveProfile.data))
    profile = (await db.execute(stmt)).scalar_one_or_none()
    if profile is None:
        return None
    return LoadedProfile(duration_seconds=profile.duration_seconds, data=profile.data)


def to_read_schema(loaded: LoadedProfile) -> DiveProfileRead:
    """The stored payload as the wire shape, integers untouched."""
    data = loaded.data or {}
    depth = data.get("depth")
    temperature = data.get("temperature")
    return DiveProfileRead(
        duration_seconds=loaded.duration_seconds,
        depth=DiveProfileSeries(t=depth["t"], v=depth["v"]) if depth else None,
        temperature=DiveProfileSeries(t=temperature["t"], v=temperature["v"]) if temperature else None,
        pressure=[
            DiveProfilePressureSeries(gas_number=cylinder["gas_number"], t=cylinder["t"], v=cylinder["v"])
            for cylinder in data.get("pressure") or []
        ],
    )


async def delete_profile_for_dive(db: AsyncSession, *, dive_id: int, commit: bool = True) -> bool:
    """Hard-delete a dive's profile. Returns whether there was one to delete.

    Called from both the file-delete and the dive-delete paths, because the FK's
    `ON DELETE CASCADE` fires on neither - see `models/dive_profile.py`. A profile whose
    source export is gone can never be re-derived or checked against anything, so it goes
    with the file rather than outliving it.
    """
    result = cast(CursorResult, await db.execute(delete(DiveProfile).where(DiveProfile.dive_id == dive_id)))
    deleted = result.rowcount > 0
    if commit:
        await db.commit()
    return deleted


async def get_profile_infos_for_dives(db: AsyncSession, *, dive_ids: list[int]) -> dict[int, DiveProfileInfo | None]:
    """Resolve several dives' profile summaries in one query.

    Only the detail endpoint asks for this today, and only ever for one dive - but this
    is the `get_file_infos_for_dives` shape, it makes the explicit-columns discipline the
    default, and it is what a profile sparkline in the dive list would need without a
    rewrite.

    `channels` is derived from which extremes are non-NULL rather than stored: a column
    saying which curves a row carries is a column that can disagree with the row.
    """
    if not dive_ids:
        return {}

    stmt = select(
        DiveProfile.dive_id,
        DiveProfile.uuid,
        DiveProfile.duration_seconds,
        DiveProfile.depth_sample_count,
        DiveProfile.max_depth_cm,
        DiveProfile.min_temperature_c10,
        DiveProfile.max_temperature_c10,
        DiveProfile.min_pressure_bar10,
        DiveProfile.max_pressure_bar10,
        DiveProfile.updated_at,
    ).where(DiveProfile.dive_id.in_(set(dive_ids)))

    infos: dict[int, DiveProfileInfo | None] = dict.fromkeys(dive_ids)
    for row in await db.execute(stmt):
        channels: list[str] = []
        if row.depth_sample_count > 0:
            channels.append("depth")
        if row.min_temperature_c10 is not None:
            channels.append("temperature")
        if row.min_pressure_bar10 is not None:
            channels.append("pressure")

        infos[row.dive_id] = DiveProfileInfo(
            uuid=row.uuid,
            duration_seconds=row.duration_seconds,
            depth_sample_count=row.depth_sample_count,
            channels=channels,
            max_depth=_scaled(row.max_depth_cm, DEPTH_SCALE),
            min_temperature=_scaled(row.min_temperature_c10, TEMPERATURE_SCALE),
            max_temperature=_scaled(row.max_temperature_c10, TEMPERATURE_SCALE),
            min_pressure=_scaled(row.min_pressure_bar10, PRESSURE_SCALE),
            max_pressure=_scaled(row.max_pressure_bar10, PRESSURE_SCALE),
            updated_at=row.updated_at,
        )
    return infos


# How many files are processed between commits. Small enough that an interrupted run
# loses little, large enough that a few hundred dives isn't a few hundred transactions.
_BACKFILL_BATCH_SIZE = 50


async def backfill_profiles(
    db: AsyncSession,
    *,
    parser_key: str | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> BackfillReport:
    """Re-extract profiles from the exports already stored against dives.

    Selects the dives whose profile is missing, was produced by an older extractor, or
    came out of different bytes than the file now on the dive. `force` re-extracts
    everything matching `parser_key` regardless - what you want after fixing a parser
    without bumping `PROFILE_EXTRACTOR_VERSION`.

    A one-shot script drives this, not an arq job: the API-side queue plumbing was
    deliberately deleted (see DECISIONS.md) and the worker runs crons only, so a backfill
    scheduled as a cron would rescan the whole corpus forever for a job that finishes
    once per extractor version. See `src/scripts/backfill_dive_profiles.py`.
    """
    # Imported here rather than at module scope: `dive_files` imports *this* module for
    # the extraction hooks in `store_dive_file`, so a top-level import would be circular.
    from ..models.dive_file import DiveFile  # noqa: I001 - kept next to the import it depends on
    from .dive_files import load_dive_file
    from .dive_parsers import PARSER_BY_KEY

    stmt = (
        select(DiveFile.dive_id, DiveFile.sha256, DiveFile.parser_key, DiveFile.user_id)
        # Explicit columns, never `select(DiveFile)`: the `bytea` would ride along for
        # every row in the corpus before a single profile was extracted.
        .outerjoin(DiveProfile, DiveProfile.dive_id == DiveFile.dive_id)
        .order_by(DiveFile.dive_id)
    )
    if parser_key is not None:
        stmt = stmt.where(DiveFile.parser_key == parser_key)
    if not force:
        stmt = stmt.where(
            (DiveProfile.id.is_(None))
            | (DiveProfile.extractor_version != PROFILE_EXTRACTOR_VERSION)
            | (DiveProfile.source_sha256 != DiveFile.sha256)
        )
    if limit is not None:
        stmt = stmt.limit(limit)

    candidates = list(await db.execute(stmt))
    examined = extracted = skipped = no_samples = failed = 0
    touched_user_ids: set[int] = set()

    for index, row in enumerate(candidates, start=1):
        examined += 1

        parser = PARSER_BY_KEY.get(row.parser_key)
        if parser is None:
            # A file recorded under a parser key this build no longer has. Nothing to
            # re-read it with, and nothing to be done about it here.
            logger.warning("Skipping dive %s: unknown parser key %r", row.dive_id, row.parser_key)
            failed += 1
            continue

        if not force:
            existing = await get_existing_profile(db, dive_id=row.dive_id)
            if should_extract(existing, sha256=row.sha256) == "skip":
                skipped += 1
                continue

        file = await load_dive_file(db, dive_id=row.dive_id)
        if file is None:
            logger.warning("Skipping dive %s: its stored file vanished mid-run", row.dive_id)
            failed += 1
            continue

        profile = extract_profile(parser, file.data)
        if profile is None:
            # Either the file genuinely carries no samples (every pre-transmitter export
            # in the corpus that predates sample logging) or extraction failed and was
            # logged inside `extract_profile`. Both leave the dive without a profile.
            no_samples += 1
            continue

        if dry_run:
            extracted += 1
            continue

        await store_profile(
            db,
            dive_id=row.dive_id,
            profile=profile,
            source_sha256=file.sha256,
            parser_key=row.parser_key,
        )
        extracted += 1
        touched_user_ids.add(row.user_id)

        if index % _BACKFILL_BATCH_SIZE == 0:
            await db.commit()

    if not dry_run:
        await db.commit()
        # Cached dive reads embed `profile`, and every dive this run touched is now
        # claiming it has none. See the script for why this needs a live Redis pool.
        from .cache_invalidation import invalidate_dive_caches

        for user_id in touched_user_ids:
            await invalidate_dive_caches(user_id)

    return BackfillReport(examined=examined, extracted=extracted, skipped=skipped, no_samples=no_samples, failed=failed)
