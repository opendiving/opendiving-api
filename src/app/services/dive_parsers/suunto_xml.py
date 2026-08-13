import xml.etree.ElementTree as ET
from decimal import Decimal

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException

from ...schemas.dive_profile import ParsedPressureSeries, ParsedProfileSchema, ParsedSeries
from ...schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from .base import DiveParser
from .channels import CENTIMETERS_PER_METER, TENTHS_PER_UNIT, scaled_int_or_none
from .exceptions import EXTRACTION_ERRORS, DiveParseError, UnsupportedDiveFileError

_SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"
_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_TWO_DECIMAL_PLACES = Decimal("0.01")

# DM5 XML expresses its *cylinder* pressures in millibar - start/end pressures, the
# per-sample transmitter readings and `CylinderWorkPressure` (200000 = 200 bar) alike.
# Cross-checked against the same dive exported as JSON, where
# `DiveMixture/StartPressure: 205203` reads `20520312` Pascal: both are 205.2 bar. This
# went unnoticed for a long time because pre-2025 exports have no transmitter and write
# `0`.
_MILLIBAR_PER_BAR = Decimal("1000")

# `SurfacePressure` is the exception, and this comment used to say otherwise. It is
# **Pascal**, not millibar: 105700 is 1.057 bar, and reading it as millibar would give
# 105.7 bar - a hundred metres of seawater, at the surface. The corpus settles it twice
# over. Every one of the 384 XML exports lands in 103100-106700, which is a plausible
# barometric range only on the Pascal reading; and the same dives exported as JSON write
# the identical integer into `Header.Diving.SurfacePressure`, which that parser already
# treats as Pascal. `ck_dive_surface_pressure_range` (0.5-1.2 bar) is the backstop that
# would catch this being got wrong again.
_PASCALS_PER_BAR = Decimal("100000")
# Millibar -> tenths of a bar. The depth/temperature conversions this shares with the
# other parsers live in `channels.py`; see `schemas/dive_profile.py` for why the profile
# is stored as scaled integers at all.
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
    """Qualify a bare element name with the Suunto XML namespace.

    ElementTree matches on the fully-qualified `{namespace}name`, so an unqualified
    `find("MaxDepth")` silently matches nothing against these documents rather than
    erroring - which is why every lookup in this module goes through here.
    """
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
    """Convert a DM5 cylinder-pressure reading to bar. See `_MILLIBAR_PER_BAR`.

    Via Decimal (constructed from `str(value)`) rather than plain float division, for the
    same reason as `SuuntoJsonParser._pascals_to_bar`: it avoids introducing binary
    floating-point noise the rounding below would then have to hide.
    """
    return _decimal_divide(value, _MILLIBAR_PER_BAR)


def _pascals_to_bar(value: float | None) -> float | None:
    """Convert `SurfacePressure` to bar - the one field here that isn't millibar."""
    return _decimal_divide(value, _PASCALS_PER_BAR)


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
    `DiveProfile`. The export has plenty of other fields (algorithm/tissue-loading stats,
    deco stops, `<Marks>`) with nowhere to persist them, so they aren't parsed at all.
    """

    key = "suunto_xml"
    content_type = "application/xml"

    @classmethod
    def can_parse(cls, filename: str, content: bytes) -> bool:
        """Whether this looks like a Suunto XML export: a `.xml` file whose root element is
        a namespaced `<Dive>`.

        Parses through `defusedxml`, since this runs on uploaded bytes before anything has
        vouched for them. Answers False for anything malformed instead of raising - the
        caller is choosing between parsers, not parsing yet.
        """
        if not filename.lower().endswith(".xml"):
            return False
        try:
            root = DET.fromstring(content)
        except ET.ParseError, DefusedXmlException:
            return False
        return bool(root.tag == _tag("Dive"))

    @classmethod
    def parse(cls, content: bytes) -> ParsedDiveSchema:
        """Extract the dive itself (not its samples - see `parse_profile`).

        Distinguishes two failures the caller reports differently: a file that isn't this
        format at all raises `UnsupportedDiveFileError`, while one that is but is
        internally broken raises `DiveParseError`. `DET.fromstring` is the XXE-guarded
        parser and is not interchangeable with `ET.fromstring` here.
        """
        try:
            root = DET.fromstring(content)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise DiveParseError(f"Invalid XML: {exc}") from exc

        if root.tag != _tag("Dive"):
            raise UnsupportedDiveFileError(f"Root element is not a Suunto <Dive>: {root.tag}")

        try:
            return cls._parse_dive(root)
        except EXTRACTION_ERRORS as exc:
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
        except EXTRACTION_ERRORS as exc:
            raise DiveParseError(f"Malformed Suunto XML dive samples: {exc}") from exc

    @classmethod
    def _parse_samples(cls, root: ET.Element) -> ParsedProfileSchema | None:
        """Group `DiveSamples/Dive.Sample` into one series per channel.

        Each channel gets its own time axis rather than sharing one: a sample element may
        carry any subset of depth, temperature and tank pressure, so a shared axis would be
        mostly nulls. Samples without a `Time` are skipped - a reading with no position on
        the axis can't be plotted.
        """
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
            depth = scaled_int_or_none(_float(sample, "Depth"), CENTIMETERS_PER_METER)
            if depth is not None:
                depth_t.append(time)
                depth_v.append(depth)

            # `Temperature`, not `AveragedTemperature`: the raw reading is what the sensor
            # saw, and smoothing is a chart decision that shouldn't be baked into storage.
            temperature = scaled_int_or_none(_float(sample, "Temperature"), TENTHS_PER_UNIT)
            if temperature is not None:
                temperature_t.append(time)
                temperature_v.append(temperature)

            pressure = scaled_int_or_none(_float(sample, "Pressure"), _TENTH_BAR_PER_MILLIBAR)
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
        """Map a `<Dive>` element onto `ParsedDiveSchema`.

        Unlike the JSON export this one records `BottomTemperature` directly, so it is read
        rather than derived. `DiveNumberInSerie` is deliberately ignored - see the comment
        below for why importing it would misnumber a diver's log.
        """
        mixtures = [
            cls._parse_mixture(mix, gas_number)
            for gas_number, mix in enumerate(root.findall(f"{_tag('DiveMixtures')}/{_tag('DiveMixture')}"), start=1)
        ]

        return ParsedDiveSchema(
            avg_depth=_float(root, "AvgDepth"),
            bottom_temperature=_float(root, "BottomTemperature"),
            # Already whole percent here, unlike the JSON export of the same dive, which
            # writes CNS as a 0-1 fraction (`CnsEnd: 7` against `EndTissue.CNS: 0.069`).
            # OTU is the same absolute count in both.
            cns_start=_float(root, "CnsStart"),
            cns_end=_float(root, "CnsEnd"),
            otu_start=_float(root, "OtuStart"),
            otu_end=_float(root, "OtuEnd"),
            # Pascal, not millibar - the one pressure in this format that isn't. See
            # `_PASCALS_PER_BAR`. Not passed through `_round2_or_none` like the cylinder
            # pressures are: the Decimal division is already exact (105700 -> 1.057), and
            # rounding to 2 places would throw away the file's own 100 Pa resolution.
            surface_pressure_bar=_pascals_to_bar(_float(root, "SurfacePressure")),
            # Deliberately not parsed, though the export has a `DiveNumberInSerie`. That
            # is the *computer's* counter, not the diver's lifetime dive number: it starts
            # at 1 on a new or factory-reset device and restarts again on the next one, so
            # importing it would stamp a dive #1 onto someone's 300th dive. The field stays
            # on `ParsedDiveSchema` for a format that does carry a real lifetime number
            # (Subsurface's XML does); until then both Suunto parsers leave it null and the
            # number comes from `GET /dives/next-number`, which derives it from the dive's
            # own date - see `services/dive_numbering.py`.
            dive_number=None,
            duration=_int(root, "Duration"),
            max_depth=_float(root, "MaxDepth"),
            start_time=_text(root, "StartTime"),
            mixtures=mixtures,
        )

    @staticmethod
    def _parse_mixture(mix: ET.Element, gas_number: int) -> DiveMixtureSchema:
        """Map one `<DiveMixture>` onto a `DiveMixture`.

        Gas fractions are already percentages here (unlike the JSON export's 0-1
        fractions), but pressures are millibar - see the comment below for what reading
        them as bar did to every transmitter-equipped dive. An element the export omits
        (or marks `xsi:nil`) stays `None` rather than becoming 0.0, since a gas fraction
        that was never recorded is not a gas fraction of zero - see `DiveMixtureSchema`.

        A cylinder with no transmitter is the one case this format writes a value for
        something it didn't measure: `<StartPressure>0</StartPressure>` on 255 of the
        corpus's 353 mixtures, where the same dive's JSON omits the key outright.
        `DiveMixtureSchema` nulls those on the way in - the evidence for it is in that
        validator, since it holds for every parser rather than just this one.
        """
        return DiveMixtureSchema(
            # Millibar, not bar - see `_MILLIBAR_PER_BAR`. Reading these as bar stored
            # `start_pressure = 205203` for every dive imported from a 2025+ transmitter
            # export, which made its `gas_use`/RMV meaningless.
            end_pressure=_round2_or_none(_millibar_to_bar(_float(mix, "EndPressure"))),
            # Position in `<DiveMixtures>`, 1-based, which is the same numbering
            # `_XML_GAS_NUMBER` pins the single pressure channel to and the same one the
            # JSON export of a D5 dive reports in `Cylinders[].GasNumber`. The format has
            # no number of its own; `<TransmitterId>` is a device serial, not an index.
            gas_number=gas_number,
            helium=_round2_or_none(_float(mix, "Helium")),
            # Left for the user to fill in themselves rather than parsed - see
            # DECISIONS.md.
            name=None,
            oxygen=_round2_or_none(_float(mix, "Oxygen")),
            # Already bar here (`<PO2>1.4</PO2>`), unlike the JSON export's Pascal.
            po2_limit=_float(mix, "PO2"),
            # **`<Type>` is deliberately not read as the role**, though it is the only
            # candidate field this format has. It is 1 for all 353 mixtures in the corpus
            # - including both cylinders of the one two-gas dive, where a 21/0 back gas
            # and a 49/0 deco bottle are both `<Type>1</Type>`. Whatever it encodes, it
            # is not what the cylinder was carried for, and mapping it would confidently
            # label a deco bottle "bottom". `<PO2>` is the field that actually separates
            # those two rows (1.4 against 1.6), and it is read above.
            role=None,
            start_pressure=_round2_or_none(_millibar_to_bar(_float(mix, "StartPressure"))),
            volume=_float(mix, "Size"),
        )
