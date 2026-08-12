import json
import logging
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ...schemas.dive_profile import ParsedPressureSeries, ParsedProfileSchema
from ...schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from .base import DiveParser
from .channels import CENTIMETERS_PER_METER, TENTHS_PER_UNIT, scaled_int_or_none, series
from .exceptions import EXTRACTION_ERRORS, DiveParseError

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


def _parse_mixture(gas: dict[str, Any]) -> DiveMixtureSchema:
    """Map one `Gases[]` entry onto a `DiveMixture`, converting SI units as it goes.

    A key the entry omits stays `None` - an untransmitted backup cylinder really has no
    start pressure, and an export that never recorded a gas fraction has not recorded a
    0 % one. See `DiveMixtureSchema`.
    """
    return DiveMixtureSchema(
        end_pressure=_round2_or_none(_pascals_to_bar(gas.get("EndPressure"))),
        helium=_round2_or_none(_fraction_to_percent(gas.get("Helium"))),
        # Left for the user to fill in themselves rather than parsed - see
        # DECISIONS.md.
        name=None,
        oxygen=_round2_or_none(_fraction_to_percent(gas.get("Oxygen"))),
        start_pressure=_round2_or_none(_pascals_to_bar(gas.get("StartPressure"))),
        volume=_cubic_meters_to_liters(gas.get("TankSize")),
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
    comes first and deco gases follow, matching how the form names rows
    (`getDefaultMixtureName`).

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
            helium=None,
            name=None,
            oxygen=None,
            start_pressure=_round2_or_none(_pascals_to_bar(pressures[number][0])) if number in pressures else None,
            volume=None,
        )
        for number in numbers
    ]


class SuuntoJsonParser(DiveParser):
    """Parses Suunto app / Suunto Ocean JSON dive-log exports (`DeviceLog.Header`).

    Extracts the fields with a direct equivalent on the `Dive`/`DiveMixture`
    backend models (`models/dive.py`, `models/dive_mixture.py`), plus -
    separately, via `parse_profile` - the per-sample depth/temperature/tank-
    pressure curves stored as `DiveProfile`. The export has plenty of other
    fields (tissue-loading/CNS/OTU stats, algorithm metadata, GPS track,
    battery telemetry) with nowhere to persist them, so they aren't parsed at
    all. Gas mixtures come from
    `DeviceLog.Header.Diving.Gases` (present in Suunto D5-style exports; absent
    from "clean"/header-only exports, which don't have gas data at all).
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
        temperature: list[tuple[float, int]] = []
        # Insertion-ordered, so the cylinders come out in the order the device listed
        # them rather than sorted by a number that is only a label.
        pressure: dict[int, list[tuple[float, int]]] = {}

        for sample in samples:
            time_text = sample.get("TimeISO8601")
            if not time_text:
                continue
            elapsed = (datetime.fromisoformat(time_text) - origin).total_seconds()

            depth_cm = scaled_int_or_none(sample.get("Depth"), CENTIMETERS_PER_METER)
            if depth_cm is not None:
                depth.append((elapsed, depth_cm))

            temperature_c10 = _celsius_tenths(sample.get("Temperature"))
            if temperature_c10 is not None:
                temperature.append((elapsed, temperature_c10))

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

        if not depth and not temperature and not pressure:
            return None

        return ParsedProfileSchema(
            depth=series(depth),
            temperature=series(temperature),
            pressure=[
                ParsedPressureSeries(gas_number=gas_number, t=channel.t, v=channel.v)
                for gas_number, channel in ((number, series(points)) for number, points in pressure.items())
                if channel is not None
            ],
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
        mixtures = [_parse_mixture(gas) for gas in diving.get("Gases") or []]
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

        return ParsedDiveSchema(
            avg_depth=header.get("DepthAverage", depth.get("Avg")),
            # The colder of the two recorded extremes is the best available proxy
            # for temperature at depth, since the JSON header doesn't break
            # temperature down by phase of the dive the way the XML export does.
            bottom_temperature=min(temperatures_celsius) if temperatures_celsius else None,
            dive_number=None,
            # D5-style exports report this as `Duration` rather than `DiveTime`.
            duration=_round_or_none(header.get("DiveTime", header.get("Duration"))),
            max_depth=depth.get("Max"),
            start_time=header.get("DateTime"),
            mixtures=mixtures,
        )
