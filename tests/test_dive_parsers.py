"""Unit tests for dive-computer export file parsers."""

import json

import pytest

from src.app.services.dive_parsers import DiveParseError, UnsupportedDiveFileError, parse_dive_file
from src.app.services.dive_parsers.suunto_json import SuuntoJsonParser
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser

SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

VALID_SUUNTO_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}" xmlns:i="{XSI_NS}">
  <AvgDepth>12.3</AvgDepth>
  <DiveNumberInSerie>5</DiveNumberInSerie>
  <Duration>1800</Duration>
  <MaxDepth>25.5</MaxDepth>
  <StartTime>2024-05-01T09:00:00</StartTime>
  <DiveMixtures>
    <DiveMixture>
      <!-- Millibar, as every pressure in a DM5 export is: 200000 is 200 bar. -->
      <EndPressure>50000</EndPressure>
      <Helium>0</Helium>
      <Name>Air</Name>
      <Oxygen>21</Oxygen>
      <Size>12</Size>
      <StartPressure>200000</StartPressure>
    </DiveMixture>
  </DiveMixtures>
</Dive>
"""

NOT_A_DIVE_XML = """<?xml version="1.0" encoding="utf-8"?>
<SomethingElse></SomethingElse>
"""

MALFORMED_XML = b"<Dive><Unclosed>"

SUUNTO_XML_WITH_NON_NUMERIC_DEPTH = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <MaxDepth>not-a-number</MaxDepth>
</Dive>
""".encode()

# "Billion laughs" style entity-expansion attack: a handful of nested entity
# definitions that expand exponentially when resolved, aimed at exhausting memory/CPU.
BILLION_LAUGHS_XML = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ELEMENT lolz (#PCDATA)>
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<lolz>&lol3;</lolz>
"""

# XXE attempt: tries to read a local file via an external entity.
XXE_XML = b"""<?xml version="1.0"?>
<!DOCTYPE Dive [
 <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<Dive>&xxe;</Dive>
"""

# Trimmed-down version of a Suunto Ocean/Suunto app JSON export's `DeviceLog.Header`
# (header-only exports don't include per-sample data or gas mixtures).
VALID_SUUNTO_JSON = """
{
  "DeviceLog": {
    "Header": {
      "DateTime": "2026-04-17T11:49:23.510+02:00",
      "Depth": { "Max": 45.91 },
      "DepthAverage": 20.87,
      "DiveTime": 4001.4,
      "Temperature": { "Max": 295.5, "Min": 298.2 }
    }
  }
}
"""

NOT_A_DIVE_JSON = '{"foo": "bar"}'

MALFORMED_JSON = b'{"DeviceLog": {'

# Trimmed-down version of a Suunto D5-style JSON export's `DeviceLog.Header`,
# which nests dive stats (including gas mixtures) under `Header.Diving`
# rather than directly under `Header`.
VALID_SUUNTO_JSON_WITH_GASES = """
{
  "DeviceLog": {
    "Header": {
      "Duration": 4683,
      "Depth": { "Avg": 17.79, "Max": 46.29 },
      "Diving": {
        "Gases": [
          {
            "State": "Primary",
            "Oxygen": 0.21,
            "Helium": 0,
            "PO2": 140000,
            "TransmitterID": "2411100050",
            "TankSize": 0.022,
            "TankFillPressure": 20000000,
            "StartPressure": 20714062,
            "EndPressure": 12243750
          },
          {
            "State": "Primary",
            "Oxygen": 0.49,
            "Helium": 0,
            "PO2": 160000,
            "TankSize": 0.011,
            "TankFillPressure": 20000000
          }
        ]
      }
    }
  }
}
"""


class TestSuuntoXmlParserCanParse:
    """Besides the filename check, this also inspects XML content for a
    recognizable Suunto `<Dive>` root element (mirroring `SuuntoJsonParser`'s
    `DeviceLog.Header` check), so the dispatcher can skip this parser for
    `.xml` files that aren't actually Suunto exports."""

    def test_recognizes_valid_suunto_xml(self):
        assert SuuntoXmlParser.can_parse("export.xml", VALID_SUUNTO_XML.encode()) is True

    def test_rejects_non_xml_extension(self):
        assert SuuntoXmlParser.can_parse("export.txt", VALID_SUUNTO_XML.encode()) is False

    def test_extension_check_is_case_insensitive(self):
        assert SuuntoXmlParser.can_parse("EXPORT.XML", VALID_SUUNTO_XML.encode()) is True

    def test_rejects_xml_with_wrong_root_tag(self):
        assert SuuntoXmlParser.can_parse("export.xml", NOT_A_DIVE_XML.encode()) is False

    def test_rejects_malformed_xml(self):
        assert SuuntoXmlParser.can_parse("export.xml", MALFORMED_XML) is False


class TestSuuntoXmlParserParse:
    def test_parses_top_level_fields(self):
        parsed = SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode())

        assert parsed.avg_depth == 12.3
        assert parsed.duration == 1800
        assert parsed.max_depth == 25.5
        assert parsed.start_time == "2024-05-01T09:00:00"

    def test_ignores_the_computers_own_dive_counter(self):
        """`DiveNumberInSerie` is the device's counter, which restarts on a new or
        factory-reset computer - importing it would stamp a #5 onto a diver's 300th dive.
        The number comes from the dive's date instead (`services/dive_numbering.py`)."""
        assert "<DiveNumberInSerie>5</DiveNumberInSerie>" in VALID_SUUNTO_XML

        assert SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode()).dive_number is None

    def test_parses_mixtures(self):
        parsed = SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode())

        assert len(parsed.mixtures) == 1
        mixture = parsed.mixtures[0]
        assert mixture.name is None
        assert mixture.oxygen == 21
        assert mixture.helium == 0
        assert mixture.volume == 12
        assert mixture.start_pressure == 200
        assert mixture.end_pressure == 50

    def test_rounds_mixture_pressures_and_percentages_to_two_decimal_places(self):
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures>
    <DiveMixture>
      <StartPressure>207140.62</StartPressure>
      <EndPressure>122437.5</EndPressure>
      <Oxygen>20.999</Oxygen>
      <Helium>0.001</Helium>
      <Size>12</Size>
    </DiveMixture>
  </DiveMixtures>
</Dive>
""".encode()

        parsed = SuuntoXmlParser.parse(xml)

        mixture = parsed.mixtures[0]
        assert mixture.start_pressure == 207.14
        assert mixture.end_pressure == 122.44
        assert mixture.oxygen == 21.0
        assert mixture.helium == 0.0

    def test_reads_mixture_pressures_as_millibar(self):
        """DM5 expresses every pressure in millibar, including these.

        The values here are from `Dive_2025-05-31-1259.xml`, whose JSON twin
        (`685013accbecd72812f3d840.json`) reports the same cylinder as 20520312 /
        8678125 Pascal - 205.2 and 86.78 bar. Reading these as bar is what used to store
        `start_pressure = 205203` and make the dive's RMV meaningless.
        """
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures>
    <DiveMixture>
      <StartPressure>205203</StartPressure>
      <EndPressure>86781</EndPressure>
      <Oxygen>31</Oxygen>
      <Size>11</Size>
    </DiveMixture>
  </DiveMixtures>
</Dive>
""".encode()

        mixture = SuuntoXmlParser.parse(xml).mixtures[0]

        assert mixture.start_pressure == 205.2
        assert mixture.end_pressure == 86.78

    def test_leaves_a_zero_mixture_pressure_at_zero(self):
        """Pre-transmitter exports write `0`, which must stay `0` rather than becoming a
        tiny non-zero number - 255 of the 353 `StartPressure` values in the local corpus
        are exactly this."""
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures>
    <DiveMixture>
      <StartPressure>0</StartPressure>
      <EndPressure>0</EndPressure>
      <Oxygen>21</Oxygen>
      <Size>12</Size>
    </DiveMixture>
  </DiveMixtures>
</Dive>
""".encode()

        mixture = SuuntoXmlParser.parse(xml).mixtures[0]

        assert mixture.start_pressure == 0.0
        assert mixture.end_pressure == 0.0

    def test_raises_unsupported_for_xml_with_wrong_root_tag(self):
        with pytest.raises(UnsupportedDiveFileError):
            SuuntoXmlParser.parse(NOT_A_DIVE_XML.encode())

    def test_raises_dive_parse_error_on_malformed_xml(self):
        with pytest.raises(DiveParseError):
            SuuntoXmlParser.parse(MALFORMED_XML)

    def test_raises_dive_parse_error_on_billion_laughs_entity_expansion(self):
        with pytest.raises(DiveParseError):
            SuuntoXmlParser.parse(BILLION_LAUGHS_XML)

    def test_raises_dive_parse_error_on_xxe(self):
        with pytest.raises(DiveParseError):
            SuuntoXmlParser.parse(XXE_XML)

    def test_raises_dive_parse_error_on_non_numeric_field(self):
        """Well-formed XML with the right root tag, but a numeric field that isn't
        actually numeric, should fail safely rather than raise an unhandled ValueError."""
        with pytest.raises(DiveParseError):
            SuuntoXmlParser.parse(SUUNTO_XML_WITH_NON_NUMERIC_DEPTH)


class TestSuuntoJsonParserCanParse:
    """Unlike `SuuntoXmlParser.can_parse`, this also inspects JSON content -
    it's how this parser decides the file is a recognizable Suunto export at
    all, since `.json` alone isn't a distinctive enough signal."""

    def test_recognizes_valid_suunto_json(self):
        assert SuuntoJsonParser.can_parse("export.json", VALID_SUUNTO_JSON.encode()) is True

    def test_rejects_non_json_extension(self):
        assert SuuntoJsonParser.can_parse("export.txt", VALID_SUUNTO_JSON.encode()) is False

    def test_extension_check_is_case_insensitive(self):
        assert SuuntoJsonParser.can_parse("EXPORT.JSON", VALID_SUUNTO_JSON.encode()) is True

    def test_rejects_json_without_device_log(self):
        assert SuuntoJsonParser.can_parse("export.json", NOT_A_DIVE_JSON.encode()) is False

    def test_rejects_json_with_device_log_but_no_header(self):
        assert SuuntoJsonParser.can_parse("export.json", b'{"DeviceLog": {}}') is False

    def test_rejects_malformed_json(self):
        assert SuuntoJsonParser.can_parse("export.json", MALFORMED_JSON) is False


class TestSuuntoJsonParserParse:
    def test_parses_header_fields(self):
        parsed = SuuntoJsonParser.parse(VALID_SUUNTO_JSON.encode())

        assert parsed.max_depth == 45.91
        assert parsed.avg_depth == 20.87
        assert parsed.duration == 4001
        assert parsed.start_time == "2026-04-17T11:49:23.510+02:00"
        assert parsed.mixtures == []

    def test_converts_temperature_from_kelvin_to_celsius(self):
        parsed = SuuntoJsonParser.parse(VALID_SUUNTO_JSON.encode())

        # 295.5 K -> 22.35 C is the colder (lower) of the two recorded extremes.
        assert parsed.bottom_temperature == 22.35

    def test_kelvin_to_celsius_conversion_has_no_floating_point_noise(self):
        """Regression test: 293.75 K - 273.15 lands on 20.600000000000023 in raw
        IEEE 754 double arithmetic; the converted value must be rounded off to
        match the source data's 2 decimal places instead of leaking that noise."""
        data = {"DeviceLog": {"Header": {"Temperature": {"Max": 293.75}}}}

        parsed = SuuntoJsonParser.parse(json.dumps(data).encode())

        assert parsed.bottom_temperature == 20.6

    def test_parses_header_only_export_without_samples_key(self):
        """Regression test: `DeviceLog.Samples` is optional (e.g. the "clean"
        header-only fixture this parser was built against omits it entirely) -
        parsing must not assume it's present."""
        assert "Samples" not in json.loads(VALID_SUUNTO_JSON)["DeviceLog"]

        parsed = SuuntoJsonParser.parse(VALID_SUUNTO_JSON.encode())

        assert parsed.max_depth == 45.91

    def test_falls_back_to_sample_temperatures_when_header_has_none(self):
        data = {
            "DeviceLog": {
                "Header": {"Depth": {"Max": 10.0}},
                "Samples": [
                    {"Temperature": 295.5},
                    {"Temperature": 298.2},
                    {"NoTemperatureHere": True},
                ],
            }
        }

        parsed = SuuntoJsonParser.parse(json.dumps(data).encode())

        assert parsed.bottom_temperature == pytest.approx(22.35)

    def test_parses_gas_mixtures_from_diving_gases(self):
        """D5-style exports nest dive stats (including gases) under
        `Header.Diving` rather than directly under `Header`."""
        parsed = SuuntoJsonParser.parse(VALID_SUUNTO_JSON_WITH_GASES.encode())

        assert len(parsed.mixtures) == 2

    def test_falls_back_to_duration_when_dive_time_absent(self):
        """D5-style exports report this as `Duration` rather than `DiveTime`."""
        parsed = SuuntoJsonParser.parse(VALID_SUUNTO_JSON_WITH_GASES.encode())

        assert parsed.duration == 4683

    def test_prefers_dive_time_over_duration_when_both_present(self):
        data = {"DeviceLog": {"Header": {"DiveTime": 100, "Duration": 200}}}

        parsed = SuuntoJsonParser.parse(json.dumps(data).encode())

        assert parsed.duration == 100

    def test_converts_gas_mixture_units_from_si(self):
        """Gases report pressure in Pascal (not bar), tank size in cubic meters
        (not liters), and oxygen/helium as a 0-1 fraction (not a percentage)."""
        parsed = SuuntoJsonParser.parse(VALID_SUUNTO_JSON_WITH_GASES.encode())

        primary = parsed.mixtures[0]
        assert primary.oxygen == 21.0
        assert primary.helium == 0.0
        assert primary.volume == 22.0
        # Rounded from 207.14062/122.4375 bar to 2 decimal places.
        assert primary.start_pressure == 207.14
        assert primary.end_pressure == 122.44
        assert primary.name is None

    def test_defaults_missing_mixture_pressures_to_none(self):
        """The second gas in the fixture has no `StartPressure`/`EndPressure`
        (e.g. an untransmitted backup/deco cylinder) - these should come back
        as `None`, not `0` or a crash."""
        parsed = SuuntoJsonParser.parse(VALID_SUUNTO_JSON_WITH_GASES.encode())

        secondary = parsed.mixtures[1]
        assert secondary.oxygen == 49.0
        assert secondary.start_pressure is None
        assert secondary.end_pressure is None

    def test_mixtures_default_to_empty_list_without_diving_gases(self):
        parsed = SuuntoJsonParser.parse(VALID_SUUNTO_JSON.encode())

        assert parsed.mixtures == []

    def test_raises_dive_parse_error_when_device_log_header_missing(self):
        """Format recognition lives in `can_parse`; if `parse` is called directly
                on data that doesn't match, it still fails safely r
        ather than crashing."""
        with pytest.raises(DiveParseError):
            SuuntoJsonParser.parse(NOT_A_DIVE_JSON.encode())

    def test_raises_dive_parse_error_on_malformed_json(self):
        with pytest.raises(DiveParseError):
            SuuntoJsonParser.parse(MALFORMED_JSON)


class TestParseDiveFile:
    def test_dispatches_to_suunto_parser(self):
        parsed = parse_dive_file("export.xml", VALID_SUUNTO_XML.encode())

        assert parsed.max_depth == 25.5

    def test_dispatches_to_suunto_json_parser(self):
        parsed = parse_dive_file("export.json", VALID_SUUNTO_JSON.encode())

        assert parsed.max_depth == 45.91

    def test_raises_unsupported_for_unrecognized_file(self):
        with pytest.raises(UnsupportedDiveFileError):
            parse_dive_file("export.csv", b"time,depth\n0,0\n")

    def test_raises_unsupported_for_xml_with_unknown_root(self):
        with pytest.raises(UnsupportedDiveFileError):
            parse_dive_file("export.xml", NOT_A_DIVE_XML.encode())

    def test_raises_unsupported_for_json_without_device_log(self):
        with pytest.raises(UnsupportedDiveFileError):
            parse_dive_file("export.json", b'{"foo": "bar"}')
