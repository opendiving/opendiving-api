import json
import logging
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ...schemas.dive import DecoAlgorithm, DiveMode
from ...schemas.dive_mixture import GasRole
from ...schemas.dive_profile import (
    ParsedPressureSeries,
    ParsedProfileEvent,
    ParsedProfileSchema,
    ProfileEventType,
)
from ...schemas.parsed_dive import DiveMixtureSchema, ParsedDecoModel, ParsedDevice, ParsedDiveSchema
from .base import DiveParser
from .channels import (
    CENTIMETERS_PER_METER,
    TENTHS_PER_UNIT,
    UNSCALED,
    ceiling_cm,
    scaled_int_or_none,
    series,
    unsigned_int_or_none,
)
from .exceptions import EXTRACTION_ERRORS, DiveParseError
from .positions import (
    NO_POSITIONS,
    EntryExit,
    GeoFix,
    degrees_from_radians,
    degrees_verbatim,
    entry_and_exit,
    geo_fix,
)

logger = logging.getLogger(__name__)

# Suunto app / Suunto Ocean JSON exports report temperature in Kelvin (SI units),
# unlike the Suunto DM5 XML export which already uses Celsius.
_KELVIN_TO_CELSIUS_OFFSET = Decimal("273.15")

# Suunto D5-style JSON exports (`DeviceLog.Header.Diving.Gases`) report gas
# mixtures in SI units too: pressures in Pascal (vs. bar), tank size in cubic
# meters (vs. liters), and oxygen/helium as a 0-1 fraction (vs. a 0-100 percentage).
_PASCALS_PER_BAR = Decimal("100000")
_LITERS_PER_CUBIC_METER = Decimal("1000")
_PERCENT_PER_FRACTION = Decimal("100")
_TWO_DECIMAL_PLACES = Decimal("0.01")

# Pascal -> tenths of a bar. The depth/temperature conversions this shares with the other
# parsers live in `channels.py`; see `schemas/dive_profile.py` for the scales themselves.
_TENTH_BAR_PER_PASCAL = Decimal("0.0001")

# How many cylinders one dive may describe. Mirrors `FitParser`'s cap and exists for the
# same reason: each becomes a `DiveMixtureSchema` in the `/dive/parse` response, and a
# `GasSwitch` event is a few dozen bytes, so nothing else bounds the count.
_MAX_CYLINDERS = 16

# The brand every file this parser accepts came from. It is the format's rather than a
# field's: `can_parse` requires a `DeviceLog.Header`, which is Suunto's own envelope, and
# no export in the corpus writes a maker anywhere. Written out because a device with a
# serial and no brand cannot be lined up against the same computer's FIT export, where
# `file_id.manufacturer` decodes to `suunto`. `SuuntoXmlParser` says the same thing about
# its own format.
_BRAND = "Suunto"


def _decimal_multiply(value: float | None, factor: Decimal) -> float | None:
    """Multiply `value` by `factor` using Decimal arithmetic (see `_kelvin_to_celsius`
    for why: avoids introducing binary floating-point noise from the conversion)."""
    if value is None:
        return None
    return float(Decimal(str(value)) * factor)


def _decimal_divide(value: float | None, divisor: Decimal) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)) / divisor)


def _kelvin_to_celsius(value: float | None) -> float | None:
    if value is None:
        return None
    # Subtract via Decimal (constructed from `str(value)`, which - since `value`
    # came straight from `json.loads` on a decimal literal - reproduces that
    # literal's exact digits) rather than raw floats: 273.15 has no exact binary
    # floating-point representation, so `value - 273.15` (e.g. 293.75 - 273.15)
    # lands on noise like 20.600000000000023 instead of 20.6. Decimal arithmetic
    # avoids introducing that error in the first place, rather than rounding it
    # away afterwards.
    return float(Decimal(str(value)) - _KELVIN_TO_CELSIUS_OFFSET)


def _pascals_to_bar(value: float | None) -> float | None:
    return _decimal_divide(value, _PASCALS_PER_BAR)


def _cubic_meters_to_liters(value: float | None) -> float | None:
    return _decimal_multiply(value, _LITERS_PER_CUBIC_METER)


def _fraction_to_percent(value: float | None) -> float | None:
    return _decimal_multiply(value, _PERCENT_PER_FRACTION)


def _round_or_none(value: float | int | None) -> int | None:
    return round(value) if value is not None else None


def _round2_or_none(value: float | None) -> float | None:
    """Round a gas percentage/pressure reading to 2 decimal places - dive-computer
    gauges aren't meaningfully more precise than that. Uses Decimal (constructed
    from `str(value)`) rather than plain float rounding, consistent with
    `_kelvin_to_celsius`/`_pascals_to_bar` above."""
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(_TWO_DECIMAL_PLACES))


def _celsius_tenths(kelvin: float | None) -> int | None:
    """Kelvin straight to tenths of a degree Celsius, without a float in between."""
    if kelvin is None:
        return None
    tenths = (Decimal(str(kelvin)) - _KELVIN_TO_CELSIUS_OFFSET) * TENTHS_PER_UNIT
    return int(tenths.quantize(Decimal(1), rounding=ROUND_HALF_UP))


# `Gases[].State` onto `GasRole`. A normalization table rather than a `GasRole(state)`
# cast, because the two vocabularies are not the same one: this field is Suunto's, and
# every value it can hold that we have not seen must come out `None` rather than raise or
# become a role we invented. "Primary" is the only value the corpus has (18 of 18 gases
# across the D5 exports), so the table is honest about being nearly empty - the point is
# the lookup's *shape*, which stays right when a "Deco"/"Diluent" export finally turns up.
#
# Note this is a different thing from the `State` under `DiveHeader`/`DiveFooter`, which
# reads "OC" (open circuit - a loop type, not a role) and which this parser never touches.
_GAS_ROLE_BY_STATE = {"primary": GasRole.BOTTOM}


def _role(state: Any) -> GasRole | None:
    return _GAS_ROLE_BY_STATE.get(state.strip().lower()) if isinstance(state, str) else None


# The sample keys an event can arrive under. Both, because the export generations
# disagree: the D5 shapes write `Events`, the 2026 Ocean writes `DiveEvents`, and the Ocean
# uses `Events` for unrelated activity bookkeeping (`Lap`, `Pause`, `ArrayBegin`) at the
# same time. Reading only one of them is how `_collect_gas_switch` came to miss every gas
# switch in the D5 corpus - harmless there, since those exports carry a `Gases` block it
# never needs to fall back from, and fixed here because a chart marker has no such backup.
_EVENT_KEYS = ("Events", "DiveEvents")

# `Notify[].Type` onto the vocabulary, matched case-insensitively. A normalization table
# rather than a cast, for the same reason `_GAS_ROLE_BY_STATE` is one: this vocabulary is
# Suunto's, and a value not listed here must come out as nothing rather than be forced into
# the nearest one.
#
# **Four of the twelve, and two of them are not stops.** `Deep Stop` and `Safety Stop` are
# the stops the diver is being told to make. `Deco` is the moment the dive became a
# decompression dive and `Safety Stop Broken` is a required stop left early - both are
# occurrences the vocabulary now has a value for, which is the whole of why they stopped
# being dropped: "the format has no word for this" was the old reason and is no longer one.
#
# **None of the four carries a label**, unlike an `Alarm`/`Warning`: a `Notify`'s `Type` is
# the device's name for its own *state*, so writing "Deco" as the wording of an occurrence
# would put on the marker something the diver never read.
#
# The other eight stay dropped, each for a reason of its own rather than for want of a type.
# "Deep Stop Ahead", "Safety Stop Ahead" and "Stop done" are the prompt before and the
# confirmation after a stop this reader already marks, and carrying all three would put three
# ticks on one stop. "Gas Switch" duplicates the `GasSwitch` event in the same sample.
# "Deco Window", "NoFly Time", "Dive Time" and "Gas Available" are the computer narrating its
# own state rather than something that happened on the dive - the reason everything under
# `State` goes, applied to the four `Notify` values that are the same thing.
_STOP_TYPE_BY_NOTIFY = {
    "deep stop": ProfileEventType.DEEP_STOP,
    "safety stop": ProfileEventType.SAFETY_STOP,
    "deco": ProfileEventType.NDL_REACHED,
    "safety stop broken": ProfileEventType.SAFETY_STOP_VIOLATION,
}

# The event families that carry an alert - the ceiling breaks, ppO2 alarms and ascent-rate
# alarms a tech diver most wants marked. Rare enough to render: 69 across the 35-dive corpus.
_ALERT_NAMES = ("Alarm", "Warning")

# `Alarm`/`Warning` `Type` onto the vocabulary, matched case-insensitively. **The wording is
# what earns the type**: the vocabulary was seeded from this exact list, one value per
# distinct meaning, so each alert this format names is classified rather than arriving as an
# unlabelled tick - and the device's own wording travels beside it in `label`, because
# "Ceiling Broken" is what the diver was shown and no type says it as well.
#
# Two spellings of one occurrence share a value on purpose ("Safety Stop Broken" and
# "Mandatory Safety Stop Broken"; "Deep Stop Broken" and "Violated Deep Stop"): the type says
# what class of thing happened and the label says which wording the device used.
#
# A `Type` outside this table stays `OTHER` with that wording - the vocabulary grows when a
# file names something it has no value for, and nothing is forced into the nearest one in the
# meantime.
_ALERT_TYPES = {
    "ascent speed": ProfileEventType.ASCENT_RATE,
    "mandatory safety stop": ProfileEventType.SAFETY_STOP_MANDATORY,
    "safety stop broken": ProfileEventType.SAFETY_STOP_VIOLATION,
    "mandatory safety stop broken": ProfileEventType.SAFETY_STOP_VIOLATION,
    "deep stop broken": ProfileEventType.DEEP_STOP_VIOLATION,
    "violated deep stop": ProfileEventType.DEEP_STOP_VIOLATION,
    "ceiling broken": ProfileEventType.CEILING_VIOLATION,
    "nodecotime": ProfileEventType.NDL_REACHED,
    "po2 high": ProfileEventType.PPO2_HIGH,
    "tank pressure": ProfileEventType.PRESSURE_LOW,
    "max.depth": ProfileEventType.DEPTH_ALARM,
}

# `Header.Diving.DiveMode` onto the recording's mode, matched case-insensitively. All three
# values real files carry are gas modes of an **open-circuit** computer: `Air` and `Nitrox`
# on eight D5 exports each, and `Mixed` on the one fixture that states it.
#
# **`Gauge` and `Free` are deliberately absent.** No file in hand carries either - the D5's
# free and gauge modes write no `Header.Diving` this reader has seen - and a mapping no file
# exercises is a mapping nothing checks. A value outside the table leaves `mode` absent,
# which is what §6.4a requires: guessing at an unseen string is the one thing the member
# forbids.
_DIVE_MODES = {
    "air": DiveMode.OPEN_CIRCUIT,
    "nitrox": DiveMode.OPEN_CIRCUIT,
    "mixed": DiveMode.OPEN_CIRCUIT,
}

# `Header.Diving.Algorithm` onto the model's family, matched case-insensitively. Two strings,
# each sourced separately: `Suunto Fused2 RGBM` is what 16 real exports carry and
# `Suunto Fused RGBM 2` is what the spec corpus's D5 fixture does. Both are RGBM.
#
# A string outside the table still fills `name` and leaves `algorithm` absent: a family is a
# claim about the mathematics, not a guess off a product string.
_ALGORITHMS = {
    "suunto fused2 rgbm": DecoAlgorithm.RGBM,
    "suunto fused rgbm 2": DecoAlgorithm.RGBM,
}


def _sample_events(sample: dict[str, Any], elapsed: float) -> list[ParsedProfileEvent]:
    """The markers one sample object carries, if any.

    Three families are read and the rest are dropped, which is a decision about noise
    rather than about trust. `GasSwitch` is the dive's gas history. `Notify` is the device
    prompting the diver, and four of its twelve values name an occurrence the vocabulary has
    a word for. `Alarm`/`Warning` are the things that went wrong, each classified by its own
    wording. **Everything under `State` is dropped**: it is the computer narrating
    its own mode - "Below Surface", "Wet Outside", "Surface Calculation", "Dive Active",
    "Tank pressure available" - which is not an event on a dive, and five of them land on
    t=0 of every single dive in the corpus. The 2026 Ocean's `DiveState`, `DiveStatus`,
    `Lap`, `Pause` and `ArrayBegin` go the same way and for the same reason.

    Only the `Active: true` edge is emitted. These arrive in pairs - `Deep Stop` true at
    1 424 s and false at 1 454 s is one 30-second stop - and a chart tick has no way to show
    which half of a pair it is, so the tick marks the start and the pair's other half would
    only double it. A `GasSwitch` has no `Active` and is not a pair.
    """
    events: list[ParsedProfileEvent] = []
    for key in _EVENT_KEYS:
        raw = sample.get(key)
        if raw is None:
            continue
        for entry in raw if isinstance(raw, list) else [raw]:
            if not isinstance(entry, dict):
                continue
            for name, payload in entry.items():
                event = _event(name, payload, elapsed)
                if event is not None:
                    events.append(event)
    return events


def _event(name: str, payload: Any, elapsed: float) -> ParsedProfileEvent | None:
    """One `{name: payload}` pair as an event, or `None` for the ones not worth a marker."""
    if name == "GasSwitch" and isinstance(payload, dict):
        number = payload.get("GasNumber")
        return ParsedProfileEvent(
            t=elapsed,
            type=ProfileEventType.GAS_SWITCH,
            # The file's own number, the same one `_mixtures_from_cylinders` builds
            # cylinders from and `_parse_samples` labels the pressure channels with - so a
            # switch marker joins to the cylinder it switched to. An Ocean numbers from 0.
            gas_number=int(number) if number is not None else None,
            label=None,
        )

    if not isinstance(payload, dict) or payload.get("Active") is not True:
        return None
    reported = payload.get("Type")
    if not isinstance(reported, str) or not reported.strip():
        return None

    if name == "Notify":
        stop = _STOP_TYPE_BY_NOTIFY.get(reported.strip().lower())
        return None if stop is None else ParsedProfileEvent(t=elapsed, type=stop, gas_number=None, label=None)
    if name in _ALERT_NAMES:
        # The type this wording names, and the wording itself beside it. **Both**, not one or
        # the other: the type is what a chart draws a glyph from and the label is what the
        # diver was actually shown, and "Ceiling Broken" says something no type can. An
        # alert this table has no value for stays `OTHER` with its wording, which is the
        # same marker one class less specific rather than a marker lost.
        label = reported.strip()
        return ParsedProfileEvent(
            t=elapsed,
            type=_ALERT_TYPES.get(label.lower(), ProfileEventType.OTHER),
            gas_number=None,
            label=label,
        )
    return None


def _parse_mixture(gas: dict[str, Any], gas_number: int) -> DiveMixtureSchema:
    """Map one `Gases[]` entry onto a `DiveMixture`, converting SI units as it goes.

    A key the entry omits stays `None` - an untransmitted backup cylinder really has no
    start pressure, and an export that never recorded a gas fraction has not recorded a
    0 % one. See `DiveMixtureSchema`.
    """
    return DiveMixtureSchema(
        end_pressure=_round2_or_none(_pascals_to_bar(gas.get("EndPressure"))),
        # Position in `Gases[]`, counted from 1. This block carries no `GasNumber` of
        # its own, unlike the sample-reconstructed path below, and 1 is what the same D5
        # dive's `Cylinders[].GasNumber` reports for its single cylinder - so the two
        # agree where both exist. A *Suunto Ocean* numbers from 0, but an Ocean export
        # has no `Gases` block at all and never reaches here.
        gas_number=gas_number,
        helium=_round2_or_none(_fraction_to_percent(gas.get("Helium"))),
        oxygen=_round2_or_none(_fraction_to_percent(gas.get("Oxygen"))),
        # Pascal here, unlike the XML export's plain bar: 140000 is 1.4 bar. Same
        # conversion as the cylinder pressures beside it.
        po2_limit=_round2_or_none(_pascals_to_bar(gas.get("PO2"))),
        role=_role(gas.get("State")),
        start_pressure=_round2_or_none(_pascals_to_bar(gas.get("StartPressure"))),
        volume=_cubic_meters_to_liters(gas.get("TankSize")),
    )


def _device(device_log: dict[str, Any], header: dict[str, Any], diving: dict[str, Any]) -> ParsedDevice:
    """The computer this export came off, out of `Header.Device` and `Header.Diving`.

    **`Header.Device` first, the top-level `DeviceLog.Device` behind it**, and where both
    exist they are the same object: the 2026 Ocean export in the corpus carries an
    identical block in each, and the five older fixtures carry only the header's. Reading
    the header's first keeps the device beside the dive it describes rather than beside the
    file.

    **`Name` is the device's name, not its model.** An Ocean whose owner called it `Porvoo`
    answers `Porvoo` here; a D5 nobody renamed answers `Suunto D5`. The format records no
    model of its own - `Info.HW` is a board revision (`Seal_RevA3`) - so `model` stays null
    rather than being back-derived from a name a diver may have chosen. `Info.SW` is the
    firmware (`2.51.28`), and `Info.BSL` beside it is the bootloader, which is not.

    `NumberInSeries` sits under `Header.Diving`, so it is absent from every export that has
    no `Diving` block - the whole 2026 Ocean shape, including the one file in the corpus
    that does carry a serial. Absent is what that means; the counter is not reconstructed
    from anything else.
    """
    device = header.get("Device")
    if not isinstance(device, dict):
        device = device_log.get("Device")
    if not isinstance(device, dict):
        device = {}
    info = device.get("Info")
    if not isinstance(info, dict):
        info = {}
    return ParsedDevice(
        brand=_BRAND,
        serial=device.get("SerialNumber"),
        firmware=info.get("SW"),
        name=device.get("Name"),
        dive_number=diving.get("NumberInSeries"),
    )


def _mode(diving: dict[str, Any]) -> DiveMode | None:
    """The mode this computer ran in, from `Header.Diving.DiveMode`.

    **Only the D5 header shape states it.** The 2026 Ocean shape has no `Header.Diving` at
    all, so an Ocean file yields the channels and no mode - which is correct rather than a
    gap, and is why this takes the block rather than reaching for the key itself.
    """
    reported = diving.get("DiveMode")
    return _DIVE_MODES.get(reported.strip().lower()) if isinstance(reported, str) else None


def _deco_model(diving: dict[str, Any]) -> ParsedDecoModel:
    """The model this computer ran, from `Header.Diving`.

    **`Algorithm` fills two members and they are not derived from each other.** `name` takes
    the string verbatim, because the member is the device's own name for its model and
    vendors name and version theirs as they please; `algorithm` comes from a table of the two
    strings files in hand carry, because a family is a claim about the mathematics. A string
    outside the table therefore fills `name` and leaves `algorithm` absent rather than being
    read for the word "RGBM" in it.

    **`Conservatism` is the one reading in this parser with no floor.** §6.4c puts none on the
    member, so a `0` is the P0 setting and a negative is Suunto's P-1 or P-2 - two of the
    three values the owner's exports carry. Reading a negative here as an absent-marker, which
    is the right answer for every channel above, would delete a real setting.

    Returns an object rather than `None` for an empty header: `ParsedDiveSchema`'s
    `_drop_empty_deco_model` is what turns five nulls back into no model, in one place, so a
    fourth parser inherits it.

    The tissue blocks beside these are deliberately unread: a compartment loading means
    something only inside the algorithm that computed it, and §6.4c has no member for one.
    """
    reported = diving.get("Algorithm")
    name = reported.strip() if isinstance(reported, str) else None
    return ParsedDecoModel(
        algorithm=_ALGORITHMS.get(name.lower()) if name else None,
        name=name,
        conservatism=diving.get("Conservatism"),
    )


def _dive_window_end(header: dict[str, Any]) -> datetime | None:
    """When the dive itself ended, as opposed to when the device stopped logging.

    `DiveTime` is the in-water time and `Duration` the whole logged period, and they are
    not close: one dive in the corpus records `DiveTime` 3 888 s against `Duration`
    4 231 s. That gap is the boat, and it is where the diver breaks down their kit.

    `None` when the header has no `DiveTime`, which leaves the readings unbounded -
    weaker, but never worse than not knowing. Deliberately does *not* fall back to
    `Duration`: bounding a window by its own full length isn't a bound.
    """
    start_text = header.get("DateTime")
    dive_time = header.get("DiveTime")
    if not start_text or dive_time is None:
        return None
    return datetime.fromisoformat(start_text) + timedelta(seconds=float(dive_time))


def _positions(samples: list[dict[str, Any]]) -> EntryExit:
    """The entry and exit fixes this export's samples carry, if it carries GPS at all.

    Only the 2026 Ocean shape does - the D5 exports in the corpus have no `Latitude` key
    anywhere - and it writes fixes on samples of their own, carrying `GPSAltitude` and
    nothing else. So this walks the stream for two channels that have no sample in common
    with each other: the fixes, and the depth readings `entry_and_exit` pivots on.

    **`DiveRouteOrigin` is read as a third channel, and it is where the entry position
    comes from.** Every fix in the sample stream lands after the diver surfaced (see
    `positions.py`), so on the strength of those alone this shape yields an exit and no
    entry - which is what it used to do, on all 19 GPS-carrying files, while the Suunto
    app drew both pins from the same export. The missing half is a lone block on the first
    sample, timestamped identically to `Header.DateTime`, alongside a `DiveRouteQuality`
    and a `QualityFeatures` array this deliberately ignores: the flag reads 1 on good
    origins and on `0, 0` ones alike across the corpus, so treating it as a validity
    signal would drop real positions and keep junk ones. `geo_fix` already rejects the two
    Null Island origins on the evidence of the coordinates themselves.

    It is fed in as an ordinary fix rather than assigned to the entry directly, so that a
    file which does log a pre-descent fix still gets the rule the rest of the module
    promises - the *last* position before the descent wins, whichever channel it arrived
    on. No file in the corpus exercises that yet.

    Timestamps as POSIX seconds off each sample's own `TimeISO8601`, rather than as an
    elapsed offset from `Header.DateTime` the way `_parse_samples` measures its axis.
    Nothing here needs an origin, and asking for one would reintroduce a fixed bug: an
    export mixing a naive header timestamp with offset-aware sample timestamps made
    subtracting the two a `TypeError` that failed the whole import.

    Best-effort **per sample**, like `_scan_samples` below and unlike
    `_mixtures_from_cylinders`: a sample that cannot be read is skipped and the pass
    carries on. Guarding the whole loop instead was the first attempt, and it met the
    stated goal - a dive whose header parses perfectly must not fail over one bad GPS
    sample - at the coarsest possible granularity, discarding every fix already collected
    along with the one that raised. A GPS-carrying export in this corpus yields exactly
    one usable position, so that is the whole feature lost to one bad neighbour.

    **This is a separate pass over the samples, and `_scan_samples` merged its two
    collections to avoid exactly that** - so the divergence is deliberate rather than an
    oversight of the note 45 lines down. Two things stop it folding in. The failure
    guards have to stay independent: a cylinder reconstruction that dies must not cost
    the positions, and vice versa, which one shared pass under one `try` cannot promise.
    And `_scan_samples` runs *conditionally*, only for an export with no `Gases` block,
    while this runs for every file - so folding them would mean running the cylinder scan
    on the D5 exports that skip it today, buying back a pass on the Ocean shape by adding
    one everywhere else. What it costs as it stands is one more `fromisoformat` per
    sample on a parse measured at 2-11 ms.
    """
    # An exact early-out, not a heuristic: with neither key anywhere, `geo_fix` can never
    # build a fix and the answer is `NO_POSITIONS` whatever the depths say. Worth the
    # extra scan because the D5 shape - which has no GPS at all - would otherwise pay
    # ~8 300 `fromisoformat` calls per file to build a `depths` list nothing then reads,
    # on `POST /dive/parse`, on attach, and once per stored file in `backfill_tech_fields`.
    # **Both keys, or the early-out silently un-does the origin**: they are independent in
    # principle, and a file carrying an origin but no sample fixes would bail out here
    # having never looked at it.
    if not any(
        isinstance(sample, dict) and ("Latitude" in sample or "DiveRouteOrigin" in sample) for sample in samples
    ):
        return NO_POSITIONS

    fixes: list[GeoFix] = []
    depths: list[tuple[float, float]] = []
    unreadable = 0
    for sample in samples:
        try:
            time_text = sample.get("TimeISO8601")
            if not time_text:
                continue
            at = datetime.fromisoformat(time_text).timestamp()

            depth = sample.get("Depth")
            if isinstance(depth, (int, float)) and not isinstance(depth, bool):
                depths.append((at, float(depth)))

            fix = geo_fix(
                at,
                degrees_from_radians(sample.get("Latitude")),
                degrees_from_radians(sample.get("Longitude")),
            )
            # Degrees here against radians two lines up - the same file, in two units.
            # See `degrees_verbatim`.
            origin = sample.get("DiveRouteOrigin")
            origin_fix = (
                geo_fix(
                    at,
                    degrees_verbatim(origin.get("Latitude")),
                    degrees_verbatim(origin.get("Longitude")),
                )
                if isinstance(origin, dict)
                else None
            )
        except EXTRACTION_ERRORS:
            # Counted rather than logged per sample: a file whose timestamps are all
            # unreadable would otherwise write one warning per sample, thousands of them,
            # for a single fact about the file.
            unreadable += 1
            continue
        # Both, rather than one or the other: no file in the corpus writes an origin and a
        # fix onto one sample, but nothing in the format forbids it and dropping either
        # would be this function choosing between two positions instead of `entry_and_exit`.
        # **The order is the tie-break**, and it is load-bearing rather than incidental:
        # two positions off one sample share a timestamp, and `entry_and_exit` separates
        # equal timestamps by their order here, so the sample fix wins and the origin is
        # the fallback. Reordering this tuple would change that silently - hence
        # `test_a_sample_fix_outranks_an_origin_on_the_same_sample`.
        fixes.extend(candidate for candidate in (fix, origin_fix) if candidate is not None)

    if unreadable:
        logger.warning("Skipped %d unreadable sample(s) while reading GPS fixes from a Suunto JSON export", unreadable)

    return entry_and_exit(fixes, depths)


def _scan_samples(
    samples: list[dict[str, Any]], dive_end: datetime | None
) -> tuple[dict[int, tuple[float, float]], list[int]]:
    """Everything a reconstructed cylinder needs, in one pass over the samples.

    Returns the first and last transmitter reading per cylinder, and the gas numbers the
    diver switched to in the order they were first used. Both come off the same sample
    object, so they are collected together: walking the array twice meant two passes over
    ~8 300 samples and, more expensively, parsing every `TimeISO8601` twice.

    **Gas switches are what make the cylinder list correct**, not the telemetry.
    `DiveEvents.GasSwitch.GasNumber` is the only record this export keeps of *which*
    cylinders were on the dive, and it is keyed by the same gas number as `Cylinders[]` -
    which is what makes a transmitter reading attributable to a specific cylinder rather
    than to "whichever tank this was". Switch order is chronological, so the back gas
    comes first and deco gases follow - which is the order the form labels its rows by
    position in ("Tank 1", "Tank 2"), so the two agree without either naming the other.

    Pressures are ordered by the sample's own timestamp rather than by position in the
    array: the *union* of an Ocean export's sample timestamps is not monotonic (adjacent
    entries go backwards by up to 0.7 s, because the separate sensor streams are appended
    out of order), so "the last entry in the file" is not reliably the last reading of the
    dive.

    **Readings after the dive ended are dropped**, which matters far more than it sounds.
    The transmitter keeps reporting while the computer is still logging on the surface,
    so the last reading in the file is whatever the tank read once the diver purged the
    regulator to break down their kit. Two dives in the corpus end that way, and taking
    the final reading gave them an end pressure of **0.14 bar** instead of 53 and 76 -
    which `compute_gas_use` would have turned into a diver breathing their whole cylinder
    dry, and an RMV to match. Bounding on `DiveTime` moves every other dive by under
    2 bar, the surface-breathing before derigging.

    A cylinder is only included once it has a real reading. An Ocean reports five slots
    on every sample with `Pressure: null` in the ones nothing is paired to, and its final
    samples null out even the live slot - so `None` readings are skipped rather than
    ending the series.
    """
    extremes: dict[int, tuple[tuple[datetime, float], tuple[datetime, float]]] = {}
    order: list[int] = []
    # A set alongside the list purely for the membership test. `not in order` on a
    # growing list runs once per sample, so a file with many distinct `GasNumber`s made
    # this quadratic - 8 000 of them took 0.26 s against 0.02 s for 2 000, and the curve
    # keeps going. The list is still what carries the order.
    seen: set[int] = set()
    for sample in samples:
        _collect_gas_switch(sample, order, seen)

        time_text = sample.get("TimeISO8601")
        if not time_text:
            continue
        moment = datetime.fromisoformat(time_text)
        # Compared only when both sides agree on tz-awareness. A header that writes a
        # naive `DateTime` alongside offset-aware sample timestamps is malformed, but it
        # is malformed in a way that has nothing to do with cylinders, and `>` between a
        # naive and an aware datetime raises - which used to fail the whole import here,
        # before a single cylinder had been looked at.
        if dive_end is not None and (moment.tzinfo is None) == (dive_end.tzinfo is None) and moment > dive_end:
            continue
        for cylinder in sample.get("Cylinders") or []:
            pressure = cylinder.get("Pressure")
            gas_number = cylinder.get("GasNumber")
            # A reading with no cylinder to attach it to is dropped, not guessed at -
            # every real Ocean sample numbers all five slots, so this is a malformed file
            # rather than a shape worth supporting.
            if pressure is None or gas_number is None:
                continue
            # A running earliest/latest rather than every reading kept and sorted at the
            # end: only two of the ~350 readings per cylinder are ever used, and this is
            # already the hot loop over an 8 000-sample export.
            reading = (moment, float(pressure))
            first, last = extremes.get(int(gas_number), (reading, reading))
            extremes[int(gas_number)] = (
                reading if reading[0] < first[0] else first,
                reading if reading[0] >= last[0] else last,
            )

    return {number: (first[1], last[1]) for number, (first, last) in extremes.items()}, order


def _collect_gas_switch(sample: dict[str, Any], order: list[int], seen: set[int]) -> None:
    """Append any gas number this sample switched to, if it is not already known.

    `seen` is the membership test and `order` the result - see `_scan_samples` for why
    they are separate.
    """
    events = sample.get("DiveEvents")
    for event in events if isinstance(events, list) else [events]:
        if not isinstance(event, dict):
            continue
        switch = event.get("GasSwitch")
        if not isinstance(switch, dict):
            continue
        number = switch.get("GasNumber")
        if number is None or int(number) in seen:
            continue
        seen.add(int(number))
        order.append(int(number))


def _mixtures_from_cylinders(samples: list[dict[str, Any]], dive_end: datetime | None) -> list[DiveMixtureSchema]:
    """Reconstruct the dive's cylinders from gas-switch events and transmitter telemetry.

    The 2026 Suunto Ocean's JSON export is a third header shape: it has no
    `Header.Diving` block at all, so the `Gases[]` path finds nothing and every dive
    imported from one came back with no mixtures - even though the file records both
    which cylinders were breathed and several hundred pressure readings, just not where
    the other exports keep them.

    **A cylinder is listed because the diver switched to it, not because it transmitted.**
    That distinction is what makes this safe on a multi-gas dive. Building the list from
    telemetry alone would emit exactly one mixture for a two-tank dive - and a lone
    mixture carrying both pressures is precisely the shape `compute_gas_use` derives an
    RMV from, so a stage bottle's pressure drop would have been silently attributed to
    the whole dive. Reading `GasSwitch` instead means a two-gas dive produces two
    cylinders, each pressure lands on the numbered cylinder it actually belongs to, and
    the RMV correctly declines to compute because there is more than one tank.

    Only the pressures are real, and nothing else is invented to fill the gap. This
    export records no gas fraction and no tank size anywhere - verified across the whole
    2026 corpus, where the string `Oxygen` does not appear in a single file - so
    `oxygen`/`helium`/`volume` come back `None` and the dive form applies its own
    `DEFAULT_MIXTURE` to them, exactly as it would for a cylinder the diver added by
    hand. Reporting air here would have been indistinguishable from having read air.
    """
    pressures, breathed = _scan_samples(samples, dive_end)
    # Switch order first, then any cylinder that transmitted without a recorded switch:
    # evidence of a tank is evidence of a tank, whichever way round it arrived.
    # Capped for the same reason the FIT parser caps its cylinders: every entry becomes a
    # `DiveMixtureSchema` in the `/dive/parse` response, and nothing else bounds how many
    # distinct `GasNumber`s a file may claim. No Suunto pairs more than five.
    switched = set(breathed)
    numbers = (breathed + [number for number in sorted(pressures) if number not in switched])[:_MAX_CYLINDERS]

    return [
        DiveMixtureSchema(
            end_pressure=_round2_or_none(_pascals_to_bar(pressures[number][1])) if number in pressures else None,
            # The file's own number, not a position - this is the one export shape that
            # states it, in the `GasSwitch`/`Cylinders[]` entries these cylinders were
            # reconstructed from, and it is the same number `_parse_samples` labels the
            # pressure channels with. Keeping it means a mixture row and its curve on the
            # chart refer to the same cylinder by the same name.
            gas_number=number,
            helium=None,
            oxygen=None,
            # This shape records neither, for the same reason it records no gas fraction:
            # there is no `Gases` block anywhere in it.
            po2_limit=None,
            role=None,
            start_pressure=_round2_or_none(_pascals_to_bar(pressures[number][0])) if number in pressures else None,
            volume=None,
        )
        for number in numbers
    ]


class SuuntoJsonParser(DiveParser):
    """Parses Suunto app / Suunto Ocean JSON dive-log exports (`DeviceLog.Header`).

    Extracts the fields with a direct equivalent on the `Dive`/`DiveMixture` backend models
    (`models/dive.py`, `models/dive_mixture.py`), the recording's own mode and deco model out
    of `Header.Diving` (`_mode`, `_deco_model`), plus - separately, via `parse_profile` - the
    per-sample curves and the sample stream's events stored as `DiveProfile`, and - from the
    same sample stream, out of both its satellite fixes and its `DiveRouteOrigin` - the
    entry/exit positions described in `positions.py`.

    **Seven of the ten channels**: depth, deco ceiling, temperature and tank pressure, plus
    the no-decompression clock, the time to surface and both gradient factors. The two this
    format does not carry are `ppo2` and `cns` - no sample object holds a computed partial
    pressure or a running CNS clock anywhere, and the dive-level `StartTissue.CNS` /
    `EndTissue.CNS` that are read are two scalars rather than a curve.

    What is still refused: the per-compartment tissue loading (a loading means something only
    inside the algorithm that computed it), `gfLeadingTissue` (the compartment's *number*,
    not a loading), `DeviceInternalAbsPressure` (the device's own ambient sensor, not a tank
    pressure - see `_parse_samples`), the rest of the GPS track and the battery telemetry.
    `Header.Device` has no column behind it either and is read all the same, as the
    `ParsedDevice` `_device` builds: two exports of one dive can only be told apart by what
    recorded them. Gas mixtures come from `DeviceLog.Header.Diving.Gases` (present in Suunto
    D5-style exports; absent from "clean"/header-only exports, which don't have gas data at
    all).
    """

    key = "suunto_json"
    content_type = "application/json"

    @classmethod
    def can_parse(cls, filename: str, content: bytes) -> bool:
        """Whether this looks like a Suunto JSON export: a `.json` file whose top level is
        an object carrying `DeviceLog.Header`.

        Sniffs rather than trusts the extension, and answers False for anything malformed
        instead of raising - the caller is choosing between parsers, not parsing yet.
        """
        if not filename.lower().endswith(".json"):
            return False
        try:
            data: Any = json.loads(content)
        except json.JSONDecodeError:
            return False
        if not isinstance(data, dict):
            return False
        device_log = data.get("DeviceLog")
        if not isinstance(device_log, dict):
            return False
        return isinstance(device_log.get("Header"), dict)

    @classmethod
    def parse(cls, content: bytes) -> ParsedDiveSchema:
        """Extract the dive itself (not its samples - see `parse_profile`).

        Every structural surprise in the file becomes a `DiveParseError`, so a caller never
        sees a raw `KeyError` from a Suunto export that omits a field this expects.
        """
        try:
            data: Any = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DiveParseError(f"Invalid JSON: {exc}") from exc

        try:
            return cls._parse_dive(data)
        except EXTRACTION_ERRORS as exc:
            raise DiveParseError(f"Malformed Suunto JSON dive data: {exc}") from exc

    @classmethod
    def parse_profile(cls, content: bytes) -> ParsedProfileSchema | None:
        """Extract `DeviceLog.Samples[]` as per-channel series."""
        try:
            data: Any = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DiveParseError(f"Invalid JSON: {exc}") from exc

        try:
            return cls._parse_samples(data)
        except EXTRACTION_ERRORS as exc:
            raise DiveParseError(f"Malformed Suunto JSON dive samples: {exc}") from exc

    @staticmethod
    def _parse_samples(data: dict[str, Any]) -> ParsedProfileSchema | None:
        """Group a sample array into one series per channel.

        Channels in these exports are **independently sampled**: a Suunto Ocean dive has
        8 292 sample objects, of which 395 carry `Depth`/`Ceiling`/`Cylinders`, 3 933
        carry `Temperature` at 1 Hz, and the rest carry only events, GPS or battery
        telemetry. Hence one series per channel rather than a shared axis full of nulls -
        which on that dive would be ~90 % null in the depth column.

        A consequence of the same fact: the *union* of sample timestamps is not
        monotonic (adjacent entries go backwards by up to 0.7 s, because the separate
        sensor streams are appended out of order), so each channel is sorted on its own
        timestamps at the end. That is not a parse error and must never be treated as one.
        """
        device_log = data["DeviceLog"]
        samples = device_log.get("Samples") or []
        origin_text = (device_log.get("Header") or {}).get("DateTime")
        if not samples or not origin_text:
            return None

        origin = datetime.fromisoformat(origin_text)

        depth: list[tuple[float, int]] = []
        ceiling: list[tuple[float, int]] = []
        temperature: list[tuple[float, int]] = []
        ndl: list[tuple[float, int]] = []
        tts: list[tuple[float, int]] = []
        gradient_factor: list[tuple[float, int]] = []
        surface_gradient_factor: list[tuple[float, int]] = []
        # Insertion-ordered, so the cylinders come out in the order the device listed
        # them rather than sorted by a number that is only a label.
        pressure: dict[int, list[tuple[float, int]]] = {}
        events: list[ParsedProfileEvent] = []

        for sample in samples:
            time_text = sample.get("TimeISO8601")
            if not time_text:
                continue
            elapsed = (datetime.fromisoformat(time_text) - origin).total_seconds()

            events.extend(_sample_events(sample, elapsed))

            depth_cm = scaled_int_or_none(sample.get("Depth"), CENTIMETERS_PER_METER)
            if depth_cm is not None:
                depth.append((elapsed, depth_cm))

            # Unlike depth, a zero here is the *absence* of a reading: this export writes
            # `"Ceiling": 0` on every no-deco sample where the DM5 XML of the same dive
            # writes `xsi:nil`. See `ceiling_cm`, which is where the two are reconciled.
            ceiling_value = ceiling_cm(sample.get("Ceiling"))
            if ceiling_value is not None:
                ceiling.append((elapsed, ceiling_value))

            temperature_c10 = _celsius_tenths(sample.get("Temperature"))
            if temperature_c10 is not None:
                temperature.append((elapsed, temperature_c10))

            # Already whole seconds, which is the scale the channel is stored in. **A zero
            # is a reading**: it is what a computer shows the moment a dive stops being a
            # no-decompression dive, and a D5 export in hand writes it on five consecutive
            # samples at 42.6 to 44.5 m with a time to surface of 256 to 268 s beside it.
            # The `-1` this export writes on 1 031 samples across the 19 Ocean files - 916
            # of them with a ceiling above zero, the device showing a stop depth in place of
            # a clock it no longer has - is the absent-marker, and `unsigned_int_or_none`
            # drops it. A `6000` is the Ocean's display cap and carried as the reading it is.
            ndl_seconds = unsigned_int_or_none(sample.get("NoDecTime"), UNSCALED)
            if ndl_seconds is not None:
                ndl.append((elapsed, ndl_seconds))

            # **A `TimeToSurface` of zero is this format's absent-marker**, which only the
            # file could settle and which is why it is decided here rather than in the shared
            # helper. The Ocean writes it on 199 of the 364 samples of one dive that carry
            # the member, at every depth from 0 to 19 m - including two rows from a sample at
            # 14.63 m that says `88`. A time to surface that is zero at 14 m and 88 s at 14 m
            # seconds later is not a time; it is the space the device writes when it has no
            # figure. The D5 shapes write exactly one per file, always on the first sample,
            # where a real ascent from 1.24 m would take the 8 to 12 s the next sample states.
            tts_seconds = unsigned_int_or_none(sample.get("TimeToSurface"), UNSCALED)
            if tts_seconds is not None and tts_seconds > 0:
                tts.append((elapsed, tts_seconds))

            factors = sample.get("RtGradientFactors")
            if isinstance(factors, dict):
                # `-100` is the absent-marker, written on 5 531 of 7 194 samples - where no
                # compartment leads - while `0` is a reading, a leading tissue at ambient, on
                # 585. Nothing is capped above: `gf99` reaches four figures on a real
                # decompression ascent, Suunto publishes no definition of the field, and
                # nothing in the file accounts for the size, so the number goes in as written.
                gf99 = unsigned_int_or_none(factors.get("gf99"), UNSCALED)
                if gf99 is not None:
                    gradient_factor.append((elapsed, gf99))

                # **Two spellings of one field, and both are real.** The Ocean's 2.40.56
                # firmware writes `gtSurface` on all 7 194 samples of the 19 files in hand
                # and its 2.51.28 firmware writes `gfSurface`; the `gt` is a vendor typo
                # fixed in an update. A reader that knew one would lose the channel on
                # whichever half of the installed base wrote the other.
                #
                # `gfLeadingTissue` beside them is the compartment's *number* rather than a
                # loading, and is not read.
                surface = factors.get("gfSurface", factors.get("gtSurface"))
                gf_surface = unsigned_int_or_none(surface, UNSCALED)
                if gf_surface is not None:
                    surface_gradient_factor.append((elapsed, gf_surface))

            # `Cylinders[].Pressure` is the transmitter. `DeviceInternalAbsPressure`,
            # which sits right next to it in the same sample object, is the *device's own
            # ambient pressure sensor* - roughly 96 400 Pa at the surface. It is
            # deliberately not read here: labelling it "tank pressure" on a chart divers
            # plan gas from would be actively wrong, and it is close enough in shape to a
            # pressure reading to be picked up by mistake by the next person.
            for cylinder in sample.get("Cylinders") or []:
                cylinder_bar10 = scaled_int_or_none(cylinder.get("Pressure"), _TENTH_BAR_PER_PASCAL)
                if cylinder_bar10 is None:
                    continue
                # A Suunto Ocean reports five cylinder slots on every sample with only
                # one populated, so a slot is only a series once it has a real reading.
                gas_number = int(cylinder["GasNumber"])
                # Capped like the mixtures, and for the parallel reason: each cylinder
                # becomes a pressure channel in the stored profile, and `downsample` caps
                # points *within* a channel rather than how many there are.
                if gas_number not in pressure and len(pressure) >= _MAX_CYLINDERS:
                    continue
                pressure.setdefault(gas_number, []).append((elapsed, cylinder_bar10))

        if not any((depth, ceiling, temperature, ndl, tts, gradient_factor, surface_gradient_factor, pressure)):
            return None

        return ParsedProfileSchema(
            depth=series(depth),
            ceiling=series(ceiling),
            temperature=series(temperature),
            ndl=series(ndl),
            tts=series(tts),
            # **No `ppo2` and no `cns` channel from this format**, which is the export's
            # shape rather than a refusal: a sample object carries no computed partial
            # pressure and no running CNS clock anywhere. The dive-level `StartTissue.CNS`
            # and `EndTissue.CNS` are read, and they are two scalars rather than a curve.
            gradient_factor=series(gradient_factor),
            surface_gradient_factor=series(surface_gradient_factor),
            pressure=[
                ParsedPressureSeries(gas_number=gas_number, t=channel.t, v=channel.v)
                for gas_number, channel in ((number, series(points)) for number, points in pressure.items())
                if channel is not None
            ],
            events=events,
        )

    @staticmethod
    def _parse_dive(data: dict[str, Any]) -> ParsedDiveSchema:
        """Map `DeviceLog.Header` onto `ParsedDiveSchema`.

        Field names vary across export generations, so several are read with a fallback
        (`DiveTime`/`Duration`, `DepthAverage`/`Depth.Avg`). Bottom temperature is derived
        rather than read: the JSON header has no per-phase breakdown the way the XML export
        does, so the colder of the recorded extremes stands in, falling back to the coldest
        sample when the header carries neither.
        """
        header = data["DeviceLog"]["Header"]
        samples = data["DeviceLog"].get("Samples") or []
        depth = header.get("Depth") or {}
        temperature = header.get("Temperature") or {}
        diving = header.get("Diving") or {}
        # `Gases` is the authoritative list where the export has one - it carries the gas
        # fractions and tank size that telemetry alone can't. Only when it is absent
        # entirely (the 2026 Ocean shape, which has no `Diving` block at all) are the
        # cylinders reconstructed from the sample stream.
        mixtures = [
            _parse_mixture(gas, gas_number) for gas_number, gas in enumerate(diving.get("Gases") or [], start=1)
        ]
        if not mixtures:
            # Best-effort enrichment, so it degrades to "no mixtures" rather than taking
            # the import down with it. Unlike the header fields above, this walks the
            # whole sample stream of an export shape that is barely documented, and it
            # runs for *every* file with no `Gases` block - including ones that have no
            # cylinder data at all and never did. Before this guard, an export mixing a
            # naive `Header.DateTime` with offset-aware sample timestamps turned a
            # previously fine import into a 422, and it failed in `_cylinder_pressures`
            # before any cylinder was even inspected.
            #
            # `EXTRACTION_ERRORS` rather than a tuple spelled out here, because a narrower
            # one had already let this promise lapse: the guarded code runs `Decimal`
            # arithmetic and `timedelta(seconds=...)`, and `json.loads` accepts bare
            # `Infinity` and overflows large exponents to `inf`, so a cylinder pressure of
            # `Infinity` raised `decimal.InvalidOperation` straight past it and took the
            # header fields down with samples they had nothing to do with.
            try:
                mixtures = _mixtures_from_cylinders(samples, _dive_window_end(header))
            except EXTRACTION_ERRORS:
                logger.warning("Could not reconstruct cylinders from Suunto JSON samples", exc_info=True)
                mixtures = []

        temperatures_celsius = [
            celsius
            for celsius in (
                _kelvin_to_celsius(temperature.get("Max")),
                _kelvin_to_celsius(temperature.get("Min")),
            )
            if celsius is not None
        ]
        if not temperatures_celsius:
            temperatures_celsius = [
                celsius
                for celsius in (_kelvin_to_celsius(sample.get("Temperature")) for sample in samples)
                if celsius is not None
            ]

        start_tissue = diving.get("StartTissue") or {}
        end_tissue = diving.get("EndTissue") or {}
        entry, exit_fix = _positions(samples)

        return ParsedDiveSchema(
            avg_depth=header.get("DepthAverage", depth.get("Avg")),
            # The colder of the two recorded extremes is the best available proxy
            # for temperature at depth, since the JSON header doesn't break
            # temperature down by phase of the dive the way the XML export does.
            bottom_temperature=min(temperatures_celsius) if temperatures_celsius else None,
            # **CNS is a 0-1 fraction here and whole percent in the XML export**, which
            # is invisible until the same dive is read both ways: `EndTissue.CNS: 0.069`
            # against `<CnsEnd>7</CnsEnd>`. Storing it unconverted would report a 69 %
            # oxygen clock as 0.069 %. OTU needs no conversion - it is the same absolute
            # count in both (17.89 against a rounded 18 on that dive).
            #
            # Rounded to 2 places like the gas readings, for the same reason: this export
            # writes OTU as a full float32 (`17.89002799987793`), and no dive computer
            # accounts oxygen exposure to a hundred-billionth of an OTU.
            cns_start=_round2_or_none(_fraction_to_percent(start_tissue.get("CNS"))),
            cns_end=_round2_or_none(_fraction_to_percent(end_tissue.get("CNS"))),
            otu_start=_round2_or_none(start_tissue.get("OTU")),
            otu_end=_round2_or_none(end_tissue.get("OTU")),
            # Pascal, the same integer the XML export writes into its own
            # `<SurfacePressure>`.
            surface_pressure_bar=_pascals_to_bar(diving.get("SurfacePressure")),
            # Radians in this export's sample fixes, degrees in the FIT file of the same
            # dive - and degrees again in its own `DiveRouteOrigin`, which is where the
            # entry comes from since every sample fix lands after the diver surfaced. See
            # `degrees_from_radians` and `degrees_verbatim`.
            entry_latitude=None if entry is None else entry.latitude,
            entry_longitude=None if entry is None else entry.longitude,
            exit_latitude=None if exit_fix is None else exit_fix.latitude,
            exit_longitude=None if exit_fix is None else exit_fix.longitude,
            # Null for the reason `SuuntoXmlParser` spells out at its own `dive_number`:
            # `Header.Diving.NumberInSeries` is the *computer's* counter, not the diver's
            # lifetime number. It is read, and it goes on the device below.
            dive_number=None,
            device=_device(data["DeviceLog"], header, diving),
            mode=_mode(diving),
            deco_model=_deco_model(diving),
            # D5-style exports report this as `Duration` rather than `DiveTime`.
            duration=_round_or_none(header.get("DiveTime", header.get("Duration"))),
            max_depth=depth.get("Max"),
            start_time=header.get("DateTime"),
            mixtures=mixtures,
        )
