"""Extraction and storage for a dive's per-sample curves and the events alongside them.

Four channels - depth, deco ceiling, temperature and per-cylinder tank pressure - plus the
moments a device marked rather than sampled: gas switches, stops, bookmarks and alerts.

The **only** module that reads or writes `dive_profile.data` - the same seam discipline
as `services/dive_files.py`, for the same reason: the payload's encoding is an
implementation detail, and everything above this module deals in `NormalizedProfile`.

Two halves. The top one is pure and DB-free (`normalize`, `derive_gas_attribution`,
`downsample`, `extract_profile`, `should_extract`), following the `reconcile()` idiom in
`dive_files.py`: the decisions worth testing are testable without a database. The bottom
one persists.

One thing here is not a curve: `derive_gas_attribution` reads the gas switches back
against the depth channel to work out which cylinder was breathed for how long and how
deep, and that lands in a summary column rather than in `data`. It is the profile's job
because the samples are the only evidence for it, and it is consumed a table away by
`services/dive_gas.py`, which owns every figure derived from it.

**Why JSONB rather than a packed `bytea`.** A packed int16 encoding would be perhaps 3x
smaller on a column Postgres already TOASTs and compresses, and would make
`SELECT data->'depth' FROM dive_profile WHERE ...` from `psql` impossible. The whole
file-retention rationale in this repo (see `models/dive_file.py`) is "develop new
extractions against real data"; being able to read what came out, with the tools already
on the box, is worth more than the bytes. This is the codebase's first JSONB column.
"""

import logging
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import ValidationError
from sqlalchemy import CursorResult, delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer
from uuid6 import uuid7

from ..models.dive_profile import DiveProfile
from ..schemas.dive_profile import (
    CEILING_SCALE,
    DEPTH_SCALE,
    PRESSURE_SCALE,
    TEMPERATURE_SCALE,
    DiveProfileEvent,
    DiveProfileInfo,
    DiveProfilePressureSeries,
    DiveProfileRead,
    DiveProfileSeries,
    GasAttribution,
    ParsedProfileSchema,
    ProfileEventType,
)
from .blob_store import BlobMissingError
from .dive_parsers import DiveParseError, DiveParser

logger = logging.getLogger(__name__)

# Bumped whenever a change to this module or to any `parse_profile` would produce
# different samples from the same bytes. Stored on the row, so `should_extract` can tell
# "already done" from "done by an older extractor", and so the backfill script has
# something to select on.
#
# 2: the deco `ceiling` channel and `events`, which every parser had been dropping.
# 3: `gas_attribution`, derived from those events - which gas was breathed for how long
#    and how deep - and `_downsample_series` keeping each channel's first and last sample,
#    which it had not been guaranteeing. The second is why this covers the samples and not
#    only the new column.
PROFILE_EXTRACTOR_VERSION = 3

# Per channel, applied server-side at extraction. A 2026 Suunto Ocean export carries
# 3 933 temperature samples on one dive, which is already past this; depth (395) never
# reaches it. See `downsample` for why the cap is enforced by min/max bucketing.
MAX_POINTS_PER_CHANNEL = 1200

# Events are capped by count and **not** by the bucketing below: min/max over a bucket is
# meaningless for a marker, and a chart that showed "some of the gas switches" would be
# worse than one that showed none. The cap exists for the reason `_MAX_CYLINDERS` does in
# the parsers - nothing else bounds how many markers a file may claim, and every one of
# them is a few dozen bytes in a payload that is fetched whole - not because any real dive
# approaches it: the worst dive in the corpus produces 17.
MAX_EVENTS = 200

# And how long one marker's `label` may be, which is what makes the cap above mean anything.
# `label` is the only field in the payload carrying text straight off an uploaded file -
# every channel is bounded by `MAX_POINTS_PER_CHANNEL` and every other value is a number -
# so without this a 5 MB export of nothing but long alert strings becomes a 5 MB JSONB row,
# on a table whose whole design assumes tens of KB and serves them whole on every read.
# 120 is far past any device's wording: the longest in the corpus is "Mandatory Safety Stop
# Broken", at 28.
MAX_LABEL_CHARS = 120


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
class ProfileEvent:
    """One stored marker: an integer second, a type from the closed vocabulary, and
    whatever the device said about it."""

    t: int
    type: ProfileEventType
    gas_number: int | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedProfile:
    """A dive's channels, ready to store: rebased to zero, deduped, sorted, capped."""

    depth: ProfileSeries | None = None
    ceiling: ProfileSeries | None = None
    temperature: ProfileSeries | None = None
    pressure: list[ProfilePressureSeries] = field(default_factory=list)
    events: list[ProfileEvent] = field(default_factory=list)
    # Not a channel and not stored in `data`: a summary column, derived from `events` and
    # `depth` by `derive_gas_attribution` and carried here so one `NormalizedProfile` is
    # still everything `store_profile` needs. Empty on a profile whose file said nothing
    # about which gas was breathed when, which is most of them.
    gas_attribution: list[GasAttribution] = field(default_factory=list)

    @property
    def channels(self) -> list[str]:
        """Which curves a chart would draw, in the order the UI stacks them.

        `ceiling` sits next to `depth` because it is drawn on depth's axis rather than one
        of its own - a ceiling of 3 m has to land at the same y as a depth of 3 m, or the
        shaded no-ascent region wouldn't bound the curve it describes.

        **Nothing in `src` reads this.** What ships is `get_profile_infos_for_dives`, which
        derives the same list from which summary columns came back non-NULL - a row's own
        account of itself, rather than one the extractor remembered. This is the same
        ordering stated where it can be asserted directly against a profile in hand, and the
        two are meant to agree; a test that finds them disagreeing has found a bug in the
        column-derived one, which is the copy that matters.
        """
        present = []
        if self.depth is not None:
            present.append("depth")
        if self.ceiling is not None:
            present.append("ceiling")
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
        return [series for series in (self.depth, self.ceiling, self.temperature, *self.pressure) if series is not None]

    def to_data(self) -> dict[str, Any]:
        """The JSONB payload. Absent key, never null, for a channel this dive doesn't carry.

        Events follow the same rule as the channels: a dive whose file recorded none has no
        `events` key rather than an empty array, so the payload never carries a shape that
        means the same thing two ways.
        """
        data: dict[str, Any] = {}
        if self.depth is not None:
            data["depth"] = {"t": self.depth.t, "v": self.depth.v}
        if self.ceiling is not None:
            data["ceiling"] = {"t": self.ceiling.t, "v": self.ceiling.v}
        if self.temperature is not None:
            data["temperature"] = {"t": self.temperature.t, "v": self.temperature.v}
        if self.pressure:
            data["pressure"] = [
                {"gas_number": cylinder.gas_number, "t": cylinder.t, "v": cylinder.v} for cylinder in self.pressure
            ]
        if self.events:
            data["events"] = [
                # Keys a value was never established for are left out rather than written
                # as null, the same way an absent channel is - so `gas_number` present and
                # null can't come to mean something different from absent.
                {"t": event.t, "type": event.type.value}
                | ({"gas_number": event.gas_number} if event.gas_number is not None else {})
                | ({"label": event.label} if event.label is not None else {})
                for event in self.events
            ]
        return data


@dataclass(frozen=True, slots=True)
class LoadedProfile:
    """A stored profile's series plus the span the chart's x axis has to cover."""

    duration_seconds: int
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProfileGasAttribution:
    """A dive's per-cylinder attribution and the span it was derived over.

    The two travel together because they are the two halves of one fraction: "these
    figures cover 39 of the 77 minutes recorded" is only falsifiable if both numbers come
    off the same profile row. The dive's own `duration` is the diver's record and may have
    been edited, which would make the fraction say whatever the edit said.

    `duration_seconds` is the profile's full span rather than the depth channel's, which
    is what the attribution actually walked. The two differ by seconds where they differ
    at all - a device goes on logging temperature a moment past the last depth reading -
    and the profile's span is the one already stored, already meant by "the recorded
    dive", and the one a client can line up against `DiveProfileInfo`.
    """

    duration_seconds: int = 0
    entries: list[GasAttribution] = field(default_factory=list)


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


def _rebase_events(parsed: ParsedProfileSchema, origin: float) -> list[ProfileEvent]:
    """Put the file's markers on the same integer-second axis as the channels.

    **Clamped at zero rather than dropped below it.** An event that precedes the first
    sample is the ordinary case, not a corrupt one: a Suunto XML export numbers its samples
    from `<Time>1</Time>` while recording the dive's opening gas selection at
    `<GasChangeTime>0</GasChangeTime>`, so rebasing puts it at -1. Discarding it would lose
    which gas a dive *started* on - the one marker a two-gas dive most needs, and the one
    Phase 4's per-tank attribution has to begin from. There is nowhere else on a chart for
    "before the first reading" to go, so it goes at the start.

    **The high end is deliberately not clamped**, and the asymmetry is the point rather than
    an oversight. Zero is where the dive begins for every format, so pinning to it moves a
    marker by a second or two onto a boundary that is real. There is no equivalent at the
    other end: `duration_seconds` is the span of the *samples*, and a device goes on
    recording after the last one - a Suunto Ocean writes 8 292 samples of which 395 carry
    depth, and a FIT `user_marker` can be pressed after the final `record`. A marker there
    happened when the file says it happened, and dragging it back onto the last sample would
    invent a time to keep it on screen. A chart that draws past its x domain is the chart's
    to clip.

    The origin is the *sample* channels' - see `normalize`. Events are sorted here rather
    than being required to arrive sorted, because unlike the sample channels there is only
    one stream of them and nothing a parser knows that this doesn't: the XML export lists
    gas changes nested inside each `<DiveMixture>`, so file order is cylinder order, not
    time order. A stable sort, so two markers on the same second keep the order the file
    listed them in.

    Deduped on the whole event, keeping the first. Rounding onto integer seconds is what
    makes this necessary: the same `GasSwitch` can arrive under both `Events` and
    `DiveEvents` on one Suunto JSON sample, and two samples a fraction of a second apart can
    repeat one `Notify` - either would otherwise stack two identical ticks on one pixel.

    `label` is truncated here rather than bounded on the schema, which would raise: a file
    whose one long alert took its depth curve down with it is exactly what the never-fail
    contract on `extract_profile` exists to prevent. One place, so all three formats inherit
    it, the same way `ceiling_cm` holds the zero rule.
    """
    seen: set[tuple[int, ProfileEventType, int | None, str | None]] = set()
    ordered: list[ProfileEvent] = []
    for event in sorted(parsed.events, key=lambda event: event.t):
        label = event.label[:MAX_LABEL_CHARS] if event.label is not None else None
        key = (max(0, round(event.t - origin)), event.type, event.gas_number, label)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(ProfileEvent(t=key[0], type=event.type, gas_number=event.gas_number, label=label))
    return ordered


def normalize(parsed: ParsedProfileSchema) -> NormalizedProfile | None:
    """Turn a parser's raw per-channel arrays into the stored shape, or `None` if empty.

    Format-independent work, done exactly once here rather than in each parser: rebasing
    the axis to zero, rounding to integer seconds, deduping collisions, and dropping
    channels that turned out to carry nothing.

    The origin is the earliest reading across *all* channels, not each channel's own
    first sample: the channels share one x axis on the chart, so shifting them
    independently would slide the temperature curve off the depth curve it is meant to
    line up with.

    **Events do not get a vote on the origin**, though they are rebased against it. They
    are markers a device wrote alongside the samples rather than a stream with its own
    cadence, and letting one of them be the earliest thing in the file would slide every
    curve away from the axis the samples define. A file consisting only of events has no
    profile to draw and returns `None` for the same reason a file of no readings does.
    """
    channels: list[list[tuple[float, int]]] = []
    depth_points = list(zip(parsed.depth.t, parsed.depth.v, strict=True)) if parsed.depth else []
    ceiling_points = list(zip(parsed.ceiling.t, parsed.ceiling.v, strict=True)) if parsed.ceiling else []
    temperature_points = (
        list(zip(parsed.temperature.t, parsed.temperature.v, strict=True)) if parsed.temperature else []
    )
    pressure_points = [
        (cylinder.gas_number, list(zip(cylinder.t, cylinder.v, strict=True))) for cylinder in parsed.pressure
    ]
    channels = [
        points
        for points in (depth_points, ceiling_points, temperature_points, *(p for _, p in pressure_points))
        if points
    ]

    if not channels:
        return None

    # Each channel is sorted (the schema validates it), so its first timestamp is its
    # minimum. The union across channels is not sorted, hence the `min`.
    origin = min(points[0][0] for points in channels)

    return NormalizedProfile(
        depth=_rebase(depth_points, origin) if depth_points else None,
        ceiling=_rebase(ceiling_points, origin) if ceiling_points else None,
        temperature=_rebase(temperature_points, origin) if temperature_points else None,
        pressure=[
            ProfilePressureSeries(gas_number=gas_number, t=series.t, v=series.v)
            for gas_number, series in (
                (number, _rebase(points, origin)) for number, points in pressure_points if points
            )
        ],
        events=_rebase_events(parsed, origin),
    )


def derive_gas_attribution(profile: NormalizedProfile) -> list[GasAttribution]:
    """Which gas was breathed for how long, and how deep, from the switches the device recorded.

    The one fact a multi-tank dive is missing. Cylinder volumes and pressures are already
    on the mixtures; what nothing in the log records is *when* each was breathed, without
    which a staged deco bottle and a back gas are both divided by the whole dive's average
    depth - which is exactly why `compute_gas_use` refuses a dive with more than one
    cylinder (see its docstring).

    **Gas-switch events are the only source, deliberately.** Two others were considered:

    - *Per-cylinder pressure activity* - reading "this tank was being breathed" off the
      stretch where its pressure falls. The corpus says it is both unavailable and unsound.
      Unavailable: all 19 multi-gas exports in it carry exactly **one** pressure channel,
      because a diver has one transmitter and it stays on the back gas, so there is never a
      second curve to compare against. Unsound: tank pressure moves with temperature, and
      the same dive proves it - `Dive_2025-06-03-1215` records the back gas at 122.44 bar
      at the switch, and its own transmitter goes on to read 117.8 bar at the surface, 4.6
      bar "used" by a cylinder nobody was breathing.
    - *A single-gas fallback* - attributing the whole dive to the one cylinder when a file
      records no switches at all. It would be dead code: a one-mixture dive already yields
      a figure through `compute_gas_use` with no attribution involved, and no file in the
      corpus carries a pressure channel without a gas-switch event beside it.

    A switch before the first depth sample is the dive *starting* on that gas - the
    ordinary case, since a Suunto records the opening selection at t=0 (see
    `_rebase_events`) - so it is clipped forward to the first sample rather than dropped.
    A stretch *before* the first switch, however, is left unattributed: nothing says what
    was breathed then, and `compute_multi_tank_gas_use` reports the shortfall as
    `attributed_seconds` rather than quietly dividing a cylinder's gas by less time than it
    was breathed for.

    One entry per gas number rather than per interval, because the pressures it will be
    joined to belong to a cylinder rather than to a stretch of the dive: a diver who
    returns to their back gas for the ascent has two intervals on it and one tank.
    Ordered by when each gas was first breathed - the order a diver lists cylinders in.

    **`seconds` is wall clock and `mean_depth_cm` is an unweighted mean of the samples
    inside it**, which are the same measure only while the depth channel's cadence is
    even. Every format in the corpus samples depth on a fixed interval (10 s or 20 s for
    the Suunto XML export, ~11 s for an Ocean, 1 Hz for FIT), and this runs before
    `downsample`, so nothing has thinned them unevenly either. Where it would bite is a
    sensor dropout - a gap in `t` inside one stretch - which would weight the mean toward
    whichever side was still recording while `seconds` counts the gap in full. Interval
    weighting would fix that and is deliberately not done: it would need an edge rule at
    each end of every stretch, and there is no dive in the corpus for the rule to be
    checked against.

    At a boundary the two halves also count the sample *on* it differently: a stretch's
    seconds run up to the next switch's second, while the sample taken at that second is
    assigned to the gas being switched to. So one reading sits on the far side of the
    boundary from the second it was counted in - one sample out of tens or hundreds, and
    the alternative (counting it into the stretch that was ending) is no more correct, since
    the switch happened at some unrecorded instant within that sampling interval either way.

    **Runs before `downsample`**, which is what `finalize_profile` exists to sequence.
    Min/max bucketing keeps each bucket's extremes and discards everything between them,
    so a mean taken afterwards would be a mean of the dive's peaks and troughs rather than
    of the dive.
    """
    if profile.depth is None:
        return []

    switch_times: list[int] = []
    switch_gases: list[int] = []
    first_sample, last_sample = profile.depth.t[0], profile.depth.t[-1]
    for event in profile.events:
        if event.type is not ProfileEventType.GAS_SWITCH or event.gas_number is None:
            continue
        # Sorted by `normalize`, so the first switch at or before the first sample is the
        # gas the dive began on and any earlier one is superseded by it.
        moment = max(event.t, first_sample)
        if moment > last_sample:
            break
        if switch_gases and switch_gases[-1] == event.gas_number:
            # The same gas selected twice running is one stretch on it, not two. Suunto
            # writes this whenever a diver browses the gas list without changing anything.
            continue
        if switch_times and switch_times[-1] == moment:
            # Two switches on one second: the later one is what the diver ended up on, the
            # same last-reading-wins rule `_rebase` applies to a channel.
            switch_gases[-1] = event.gas_number
            continue
        switch_times.append(moment)
        switch_gases.append(event.gas_number)

    if not switch_times:
        return []

    seconds: dict[int, int] = {}
    for index, (moment, gas_number) in enumerate(zip(switch_times, switch_gases, strict=True)):
        # The last stretch runs to the last depth sample: a dive ends where its recording
        # does, and there is no switch marking the surface.
        until = switch_times[index + 1] if index + 1 < len(switch_times) else last_sample
        seconds[gas_number] = seconds.get(gas_number, 0) + (until - moment)

    depth_totals: dict[int, int] = {}
    depth_counts: dict[int, int] = {}
    for moment, centimeters in zip(profile.depth.t, profile.depth.v, strict=True):
        index = bisect_right(switch_times, moment) - 1
        if index < 0:
            continue
        gas_number = switch_gases[index]
        depth_totals[gas_number] = depth_totals.get(gas_number, 0) + centimeters
        depth_counts[gas_number] = depth_counts.get(gas_number, 0) + 1

    attribution = []
    for gas_number in dict.fromkeys(switch_gases):
        # A gas whose every stretch fell between two depth samples has a time but no depth
        # to normalize it against, and is dropped rather than given a borrowed one.
        #
        # So is one that was attributed no time at all, which happens when a switch rebases
        # exactly onto the last depth sample: there is no dive left after it, so the stretch
        # is zero seconds long. Dropped **here** rather than left for the consumer to
        # discard, because the two are not equivalent - an entry that claims a cylinder and
        # accounts for none of the dive lets `compute_multi_tank_gas_use` take it for a
        # cylinder that simply produced no figure, and report the remaining tanks as
        # covering the whole dive. Absent from the attribution, the same cylinder reaches
        # the branch that refuses a dive whose breathed cylinder was never attributed. A
        # switch one second later already takes that path, via the `break` above; a
        # difference of one second must not decide between a refusal and a wrong figure.
        count = depth_counts.get(gas_number, 0)
        if count == 0 or seconds[gas_number] <= 0:
            continue
        attribution.append(
            GasAttribution(
                gas_number=gas_number,
                seconds=seconds[gas_number],
                mean_depth_cm=round(depth_totals[gas_number] / count),
            )
        )
    return attribution


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

    **The first and last samples are always kept**, which min/max bucketing does not give
    for free: `min` returns the *first* of equal values, so a dive that ends with a run of
    identical readings - a diver floating at the surface, which is how a 1 Hz recording
    usually ends - picks the beginning of that run and drops the true final sample. The
    channel then stops seconds before the dive did. Invisible on a chart, and not invisible
    at all once `duration_seconds` became the denominator of a coverage fraction whose
    numerator is derived from the *full-resolution* channel: the fraction came out over
    100%. Endpoints are also just the right thing for a series that says when a dive
    started and stopped.
    """
    if len(t) <= max_points:
        return t, v

    # Two points per bucket (the min and the max), and two more for the endpoints below,
    # so the cap is what bounds the bucket count rather than the other way round.
    buckets = (max_points - 2) // 2
    span = t[-1] - t[0]

    picked: list[int] = [0]
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

    picked.append(len(t) - 1)
    # Deduped rather than guarded, because either endpoint may already have been picked as
    # its bucket's min or max. `dict.fromkeys` keeps the order, which is the increasing
    # index order every bucket appended in - so the series stays sorted by construction.
    kept = list(dict.fromkeys(picked))
    return [t[index] for index in kept], [v[index] for index in kept]


def downsample(
    profile: NormalizedProfile, max_points: int = MAX_POINTS_PER_CHANNEL, max_events: int = MAX_EVENTS
) -> NormalizedProfile:
    """Cap every channel independently. A channel already under the cap is untouched.

    Events are truncated rather than bucketed. Min/max over a window of markers means
    nothing - there is no "highest" gas switch - and thinning them would leave a chart
    showing some of a dive's switches with no way to tell that it was showing some. So the
    cap is a plain head-of-list, far above any real dive (see `MAX_EVENTS`), and hitting it
    is logged rather than passed off as a complete set.
    """

    def capped(series: ProfileSeries | None) -> ProfileSeries | None:
        if series is None:
            return None
        t, v = _downsample_series(series.t, series.v, max_points)
        return ProfileSeries(t=t, v=v)

    if len(profile.events) > max_events:
        logger.warning(
            "Profile carries %d events, keeping the first %d - this is far past any real dive",
            len(profile.events),
            max_events,
        )

    return NormalizedProfile(
        depth=capped(profile.depth),
        ceiling=capped(profile.ceiling),
        temperature=capped(profile.temperature),
        pressure=[
            ProfilePressureSeries(gas_number=cylinder.gas_number, t=t, v=v)
            for cylinder, (t, v) in (
                (cylinder, _downsample_series(cylinder.t, cylinder.v, max_points)) for cylinder in profile.pressure
            )
        ],
        events=profile.events[:max_events],
        # Passed through untouched: it was derived from the full-resolution channels on
        # purpose (see `derive_gas_attribution`), so thinning them must not disturb it.
        gas_attribution=profile.gas_attribution,
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
        return finalize_profile(parser.parse_profile(content))
    except DiveParseError:
        logger.warning("Profile extraction failed for a %s file: malformed samples", parser.key, exc_info=True)
        return None
    except Exception:
        logger.exception("Unexpected error extracting a profile from a %s file", parser.key)
        return None


def finalize_profile(parsed: ParsedProfileSchema | None) -> NormalizedProfile | None:
    """Normalize and cap an already-parsed profile.

    Split out of `extract_profile` for `_extract_all`, which gets its `parsed` from
    `parse_all` and would otherwise have to repeat these three steps - and repeat them
    exactly, since a profile normalized one way at attach and another way in a backfill
    is the kind of drift nothing would notice. **Raises**, unlike its caller: the
    never-raises promise belongs to the wrappers, and this is the shared middle.

    The order of the last two steps is load-bearing rather than incidental: gas attribution
    reads a mean depth off the depth channel, and `downsample` keeps each bucket's extremes
    and throws away the samples between them. Deriving it here, where both are in view,
    is what stops the two ever being sequenced the other way round.
    """
    if parsed is None:
        return None
    normalized = normalize(parsed)
    if normalized is None:
        return None
    return downsample(replace(normalized, gas_attribution=derive_gas_attribution(normalized)))


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
    ceiling_values = profile.ceiling.v if profile.ceiling else []
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
            # A count rather than `None` when there are none: this extractor version looked
            # and found nothing, which is a different fact from an older one never having
            # looked - and the difference is what a later backfill selects on.
            event_count=len(profile.events),
            max_depth_cm=max(depth_values) if depth_values else None,
            max_ceiling_cm=max(ceiling_values) if ceiling_values else None,
            min_temperature_c10=min(temperature_values) if temperature_values else None,
            max_temperature_c10=max(temperature_values) if temperature_values else None,
            min_pressure_bar10=min(pressure_values) if pressure_values else None,
            max_pressure_bar10=max(pressure_values) if pressure_values else None,
            # A list rather than `None` when there is nothing to attribute, for the same
            # reason `event_count` is a count rather than `None`: this extractor looked.
            gas_attribution=[entry.model_dump() for entry in profile.gas_attribution],
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
    explicit `undefer`, which is what makes every other query here cheap by default.

    Detached before returning, because an attached instance keeps its undeferred payload
    materialized for the life of the session. That cost nothing while the only caller was a
    single-dive route, and stopped being free when the full export began calling this once
    per dive - twice, in fact, since `export.json` and `dives.uddf` each embed every
    profile. Without this an archive of a few hundred dives would hold every one of them,
    which is what `services/export/loader.py` promises it does not.

    `load_dive_file` and `load_certification_file` used to be the siblings this pointed at.
    They are not any more: their payloads left Postgres for the files volume, so both read
    explicit columns and there is no longer an ORM instance to detach. This is now the only
    place in the app where the problem exists at all - `dive_profile.data` is JSONB, stays
    in the database deliberately, and so still arrives attached.

    Every caller copies what it needs out of `LoadedProfile` below and none of them touch
    the row again, so detaching is invisible to all three.
    """
    stmt = select(DiveProfile).where(DiveProfile.dive_id == dive_id).options(undefer(DiveProfile.data))
    profile = (await db.execute(stmt)).scalar_one_or_none()
    if profile is None:
        return None

    db.expunge(profile)
    return LoadedProfile(duration_seconds=profile.duration_seconds, data=profile.data)


def to_read_schema(loaded: LoadedProfile) -> DiveProfileRead:
    """The stored payload as the wire shape, integers untouched."""
    data = loaded.data or {}
    depth = data.get("depth")
    ceiling = data.get("ceiling")
    temperature = data.get("temperature")
    return DiveProfileRead(
        duration_seconds=loaded.duration_seconds,
        depth=DiveProfileSeries(t=depth["t"], v=depth["v"]) if depth else None,
        ceiling=DiveProfileSeries(t=ceiling["t"], v=ceiling["v"]) if ceiling else None,
        temperature=DiveProfileSeries(t=temperature["t"], v=temperature["v"]) if temperature else None,
        pressure=[
            DiveProfilePressureSeries(gas_number=cylinder["gas_number"], t=cylinder["t"], v=cylinder["v"])
            for cylinder in data.get("pressure") or []
        ],
        events=[
            # `.get` rather than `[...]` for the two optional keys, because `to_data` omits
            # them rather than writing nulls - so a row written by any version of this
            # module reads back without a `KeyError`.
            DiveProfileEvent(
                t=event["t"],
                type=ProfileEventType(event["type"]),
                gas_number=event.get("gas_number"),
                label=event.get("label"),
            )
            for event in data.get("events") or []
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
        DiveProfile.event_count,
        DiveProfile.max_depth_cm,
        DiveProfile.max_ceiling_cm,
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
        # A dive that never owed a decompression stop has no ceiling column to draw, which
        # is exactly what a NULL extreme means here - the same derivation as the others,
        # and the reason a ceiling of zero is stored as no reading rather than as zero.
        if row.max_ceiling_cm is not None:
            channels.append("ceiling")
        if row.min_temperature_c10 is not None:
            channels.append("temperature")
        if row.min_pressure_bar10 is not None:
            channels.append("pressure")

        infos[row.dive_id] = DiveProfileInfo(
            uuid=row.uuid,
            duration_seconds=row.duration_seconds,
            depth_sample_count=row.depth_sample_count,
            # Deliberately not folded into `channels`: events aren't a curve, and a client
            # deciding whether to offer a "markers" toggle wants the count, not membership
            # of a list of axes.
            event_count=row.event_count,
            channels=channels,
            max_depth=_scaled(row.max_depth_cm, DEPTH_SCALE),
            max_ceiling=_scaled(row.max_ceiling_cm, CEILING_SCALE),
            min_temperature=_scaled(row.min_temperature_c10, TEMPERATURE_SCALE),
            max_temperature=_scaled(row.max_temperature_c10, TEMPERATURE_SCALE),
            min_pressure=_scaled(row.min_pressure_bar10, PRESSURE_SCALE),
            max_pressure=_scaled(row.max_pressure_bar10, PRESSURE_SCALE),
            updated_at=row.updated_at,
        )
    return infos


async def get_gas_attribution_for_dives(db: AsyncSession, *, dive_ids: list[int]) -> dict[int, ProfileGasAttribution]:
    """Resolve several dives' per-cylinder attribution in one query.

    Separate from `get_profile_infos_for_dives` although both read summary columns of the
    same row, because the two answer different questions for different callers: that one
    builds the `profile` a response carries, this one feeds `compute_multi_tank_gas_use`
    and is never serialized. `gas_use_history` needs this and none of the rest, over every
    dive a user has - so folding it into the other would mean either a second query there
    anyway or eleven columns fetched to use one.

    A dive with no profile, or one extracted before attribution existed, comes back as an
    empty `ProfileGasAttribution` rather than being absent: "nothing to attribute" is what
    the caller does with either, and a NULL column on a stale row means the backfill has
    not reached it yet, not that the file was silent.

    A stored entry that no longer validates is dropped with a warning rather than raising.
    The column is a summary the extractor can rewrite at will, and a shape older than the
    current one must degrade to "this dive has no per-tank figure" - never to a 500 on the
    dive detail page, which is where this is read.
    """
    if not dive_ids:
        return {}

    stmt = select(DiveProfile.dive_id, DiveProfile.duration_seconds, DiveProfile.gas_attribution).where(
        DiveProfile.dive_id.in_(set(dive_ids))
    )

    attribution: dict[int, ProfileGasAttribution] = {dive_id: ProfileGasAttribution() for dive_id in dive_ids}
    for row in await db.execute(stmt):
        try:
            entries = [GasAttribution.model_validate(entry) for entry in row.gas_attribution or []]
        except ValidationError:
            logger.warning("Ignoring unreadable gas attribution stored for dive %s", row.dive_id, exc_info=True)
            continue
        attribution[row.dive_id] = ProfileGasAttribution(duration_seconds=row.duration_seconds, entries=entries)
    return attribution


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

        try:
            file = await load_dive_file(db, dive_id=row.dive_id)
        except BlobMissingError:
            # The row is there and its file is not - data loss or an unmounted volume,
            # not a race. Counted rather than raised, so a run over a half-restored volume
            # reports how many dives are in this state instead of dying on the first.
            logger.error("Skipping dive %s: its stored file is missing from the volume", row.dive_id)
            failed += 1
            continue
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
