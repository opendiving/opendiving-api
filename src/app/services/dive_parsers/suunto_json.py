import json
from decimal import Decimal
from typing import Any

from ...schemas.parsed_dive import ParsedDiveSchema
from .base import DiveParser
from .exceptions import DiveParseError

# Suunto app / Suunto Ocean JSON exports report temperature in Kelvin (SI units),
# unlike the Suunto DM5 XML export which already uses Celsius.
_KELVIN_TO_CELSIUS_OFFSET = Decimal("273.15")


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


def _round_or_none(value: float | int | None) -> int | None:
    return round(value) if value is not None else None


class SuuntoJsonParser(DiveParser):
    """Parses Suunto app / Suunto Ocean JSON dive-log exports (`DeviceLog.Header`).

    This only covers the summary fields present in `DeviceLog.Header` - unlike
    `SuuntoXmlParser`, it does not currently parse per-sample depth/temperature
    profiles or gas mixtures, since those aren't present in every JSON export
    (e.g. the "clean"/header-only exports this was built against).
    """

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

    @staticmethod
    def _parse_dive(data: dict[str, Any]) -> ParsedDiveSchema:
        header = data["DeviceLog"]["Header"]
        samples = data["DeviceLog"].get("Samples") or []
        depth = header.get("Depth") or {}
        temperature = header.get("Temperature") or {}
        device = header.get("Device") or {}
        device_info = device.get("Info") or {}

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
            algorithm=None,
            altitude_mode=None,
            ascent_mode=None,
            ascent_time=_round_or_none(header.get("AscentTime")),
            avg_depth=header.get("DepthAverage", depth.get("Avg")),
            battery_level=None,
            # The colder of the two recorded extremes is the best available proxy
            # for temperature at depth, since the JSON header doesn't break
            # temperature down by phase of the dive the way the XML export does.
            bottom_temperature=min(temperatures_celsius) if temperatures_celsius else None,
            bottom_time=None,
            cns_end=None,
            cns_start=None,
            cylinder_volume=None,
            cylinder_work_pressure=None,
            desaturation_time=None,
            dive_number=None,
            diving_days_in_row=None,
            duration=_round_or_none(header.get("DiveTime")),
            end_pressure=None,
            end_temperature=None,
            last_deco_stop_depth=None,
            max_depth=depth.get("Max"),
            mode=None,
            olf_end=None,
            otu_end=None,
            otu_start=None,
            personal_mode=None,
            previous_max_depth=None,
            sample_interval=None,
            serial_number=device.get("SerialNumber"),
            software=device_info.get("SW"),
            source="Suunto",
            start_temperature=None,
            start_time=header.get("DateTime"),
            surface_pressure=None,
            surface_time=None,
            mixtures=[],
            samples=[],
        )
