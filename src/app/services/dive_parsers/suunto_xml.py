import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException

from ...schemas.dive_profile import ParsedPressureSeries, ParsedProfileSchema, ParsedSeries
from ...schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from .base import DiveParser
from .exceptions import DiveParseError, UnsupportedDiveFileError

_SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"
_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_TWO_DECIMAL_PLACES = Decimal("0.01")

# DM5 XML expresses **every** pressure in millibar - cylinder start/end pressures, the
# per-sample transmitter readings, `CylinderWorkPressure` (200000 = 200 bar) and
# `SurfacePressure` (105500 = 1.055 bar) alike. Cross-checked against the same dive
# exported as JSON, where `DiveMixture/StartPressure: 205203` reads `20520312` Pascal:
# both are 205.2 bar. This went unnoticed for a long time because pre-2025 exports have
# no transmitter and write `0`.
_MILLIBAR_PER_BAR = Decimal("1000")
# Depth in meters -> centimeters, temperature in Celsius -> tenths of a degree, and
# sample pressure in millibar -> tenths of a bar. See `schemas/dive_profile.py` for why
# the profile is stored as scaled integers at all.
_CENTIMETERS_PER_METER = Decimal("100")
_TENTHS_PER_UNIT = Decimal("10")
_TENTH_BAR_PER_MILLIBAR = Decimal("0.01")

# The one cylinder a DM5 export's samples can describe. The format reports a single
# `<Pressure>` per sample with no cylinder identity attached, so there is nothing to
# group by - but the series still needs a label, and `1` is what the *same dive* exported
# as JSON reports for it (`Cylinders: [{GasNumber: 1, ...}]` on a D5). Deliberately not
# the mixture's `<TransmitterId>`: that is a device serial (e.g. 2411100050), so using it
# would both read as nonsense in a chart legend and make one dive disagree with itself
# depending on which export it was imported from.
_XML_GAS_NUMBER = 1


def _tag(name: str) -> str:
    return f"{{{_SUUNTO_NS}}}{name}"


def _text(element: ET.Element, tag: str) -> str | None:
    """Return stripped text for a child element, or None if absent/nil."""
    child = element.find(_tag(tag))
    if child is None or child.get(_NIL) == "true":
        return None
    return child.text


def _float(element: ET.Element, tag: str) -> float | None:
    val = _text(element, tag)
    return float(val) if val is not None else None


def _int(element: ET.Element, tag: str) -> int | None:
    val = _text(element, tag)
    return int(val) if val is not None else None


def _decimal_divide(value: float | None, divisor: Decimal) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)) / divisor)


def _millibar_to_bar(value: float | None) -> float | None:
    """Convert a DM5 pressure reading to bar. See `_MILLIBAR_PER_BAR`.

    Via Decimal (constructed from `str(value)`) rather than plain float division, for the
    same reason as `SuuntoJsonParser._pascals_to_bar`: it avoids introducing binary
    floating-point noise the rounding below would then have to hide.
    """
    return _decimal_divide(value, _MILLIBAR_PER_BAR)


def _scaled_int(value: float | None, factor: Decimal) -> int | None:
    """Scale a reading into the integer units the profile is stored in.

    `Decimal(str(value))` rather than `round(value * factor)`: the raw readings arrive as
    decimal literals in the file, and multiplying them as binary floats puts values like
    25.85 on the wrong side of a rounding boundary (`round(25.85 * 10)` is 258, because
    the product is really 258.49999999999997). Several thousand times per dive.
    `ROUND_HALF_UP` rather than Python's banker's rounding, so a half is always a half.
    """
    if value is None:
        return None
    return int((Decimal(str(value)) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _round2_or_none(value: float | None) -> float | None:
    """Round a gas percentage/pressure reading to 2 decimal places - dive-computer
    gauges aren't meaningfully more precise than that. Uses Decimal (constructed
    from `str(value)`) rather than plain float rounding, consistent with how
    `SuuntoJsonParser` avoids binary floating-point noise elsewhere (e.g.
    `_kelvin_to_celsius`, `_pascals_to_bar`)."""
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(_TWO_DECIMAL_PLACES))


class SuuntoXmlParser(DiveParser):
    """Parses Suunto dive-log XML exports (e.g. from the Suunto app / DM5).

    Extracts the fields with a direct equivalent on the `Dive`/`DiveMixture` backend
    models (`models/dive.py`, `models/dive_mixture.py`), plus - separately, via
    `parse_profile` - the per-sample depth/temperature/tank-pressure curves stored as
    `DiveProfile`. The export has plenty of other fields (algorithm/tissue-loading/CNS/OTU
    stats, PO2 set points, deco stops, `<Marks>`) with nowhere to persist them, so they
    aren't parsed at all.
    """

    key = "suunto_xml"
    content_type = "application/xml"

    @classmethod
    def can_parse(cls, filename: str, content: bytes) -> bool:
        if not filename.lower().endswith(".xml"):
            return False
        try:
            root = DET.fromstring(content)
        except ET.ParseError, DefusedXmlException:
            return False
        return bool(root.tag == _tag("Dive"))

    @classmethod
    def parse(cls, content: bytes) -> ParsedDiveSchema:
        try:
            root = DET.fromstring(content)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise DiveParseError(f"Invalid XML: {exc}") from exc

        if root.tag != _tag("Dive"):
            raise UnsupportedDiveFileError(f"Root element is not a Suunto <Dive>: {root.tag}")

        try:
            return cls._parse_dive(root)
        except (TypeError, ValueError) as exc:
            raise DiveParseError(f"Malformed Suunto XML dive data: {exc}") from exc

    @classmethod
    def parse_profile(cls, content: bytes) -> ParsedProfileSchema | None:
        """Extract `DiveSamples/Dive.Sample` as per-channel series.

        Re-parses the XML from scratch rather than being handed a tree by `parse()`: this
        is a second entry point into XML parsing, and a second place to forget the
        XXE/entity-expansion guard. `DET.fromstring` here is not an accident and has its
        own test.
        """
        try:
            root = DET.fromstring(content)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise DiveParseError(f"Invalid XML: {exc}") from exc

        if root.tag != _tag("Dive"):
            raise UnsupportedDiveFileError(f"Root element is not a Suunto <Dive>: {root.tag}")

        try:
            return cls._parse_samples(root)
        except (TypeError, ValueError) as exc:
            raise DiveParseError(f"Malformed Suunto XML dive samples: {exc}") from exc

    @classmethod
    def _parse_samples(cls, root: ET.Element) -> ParsedProfileSchema | None:
        depth_t: list[float] = []
        depth_v: list[int] = []
        temperature_t: list[float] = []
        temperature_v: list[int] = []
        pressure_t: list[float] = []
        pressure_v: list[int] = []

        for sample in root.findall(f"{_tag('DiveSamples')}/{_tag('Dive.Sample')}"):
            time = _float(sample, "Time")
            if time is None:
                # No axis, no sample. A `Dive.Sample` without a `<Time>` can't be placed
                # on any channel, so it is dropped rather than guessed at.
                continue

            # A nil reading is a gap in that one channel - `_text` already returns None
            # for `xsi:nil="true"` - so the other channels carry on uninterrupted and the
            # chart breaks only the line that actually stopped recording. This is what
            # a mid-dive transmitter dropout looks like (224 of 441 samples, in the real
            # corpus), and it must not truncate depth.
            depth = _scaled_int(_float(sample, "Depth"), _CENTIMETERS_PER_METER)
            if depth is not None:
                depth_t.append(time)
                depth_v.append(depth)

            # `Temperature`, not `AveragedTemperature`: the raw reading is what the sensor
            # saw, and smoothing is a chart decision that shouldn't be baked into storage.
            temperature = _scaled_int(_float(sample, "Temperature"), _TENTHS_PER_UNIT)
            if temperature is not None:
                temperature_t.append(time)
                temperature_v.append(temperature)

            pressure = _scaled_int(_float(sample, "Pressure"), _TENTH_BAR_PER_MILLIBAR)
            if pressure is not None:
                pressure_t.append(time)
                pressure_v.append(pressure)

        if not depth_t and not temperature_t and not pressure_t:
            return None

        return ParsedProfileSchema(
            depth=ParsedSeries(t=depth_t, v=depth_v) if depth_t else None,
            temperature=ParsedSeries(t=temperature_t, v=temperature_v) if temperature_t else None,
            pressure=(
                [ParsedPressureSeries(gas_number=_XML_GAS_NUMBER, t=pressure_t, v=pressure_v)] if pressure_t else []
            ),
        )

    @classmethod
    def _parse_dive(cls, root: ET.Element) -> ParsedDiveSchema:
        mixtures = [cls._parse_mixture(mix) for mix in root.findall(f"{_tag('DiveMixtures')}/{_tag('DiveMixture')}")]

        return ParsedDiveSchema(
            avg_depth=_float(root, "AvgDepth"),
            bottom_temperature=_float(root, "BottomTemperature"),
            dive_number=_int(root, "DiveNumberInSerie"),
            duration=_int(root, "Duration"),
            max_depth=_float(root, "MaxDepth"),
            start_time=_text(root, "StartTime"),
            mixtures=mixtures,
        )

    @staticmethod
    def _parse_mixture(mix: ET.Element) -> DiveMixtureSchema:
        return DiveMixtureSchema(
            # Millibar, not bar - see `_MILLIBAR_PER_BAR`. Reading these as bar stored
            # `start_pressure = 205203` for every dive imported from a 2025+ transmitter
            # export, which made its `gas_use`/RMV meaningless.
            end_pressure=_round2_or_none(_millibar_to_bar(_float(mix, "EndPressure"))),
            helium=_round2_or_none(_float(mix, "Helium")) or 0.0,
            # Left for the user to fill in themselves rather than parsed - see
            # DECISIONS.md.
            name=None,
            oxygen=_round2_or_none(_float(mix, "Oxygen")) or 0.0,
            start_pressure=_round2_or_none(_millibar_to_bar(_float(mix, "StartPressure"))),
            volume=_float(mix, "Size") or 0.0,
        )
