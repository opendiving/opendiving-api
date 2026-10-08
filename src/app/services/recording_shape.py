"""A DiveJSON recording as the columns and samples this app stores, whichever door it came in by.

Two paths turn a document's recording into a `dive_recording` row and a profile: logbook
import, which reads a document it was handed, and a dive-computer file attached to a dive,
which the one reader (`services/dive_reader.py`) converts into a document first. Both call
these functions, so **the door does not matter**: the recording an attach creates from a file
is the recording an import creates from the same file - device, settings, readouts, channels,
labels and gate figures.

Session-free and pure, which is what lets the attach path call them from a thread over each of
a recording's files. What they need to say about a value they drop goes through `drop`, one
sentence per value: the import turns it into a note on the dive it is planning, and the attach
path, which has nowhere to show one, discards it.

**The profile comes back shaped and nothing more** - neither attributed nor capped. The gas
attribution reads the switches against the depth channel and the cap keeps each bucket's
extremes, so both have to run once over a recording's merged channels rather than per file
(`dive_profiles.attribute_and_cap`).
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from divejson import InWater, in_water

from ..core.utils.datetime_offset import split_local_start_time
from ..schemas.dive_profile import DEPTH_SCALE, MILLISECONDS_PER_SECOND, SINGLE_SERIES_CHANNELS, ProfileEventType
from ..schemas.logbook_import import ImportDecoModel, ImportDevice, ImportDive, ImportProfile, ImportRecording
from .dive_profiles import (
    MAX_LABEL_CHARS,
    NormalizedProfile,
    ProfileEvent,
    ProfilePressureSeries,
    ProfileSeries,
    with_channels,
)
from .dive_recordings import DECO_MODEL_COLUMNS, DEVICE_COLUMNS

# One sentence about a value that could not be kept.
Drop = Callable[[str], None]

# Postgres `Integer` is 32-bit, and **the format puts no ceiling on any of its integer
# members** - a dive's `number` is a bare `{"type": "integer"}` in the published schema, and
# `duration`, `visibility`, the profile's own `duration` and every `values` entry carry
# only a minimum. So a *conforming* document can hold a number this app's columns cannot,
# and a converter with a unit bug (a duration in microseconds, a depth in micrometres) is
# exactly how one arrives. Unbounded, that is SQLSTATE 22003 raised from the middle of the
# write: the whole logbook refused over one number, which is the failure every other bound
# here exists to prevent. A `CheckConstraint` census cannot see this, because the limit is
# the column's *width* rather than a rule written on it.
INT32_MAX = 2**31 - 1
INT32_MIN = -(2**31)


@dataclass(frozen=True, slots=True)
class Bound:
    """One column's bound, mirrored from the `CheckConstraint` that enforces it.

    Mirrored rather than derived, on the same terms as `schemas/parsed_dive.py`'s
    validators and for the same reason: a value the database refuses must not take the
    write it rode in on with it. `tests/test_logbook_import.py` counts the single-column
    `ck_dive_*` and `ck_dive_mixture_*` constraints against these tables, so a new one
    cannot be added without a guard here.
    """

    field: str
    ok: Callable[[float], bool]
    message: str


# A recording's readouts, from `models/dive_recording.py` - keyed by the format's member name,
# which is the column's for all but the surface pressure (`READOUT_COLUMN`).
READOUT_BOUNDS: tuple[Bound, ...] = (
    Bound("surface_pressure", lambda value: 0.4 <= value <= 1.2, "surface pressure must be between 0.4 and 1.2 bar"),
    Bound("cns_start", lambda value: value >= 0, "a CNS reading cannot be negative"),
    Bound("cns_end", lambda value: value >= 0, "a CNS reading cannot be negative"),
    Bound("otu_start", lambda value: value >= 0, "an OTU reading cannot be negative"),
    Bound("otu_end", lambda value: value >= 0, "an OTU reading cannot be negative"),
)
READOUT_COLUMN = {"surface_pressure": "surface_pressure_bar"}

# The channels §6.4 floors at zero - every one the computer computed rather than measured.
# Depth, ceiling and temperature are absent because a signed reading is real in all three: a
# temperature below zero is ordinary, and a depth of zero at the surface is what a Suunto
# Ocean records.
UNSIGNED_CHANNELS = frozenset(SINGLE_SERIES_CHANNELS) - {"depth", "ceiling", "temperature"}

# The deco model's three integers, from `models/dive_recording.py`. The gradient factors are
# 0-100 because the member is a whole percent of the M-value and no computer offers a setting
# above it - deliberately *not* the per-sample `gradient_factor` channel above, which is
# uncapped because a GF99 past 100 is a real reading.
#
# **`conservatism` is bounded only by the column's width**, which makes it the one entry on
# any of these lists with no floor: it is the device's own scale, and Suunto's P-1 is a
# genuine `-1`. Reading a negative as an absent-marker here, which is right for every channel,
# would delete a real setting.
DECO_MODEL_BOUNDS: tuple[Bound, ...] = (
    Bound("gf_low", lambda value: 0 <= value <= 100, "a gradient factor must be between 0 and 100 percent"),
    Bound("gf_high", lambda value: 0 <= value <= 100, "a gradient factor must be between 0 and 100 percent"),
    Bound(
        "conservatism",
        lambda value: -INT32_MAX <= value <= INT32_MAX,
        "a conservatism setting must be a whole number this app can store",
    ),
)


def finite(value: float | None) -> bool:
    """`NaN` and `inf` are not readings, whatever the column's bound says.

    Checked before every comparison rather than folded into one, because a `NaN` compares
    `False` against `<` and `>` alike and so slips through any one-sided guard - the exact
    lesson `_ParserOutput._drop_non_finite` is written up for. Python's `json` accepts a bare
    `NaN` token, so a document really can carry one.
    """
    return value is None or math.isfinite(value)


def within_int32(values: Sequence[int]) -> bool:
    """Every element storable in a Postgres `Integer` column."""
    return all(INT32_MIN <= value <= INT32_MAX for value in values)


def bounded(source: Any, bounds: Sequence[Bound], drop: Drop) -> dict[str, Any]:
    """Every bounded member of one record, with the unstorable ones dropped."""
    kept: dict[str, Any] = {}
    for bound in bounds:
        value = getattr(source, bound.field)
        if value is None:
            continue
        if not finite(value):
            drop(f"A value for `{bound.field}` was not a number, and was dropped")
            continue
        if not bound.ok(value):
            drop(f"A value for `{bound.field}` was dropped: {bound.message}")
            continue
        kept[bound.field] = value
    return kept


@dataclass(frozen=True, slots=True)
class ShapedRecording:
    """One recording's columns and samples, ready for either writer.

    `device`, `deco_model` and `readouts` are keyed by **column** name, the absent members
    left out, so a writer spreads them into the row and never has to know that the format
    says `brand` and the column says `device_brand`. `profile` is shaped and not attributed
    or capped - see the module docstring.
    """

    device: dict[str, Any]
    mode: str | None
    deco_model: dict[str, Any]
    salinity: str | None
    readouts: dict[str, float]
    start_time: datetime | None
    utc_offset_minutes: int | None
    profile: NormalizedProfile | None


def shape_series(series: Any, label: str, drop: Drop, *, unsigned: bool = False) -> ProfileSeries | None:
    """One channel, or `None` with a note. Spec §6.5's rules, exactly.

    `unsigned` is §6.4's floor on the six decompression channels: none of them is a
    quantity that runs below zero, and a negative in one is what several devices write to
    mean "no figure". Depth, ceiling and temperature keep the signed reading - a
    temperature below zero is ordinary, and so is a depth of zero at the surface - which
    is why this is a parameter rather than a rule applied to every channel.

    The whole channel goes rather than the offending sample: there is no half of a series
    to keep, and a document whose `values` and `times` no longer line up is worse than one
    channel short.
    """
    if series is None:
        return None
    times, values = series.times, series.values
    if len(times) != len(values):
        drop(f"The {label} channel had mismatched times and values, and was dropped")
        return None
    if not times:
        # "A channel with no readings must be omitted, not empty" (§6.5).
        return None
    if times[0] < 0 or any(later <= earlier for earlier, later in zip(times, times[1:], strict=False)):
        drop(f"The {label} channel's times were not increasing from zero, and was dropped")
        return None
    # The stored `data` payload is JSONB and holds any integer, but the summary columns
    # `store_profile` derives from these - `max_depth_cm` and the extremes beside it, and the
    # span - are `Integer`. A channel carrying a value outside that width goes whole.
    if not (within_int32(times) and within_int32(values)):
        drop(f"The {label} channel carried readings this app cannot store, and was dropped")
        return None
    if unsigned and any(value < 0 for value in values):
        drop(f"The {label} channel carried a negative reading, and was dropped")
        return None
    return ProfileSeries(t=list(times), v=list(values))


def shape_events(source: ImportProfile, drop: Drop) -> list[ProfileEvent]:
    """The markers, sorted, deduped and label-bounded.

    Sorted here because `derive_gas_attribution` walks them in time order and nothing
    upstream guarantees it.

    **An absent or unrecognized `type` reads as `OTHER`**, which is the boundary this
    app's storage keeps with the format: §6.6 makes `type` OPTIONAL and spells
    "unclassified" as its absence, while a JSONB key and an enum both want a value, so
    `OTHER` is what that absence is stored as. `_unknown_is_absent` has already turned a
    value from a later minor version into `None` before it reaches here.

    Which makes the `label` REQUIRED there, and that is §6.6's rule rather than this
    app's: an event that is neither classified nor labelled carries no information at
    all, so it is dropped with a note.

    `label` is truncated rather than refused: a file whose one long alert took its depth
    curve down with it would lose far more than the alert.
    """
    usable: list[tuple[int, ProfileEventType, int | None, str | None]] = []
    for event in source.events:
        if event.time is None:
            # `time` is REQUIRED (spec §6.6); without it there is no marker to place.
            drop("A profile event with no time was dropped")
            continue
        label = event.label[:MAX_LABEL_CHARS] if event.label is not None else None
        kind = event.type if event.type is not None else ProfileEventType.OTHER
        if kind is ProfileEventType.OTHER and not (label or "").strip():
            drop("An unclassified event with no label was dropped")
            continue
        usable.append((max(0, event.time), kind, event.gas_number, label))

    seen: set[tuple[int, ProfileEventType, int | None, str | None]] = set()
    ordered: list[ProfileEvent] = []
    for key in sorted(usable, key=lambda entry: entry[0]):
        if key in seen:
            continue
        seen.add(key)
        ordered.append(ProfileEvent(t=key[0], type=key[1], gas_number=key[2], label=key[3]))
    return ordered


def shape_profile(source: ImportProfile | None, drop: Drop) -> NormalizedProfile | None:
    """A recording's samples as the stored shape, or nothing, with a note.

    Built directly rather than resampled: a document's `times` are already integer
    milliseconds from the recording's start (spec §6.5), which is the stored axis, so there
    is nothing to round or place. Every axis entry keeps an int32 bound, which in
    milliseconds is twenty-four days - still far past any dive.
    """
    if source is None:
        return None
    # Every single-series channel through the same check, driven by the channel tuple, so a
    # channel added to the format cannot ship here unchecked.
    channels = {
        channel: shape_series(getattr(source, channel), channel, drop, unsigned=channel in UNSIGNED_CHANNELS)
        for channel in SINGLE_SERIES_CHANNELS
    }
    pressures: list[ProfilePressureSeries] = []
    for series in source.pressures:
        if series.gas_number is None or series.gas_number < 0:
            drop("A pressure channel with no gas number was dropped")
            continue
        checked = shape_series(series, f"pressure (gas {series.gas_number})", drop)
        if checked is not None:
            pressures.append(ProfilePressureSeries(t=checked.t, v=checked.v, gas_number=series.gas_number))

    if not any(channels.values()) and not pressures:
        if any(getattr(source, channel) for channel in SINGLE_SERIES_CHANNELS) or source.pressures:
            drop("A recording's profile carried no usable channel, and was dropped")
        return None

    return with_channels(channels, pressure=pressures, events=shape_events(source, drop))


def shape_device(source: ImportDevice | None, drop: Drop) -> dict[str, Any]:
    """The device's members keyed by column, the absent ones left out."""
    if source is None:
        return {}
    device = {
        column: value for member, column in DEVICE_COLUMNS.items() if (value := getattr(source, member)) is not None
    }
    counter = device.get("device_dive_number")
    if isinstance(counter, int) and not 0 <= counter <= INT32_MAX:
        drop("A device's own dive counter was outside the storable range")
        device.pop("device_dive_number")
    return device


def shape_readouts(source: ImportRecording, drop: Drop) -> dict[str, float]:
    """The device's readouts keyed by column, the absent and unstorable ones left out."""
    return {
        READOUT_COLUMN.get(member, member): value for member, value in bounded(source, READOUT_BOUNDS, drop).items()
    }


def recording_start(dive: ImportDive, source: ImportRecording | None, drop: Drop) -> datetime | None:
    """A recording's own start, or the dive's where it states none (§6.4a) - and `None`
    where that is a bare date, which no recording's start can be.

    A recording's start is a date-time and never a bare date; one that arrives as a date
    is read as absent, with a note, rather than as midnight.
    """
    fallback = dive.started_at if isinstance(dive.started_at, datetime) else None
    if source is None:
        return fallback
    if source.started_at is not None and not isinstance(source.started_at, datetime):
        outcome = "so the dive's start was used" if fallback is not None else "and none was stored"
        drop(f"A recording's start carried no time of day, {outcome}")
    if isinstance(source.started_at, datetime):
        return source.started_at
    return fallback


def shape_deco_model(source: ImportDecoModel | None, drop: Drop) -> dict[str, Any]:
    """One recording's deco model, as the columns that carry it - keyed by column name.

    **The gradient-factor pair is enforced here and not left to the `CheckConstraint`**,
    which is the difference between one bad reading and a lost write: a pair that arrives
    inverted would otherwise reach the database, and an `IntegrityError` inside an import
    transaction takes every dive in the archive with it. An oxygen and a helium summing past
    100 go the same way in the importer - neither number says which of the two is wrong, so
    both go and the record stays.

    Both-or-neither goes the same way and for §6.4c's own reason: one gradient factor
    alone names no setting.

    An empty dict where the document recorded no model: every value is a column a writer may
    write, and a member the document did not carry is a column it must leave alone.
    """
    if source is None:
        return {}

    kept = bounded(source, DECO_MODEL_BOUNDS, drop)
    gf_low, gf_high = kept.get("gf_low"), kept.get("gf_high")
    if (gf_low is None) != (gf_high is None):
        drop("A recording's deco model named one gradient factor without the other, so it went")
        gf_low = gf_high = None
    elif gf_low is not None and gf_high is not None and gf_low > gf_high:
        drop("A recording's low gradient factor was above its high one, so both went")
        gf_low = gf_high = None

    members: dict[str, Any] = {
        "algorithm": None if source.algorithm is None else source.algorithm.value,
        # `None` rather than `""` for a blank name: the column is nullable, and `""` would be
        # a model named nothing rather than no model recorded.
        "name": (source.name or "").strip() or None,
        "gf_low": gf_low,
        "gf_high": gf_high,
        "conservatism": kept.get("conservatism"),
    }
    return {DECO_MODEL_COLUMNS[member]: value for member, value in members.items() if value is not None}


def shape_recording(dive: ImportDive, source: ImportRecording, drop: Drop) -> ShapedRecording | None:
    """One recording of `dive`, or `None` with a note when it describes nothing.

    **A recording carrying none of its device, its profile, its files and a readout is
    dropped**, which is §3's beyond-schema rule 4 applied on the way in rather than asserted
    about the way out: an object that describes nothing would become a row nothing can
    render, with a `start_time` and no reason to exist. A readout alone is a record - a
    computer's own arithmetic, which a hand-logged dive in Subsurface keeps with no samples
    at all - and a setting alone is not.

    `started_at` absent means the dive's (§6.4a), so it is substituted here rather than left
    NULL - a reader that treated the absence as "unknown" would put every single-computer
    recording outside every gate's reach. The exception is a dive whose start is a bare date:
    a day is no recording's start, so the recording keeps a NULL one and its axis counts from
    an unknown time that day, until a file attached to it states one.
    """
    device = shape_device(source.device, drop)
    profile = shape_profile(source.profile, drop)
    readouts = shape_readouts(source, drop)
    if not device and profile is None and not source.source_files and not readouts:
        drop("A recording described no device, no samples, no file and no reading, and was dropped")
        return None

    started_at = recording_start(dive, source, drop)
    start_time, offset_minutes = (None, None) if started_at is None else split_local_start_time(started_at)
    return ShapedRecording(
        device=device,
        mode=None if source.mode is None else source.mode.value,
        deco_model=shape_deco_model(source.deco_model, drop),
        salinity=None if source.salinity is None else source.salinity.value,
        readouts=readouts,
        start_time=start_time,
        utc_offset_minutes=offset_minutes,
        profile=profile,
    )


def gate_figures(profile: NormalizedProfile | None) -> tuple[int | None, float | None]:
    """A recording's two match figures, `(duration, max_depth)`, from its samples.

    **The samples are the only source, on every path.** A DiveJSON Recording carries no
    scalars of its own, so the samples' span in whole seconds and their deepest reading in
    metres are what the strict gate compares; a recording with no samples has no figures and
    cannot be strict-matched, which is honest rather than lossy. Off the stored integer
    centimetres, which a capped profile keeps exactly: `downsample` keeps each channel's
    first and last sample and each bucket's extremes.
    """
    if profile is None:
        return None, None
    duration = round(profile.duration / MILLISECONDS_PER_SECOND)
    depth = profile.depth
    return duration, None if depth is None or not depth.v else max(depth.v) / DEPTH_SCALE


def in_water_of(profile: NormalizedProfile | None) -> InWater | None:
    """A dive's time in the water and its mean depth over that time, from a profile's depth
    channel: `divejson.in_water`, the one implementation of the rule, over the centimetres a
    profile holds. `None` where no interval counts, which leaves the caller's figures standing.
    """
    depth = None if profile is None else profile.depth
    if depth is None:
        return None
    return in_water(zip(depth.t, (Decimal(value) / DEPTH_SCALE for value in depth.v), strict=True))
