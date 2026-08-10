import json
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ...schemas.dive_profile import ParsedPressureSeries, ParsedProfileSchema, ParsedSeries
from ...schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from .base import DiveParser
from .exceptions import DiveParseError

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

# The integer scales `parse_profile` emits in - depth in centimeters, temperature in
# tenths of a degree, pressure in tenths of a bar. See `schemas/dive_profile.py`.
_CENTIMETERS_PER_METER = Decimal("100")
_TENTHS_PER_UNIT = Decimal("10")
_TENTH_BAR_PER_PASCAL = Decimal("0.0001")


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


def _scaled_int(value: float | None, factor: Decimal) -> int | None:
    """Scale a reading into the integer units the profile is stored in.

    Decimal for the same reason as everything else in this module: the readings arrive as
    decimal literals from `json.loads`, and multiplying them as binary floats puts values
    on the wrong side of a rounding boundary several thousand times per dive.
    `ROUND_HALF_UP` rather than Python's banker's rounding, so a half is always a half.
    """
    if value is None:
        return None
    return int((Decimal(str(value)) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _celsius_tenths(kelvin: float | None) -> int | None:
    """Kelvin straight to tenths of a degree Celsius, without a float in between."""
    if kelvin is None:
        return None
    tenths = (Decimal(str(kelvin)) - _KELVIN_TO_CELSIUS_OFFSET) * _TENTHS_PER_UNIT
    return int(tenths.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _series(points: list[tuple[float, int]]) -> ParsedSeries | None:
    """Turn `(seconds, value)` pairs into a time-sorted series, or `None` if there are none.

    A stable sort keyed on the timestamp alone, so two readings that landed on the same
    instant keep the order the file listed them in.
    """
    if not points:
        return None
    ordered = sorted(points, key=lambda point: point[0])
    return ParsedSeries(t=[t for t, _ in ordered], v=[v for _, v in ordered])


def _parse_mixture(gas: dict[str, Any]) -> DiveMixtureSchema:
    return DiveMixtureSchema(
        end_pressure=_round2_or_none(_pascals_to_bar(gas.get("EndPressure"))),
        helium=_round2_or_none(_fraction_to_percent(gas.get("Helium"))) or 0.0,
        # Left for the user to fill in themselves rather than parsed - see
        # DECISIONS.md.
        name=None,
        oxygen=_round2_or_none(_fraction_to_percent(gas.get("Oxygen"))) or 0.0,
        start_pressure=_round2_or_none(_pascals_to_bar(gas.get("StartPressure"))),
        volume=_cubic_meters_to_liters(gas.get("TankSize")) or 0.0,
    )


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
        try:
            data: Any = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DiveParseError(f"Invalid JSON: {exc}") from exc

        try:
            return cls._parse_dive(data)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
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
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
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

            depth_cm = _scaled_int(sample.get("Depth"), _CENTIMETERS_PER_METER)
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
                cylinder_bar10 = _scaled_int(cylinder.get("Pressure"), _TENTH_BAR_PER_PASCAL)
                if cylinder_bar10 is None:
                    continue
                # A Suunto Ocean reports five cylinder slots on every sample with only
                # one populated, so a slot is only a series once it has a real reading.
                pressure.setdefault(int(cylinder["GasNumber"]), []).append((elapsed, cylinder_bar10))

        if not depth and not temperature and not pressure:
            return None

        return ParsedProfileSchema(
            depth=_series(depth),
            temperature=_series(temperature),
            pressure=[
                ParsedPressureSeries(gas_number=gas_number, t=series.t, v=series.v)
                for gas_number, series in ((number, _series(points)) for number, points in pressure.items())
                if series is not None
            ],
        )

    @staticmethod
    def _parse_dive(data: dict[str, Any]) -> ParsedDiveSchema:
        header = data["DeviceLog"]["Header"]
        samples = data["DeviceLog"].get("Samples") or []
        depth = header.get("Depth") or {}
        temperature = header.get("Temperature") or {}
        diving = header.get("Diving") or {}
        mixtures = [_parse_mixture(gas) for gas in diving.get("Gases") or []]

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
