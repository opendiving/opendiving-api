import xml.etree.ElementTree as ET
from decimal import Decimal

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException

from ...schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from .base import DiveParser
from .exceptions import DiveParseError, UnsupportedDiveFileError

_SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"
_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_TWO_DECIMAL_PLACES = Decimal("0.01")


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

    Only extracts fields with a direct equivalent on the `Dive`/`DiveMixture`
    backend models (`models/dive.py`, `models/dive_mixture.py`) - the XML export
    has plenty of other fields (algorithm/tissue-loading/CNS/OTU stats, PO2 set
    points, per-sample depth/temperature profiles, etc.) with nowhere to persist
    them, so they aren't parsed at all.
    """

    @classmethod
    def can_parse(cls, filename: str, content: bytes) -> bool:
        if not filename.lower().endswith(".xml"):
            return False
        try:
            root = DET.fromstring(content)
        except (ET.ParseError, DefusedXmlException):
            return False
        return root.tag == _tag("Dive")

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
            end_pressure=_round2_or_none(_float(mix, "EndPressure")),
            helium=_round2_or_none(_float(mix, "Helium")) or 0.0,
            # Left for the user to fill in themselves rather than parsed - see
            # DECISIONS.md.
            name=None,
            oxygen=_round2_or_none(_float(mix, "Oxygen")) or 0.0,
            start_pressure=_round2_or_none(_float(mix, "StartPressure")),
            volume=_float(mix, "Size") or 0.0,
        )
