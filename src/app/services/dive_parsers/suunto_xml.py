import xml.etree.ElementTree as ET

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException

from ...schemas.parsed_dive import (
    DiveGasChangeSchema,
    DiveMixtureSchema,
    DiveSampleSchema,
    ParsedDiveSchema,
)
from .base import DiveParser
from .exceptions import DiveParseError, UnsupportedDiveFileError

_SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"
_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


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


class SuuntoXmlParser(DiveParser):
    """Parses Suunto dive-log XML exports (e.g. from the Suunto app / DM5)."""

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
        samples = [cls._parse_sample(s) for s in root.findall(f"{_tag('DiveSamples')}/{_tag('Dive.Sample')}")]

        return ParsedDiveSchema(
            algorithm=_int(root, "Algorithm"),
            altitude_mode=_int(root, "AltitudeMode"),
            ascent_mode=_int(root, "AscentMode"),
            ascent_time=_int(root, "AscentTime"),
            avg_depth=_float(root, "AvgDepth"),
            battery_level=_float(root, "BatteryLevel"),
            bottom_temperature=_float(root, "BottomTemperature"),
            bottom_time=_int(root, "BottomTime"),
            cns_end=_float(root, "CnsEnd"),
            cns_start=_float(root, "CnsStart"),
            cylinder_volume=_float(root, "CylinderVolume"),
            cylinder_work_pressure=_float(root, "CylinderWorkPressure"),
            desaturation_time=_int(root, "DesaturationTime"),
            dive_number=_int(root, "DiveNumberInSerie"),
            diving_days_in_row=_int(root, "DivingDaysInRow"),
            duration=_int(root, "Duration"),
            end_pressure=_float(root, "EndPressure"),
            end_temperature=_float(root, "EndTemperature"),
            last_deco_stop_depth=_float(root, "LastDecoStopDepth"),
            max_depth=_float(root, "MaxDepth"),
            mode=_int(root, "Mode"),
            olf_end=_float(root, "OlfEnd"),
            otu_end=_float(root, "OtuEnd"),
            otu_start=_float(root, "OtuStart"),
            personal_mode=_int(root, "PersonalMode"),
            previous_max_depth=_float(root, "PreviousMaxDepth"),
            sample_interval=_int(root, "SampleInterval"),
            serial_number=_text(root, "SerialNumber"),
            software=_text(root, "Software"),
            source=_text(root, "Source"),
            start_temperature=_float(root, "StartTemperature"),
            start_time=_text(root, "StartTime"),
            surface_pressure=_float(root, "SurfacePressure"),
            surface_time=_int(root, "SurfaceTime"),
            mixtures=mixtures,
            samples=samples,
        )

    @staticmethod
    def _parse_mixture(mix: ET.Element) -> DiveMixtureSchema:
        gas_changes = [
            DiveGasChangeSchema(
                gas_change_time=_int(gc, "GasChangeTime") or 0,
                po2=_float(gc, "PO2"),
                set_point_type=_int(gc, "SetPointType") or 0,
            )
            for gc in mix.findall(f"{_tag('DiveGasChanges')}/{_tag('DiveGasChange')}")
        ]
        return DiveMixtureSchema(
            end_pressure=_float(mix, "EndPressure"),
            helium=_float(mix, "Helium") or 0.0,
            name=_text(mix, "Name"),
            oxygen=_float(mix, "Oxygen") or 0.0,
            po2=_float(mix, "PO2") or 0.0,
            size=_float(mix, "Size") or 0.0,
            start_pressure=_float(mix, "StartPressure"),
            transmitter_id=_text(mix, "TransmitterId"),
            type=_int(mix, "Type") or 0,
            gas_changes=gas_changes,
        )

    @staticmethod
    def _parse_sample(sample: ET.Element) -> DiveSampleSchema:
        return DiveSampleSchema(
            time=_int(sample, "Time") or 0,
            depth=_float(sample, "Depth") or 0.0,
            temperature=_float(sample, "Temperature"),
            averaged_temperature=_float(sample, "AveragedTemperature"),
            ceiling=_float(sample, "Ceiling"),
            gas_time=_float(sample, "GasTime"),
            heading=_float(sample, "Heading"),
            pressure=_float(sample, "Pressure"),
            sac_rate=_float(sample, "SacRate"),
        )
