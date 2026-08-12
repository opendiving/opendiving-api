"""Unit tests for dive-computer export file parsers."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.app.services.dive_parsers import DiveParseError, UnsupportedDiveFileError, parse_dive_file
from src.app.services.dive_parsers.fit import FitParser
from src.app.services.dive_parsers.suunto_json import SuuntoJsonParser
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser
from tests.helpers.fit import DevField, Message, dive_fit_file, fit_file, message

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


def _ocean_json(samples: list[dict]) -> bytes:
    """A 2026 Suunto Ocean-shaped export: no `Header.Diving`, gas data only in the
    samples' `Cylinders`. Five cylinder slots per sample with one paired, as the device
    writes them."""
    return json.dumps(
        {
            "DeviceLog": {
                "Header": {"DateTime": "2026-04-03T12:04:11.390+02:00", "Depth": {"Max": 21.1}},
                "Samples": samples,
            }
        }
    ).encode()


def _ocean_sample(offset_seconds: int, pressure: int | None) -> dict:
    minute, second = divmod(offset_seconds, 60)
    return {
        "TimeISO8601": f"2026-04-03T12:{4 + minute:02d}:{11 + second:02d}.390+02:00",
        "Cylinders": [{"GasNumber": 0, "GasTime": 2789, "Pressure": pressure, "Ventilation": 0.00018}]
        + [{"GasNumber": n, "GasTime": 0, "Pressure": None, "Ventilation": 0} for n in range(1, 5)],
    }


OCEAN_JSON_WITH_CYLINDERS = _ocean_json(
    [
        _ocean_sample(0, 20510938),
        _ocean_sample(10, 15000000),
        _ocean_sample(20, 9155000),
        # The Ocean nulls out even the live slot on its final samples - that must not be
        # read as the end pressure, nor end the series.
        _ocean_sample(30, None),
    ]
)

# The same readings with the array out of chronological order, which is what the
# interleaved sensor streams actually produce.
OCEAN_JSON_WITH_UNORDERED_CYLINDERS = _ocean_json(
    [
        _ocean_sample(20, 9155000),
        _ocean_sample(0, 20510938),
        _ocean_sample(10, 15000000),
    ]
)


# FIT fixtures are built rather than pasted - see `tests/helpers/fit.py` for why a
# binary format gets a writer instead of committed blobs.
DIVE_START = datetime(2026, 4, 17, 9, 49, 23, tzinfo=UTC)
# When the device finished writing the file. The dive was logged at UTC+02:00, which is
# recoverable only from the gap between this and `local_timestamp`.
ACTIVITY_END = datetime(2026, 4, 17, 11, 1, 5, tzinfo=UTC)


def _records(samples: list[tuple[int, float, int]]) -> list[Message]:
    """`record` messages from `(seconds after the dive started, depth m, temperature C)`."""
    return [
        message("record", timestamp=DIVE_START + timedelta(seconds=offset), depth=depth, temperature=temperature)
        for offset, depth, temperature in samples
    ]


# A whole dive: a Suunto Ocean-shaped export, down to the +02:00 offset and the single
# nitrox mixture. `dive_fit_file` supplies the session around whatever is passed in.
VALID_FIT = dive_fit_file(
    message("activity", timestamp=ACTIVITY_END, local_timestamp=ACTIVITY_END + timedelta(hours=2), num_sessions=1),
    message("dive_gas", message_index=0, oxygen_content=32, helium_content=0, status="enabled"),
    *_records([(0, 1.45, 25), (1200, 45.91, 22), (2400, 20.0, 22), (4290, 0.0, 24)]),
    total_elapsed_time=4301.72,
)


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

    def test_reconstructs_mixtures_from_cylinder_telemetry(self):
        """The 2026 Ocean export is a third header shape with no `Header.Diving` at all,
        so the `Gases` path finds nothing - and every dive imported from one used to come
        back with no mixtures, despite the file carrying hundreds of transmitter
        readings."""
        parsed = SuuntoJsonParser.parse(OCEAN_JSON_WITH_CYLINDERS)

        assert len(parsed.mixtures) == 1
        mixture = parsed.mixtures[0]
        # 20510938 Pa -> 205.11 bar, 9155000 Pa -> 91.55 bar.
        assert mixture.start_pressure == 205.11
        assert mixture.end_pressure == 91.55
        # Nothing in this export records a gas fraction or a tank size, and the parser
        # does not invent one: reporting air here would be indistinguishable from having
        # read air. The form fills these from `DEFAULT_MIXTURE` instead.
        assert mixture.oxygen is None
        assert mixture.helium is None
        assert mixture.volume is None

    def test_ignores_cylinder_slots_that_never_reported(self):
        """An Ocean reports five cylinder slots on every sample with only one paired."""
        parsed = SuuntoJsonParser.parse(OCEAN_JSON_WITH_CYLINDERS)

        assert len(parsed.mixtures) == 1

    def test_cylinder_pressures_are_ordered_by_time_not_by_position(self):
        """The union of an Ocean's sample timestamps is not monotonic - separate sensor
        streams are appended out of order - so the last entry in the array is not
        reliably the last reading of the dive."""
        parsed = SuuntoJsonParser.parse(OCEAN_JSON_WITH_UNORDERED_CYLINDERS)
        mixture = parsed.mixtures[0]

        assert mixture.start_pressure == 205.11
        assert mixture.end_pressure == 91.55

    def test_a_gases_block_wins_over_cylinder_telemetry(self):
        """`Gases` carries the gas fraction and tank size that telemetry can't, so it is
        authoritative wherever the export has one."""
        parsed = SuuntoJsonParser.parse(VALID_SUUNTO_JSON_WITH_GASES.encode())

        assert [mixture.oxygen for mixture in parsed.mixtures] == [21.0, 49.0]
        assert parsed.mixtures[0].volume == 22.0

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


class TestFitParserCanParse:
    """Purely syntactic: a `.fit` name plus the `.FIT` magic at offset 8. No decoding
    happens here, so nothing in this class can raise."""

    def test_recognizes_a_fit_file(self):
        assert FitParser.can_parse("dive.fit", VALID_FIT) is True

    def test_rejects_non_fit_extension(self):
        assert FitParser.can_parse("dive.xml", VALID_FIT) is False

    def test_extension_check_is_case_insensitive(self):
        assert FitParser.can_parse("DIVE.FIT", VALID_FIT) is True

    def test_rejects_fit_extension_without_the_magic(self):
        """A rename is not a format. `.fit` on a JSON export must not reach `parse()`."""
        assert FitParser.can_parse("dive.fit", VALID_SUUNTO_JSON.encode()) is False

    def test_rejects_a_file_too_short_to_hold_a_header(self):
        assert FitParser.can_parse("dive.fit", b"\x0c\x20") is False


class TestFitParserParse:
    def test_extracts_the_dive(self):
        parsed = FitParser.parse(VALID_FIT)

        assert parsed.max_depth == 45.91
        assert parsed.avg_depth == 19.43
        assert parsed.duration == 4302
        assert parsed.bottom_temperature == 22.0

    def test_prefers_the_native_field_over_a_developer_field_of_the_same_name(self):
        """The one thing a FIT reader has to get right for Suunto's exports.

        Its exporter declares a `float32` developer field named `max_depth` alongside the
        native `uint32`/scale-1000 one, so the same session carries 32.41 and
        32.40999984741211. Collecting fields into a dict by name - the obvious way to
        walk `frame.fields` - keeps the second.
        """
        content = fit_file(
            message(
                "session",
                DevField(name="max_depth", value=32.41, field_number=5, units="m"),
                sport="diving",
                start_time=DIVE_START,
                total_elapsed_time=2001.0,
                max_depth=32.41,
            ),
        )

        assert FitParser.parse(content).max_depth == 32.41

    def test_start_time_carries_the_dive_s_own_utc_offset(self):
        """`activity.local_timestamp` is the only record of where the dive happened.

        Both are the same instant; the gap between them is the offset at the dive site.
        Losing it would log an 11:49 Red Sea dive as 09:49.
        """
        parsed = FitParser.parse(VALID_FIT)

        assert parsed.start_time == "2026-04-17T11:49:23+02:00"

    def test_start_time_falls_back_to_utc_without_an_activity_message(self):
        parsed = FitParser.parse(dive_fit_file())

        assert parsed.start_time == "2026-04-17T09:49:23+00:00"

    def test_ignores_a_local_timestamp_no_timezone_could_explain(self):
        """A corrupt `local_timestamp` becomes "no offset recorded", not a 40-hour zone -
        `timezone()` raises past +/-24 h, which would surface as a 500 on upload."""
        content = dive_fit_file(
            message(
                "activity",
                timestamp=ACTIVITY_END,
                local_timestamp=ACTIVITY_END + timedelta(hours=40),
                num_sessions=1,
            ),
        )

        assert FitParser.parse(content).start_time == "2026-04-17T09:49:23+00:00"

    def test_never_imports_the_computer_s_dive_number(self):
        """`session.dive_number` counts dives on *that device*, not in the diver's log:
        it restarts at 1 after a factory reset or a new computer. The corpus shows it
        outright - a D5 reporting `dive_number` 5 for a dive the diver labelled "#28"."""
        content = dive_fit_file(dive_number=5)

        assert FitParser.parse(content).dive_number is None

    def test_duration_falls_back_to_the_timer_time(self):
        content = fit_file(
            message("session", sport="diving", start_time=DIVE_START, total_timer_time=1800.4),
        )

        assert FitParser.parse(content).duration == 1800

    def test_duration_falls_back_to_a_garmin_dive_summary(self):
        content = fit_file(
            message("dive_summary", bottom_time=1500.0, max_depth=30.0),
            message("session", sport="diving", start_time=DIVE_START),
        )

        assert FitParser.parse(content).duration == 1500

    def test_depths_fall_back_to_a_garmin_dive_summary(self):
        content = fit_file(
            message("dive_summary", avg_depth=11.2, max_depth=30.5),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=1800.0),
        )
        parsed = FitParser.parse(content)

        assert parsed.max_depth == 30.5
        assert parsed.avg_depth == 11.2

    def test_bottom_temperature_prefers_the_recorded_minimum(self):
        content = dive_fit_file(min_temperature=18)

        assert FitParser.parse(content).bottom_temperature == 18.0

    def test_bottom_temperature_falls_back_to_the_coldest_sample(self):
        content = dive_fit_file(
            *_records([(0, 5.0, 25), (60, 30.0, 21), (120, 10.0, 24)]),
        )

        assert FitParser.parse(content).bottom_temperature == 21.0

    def test_bottom_temperature_ignores_suunto_s_max_temperature(self):
        """Suunto writes the *coldest* reading into `max_temperature`: both Ocean exports
        in the corpus hold 22 there while their samples run 22-25. Reading it as a
        maximum would be wrong and reading it as a minimum would bake one vendor's bug
        into the parser, so the sample stream decides instead."""
        content = dive_fit_file(
            *_records([(0, 5.0, 25), (60, 30.0, 23)]),
            max_temperature=22,
        )

        assert FitParser.parse(content).bottom_temperature == 23.0

    def test_extracts_gas_mixtures(self):
        parsed = FitParser.parse(VALID_FIT)

        assert len(parsed.mixtures) == 1
        mixture = parsed.mixtures[0]
        assert mixture.oxygen == 32.0
        # An explicitly recorded 0 % helium, unlike `volume` below - this is a reading.
        assert mixture.helium == 0.0
        # FIT has nowhere to record cylinder size at all, so it comes back null rather
        # than as a cylinder of no volume.
        assert mixture.volume is None
        assert mixture.name is None

    def test_skips_gases_the_diver_did_not_breathe(self):
        """A computer stores its whole configured gas list. Importing the disabled ones
        would put deco gases on a recreational air dive."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("dive_gas", message_index=1, oxygen_content=50, helium_content=0, status="disabled"),
        )
        parsed = FitParser.parse(content)

        assert [mixture.oxygen for mixture in parsed.mixtures] == [21.0]

    def test_orders_gases_by_message_index(self):
        content = dive_fit_file(
            message("dive_gas", message_index=1, oxygen_content=54, helium_content=0, status="enabled"),
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
        )
        parsed = FitParser.parse(content)

        assert [mixture.oxygen for mixture in parsed.mixtures] == [21.0, 54.0]

    def test_takes_tank_pressures_from_a_matching_garmin_tank_summary(self):
        """Nothing in the file links a `tank_summary` (keyed by transmitter ANT id) to a
        `dive_gas` (keyed by `message_index`), so they are paired by position."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("tank_summary", sensor=2411100050, start_pressure=207.0, end_pressure=62.0),
        )
        mixture = FitParser.parse(content).mixtures[0]

        assert mixture.start_pressure == 207.0
        assert mixture.end_pressure == 62.0

    def test_derives_tank_pressures_from_telemetry_without_a_tank_summary(self):
        """A Descent streams `tank_update` throughout the dive whether or not it also
        writes a `tank_summary`, so the first and last reading stand in for one."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("tank_update", timestamp=DIVE_START, sensor=2411100050, pressure=207.0),
            message("tank_update", timestamp=DIVE_START + timedelta(seconds=600), sensor=2411100050, pressure=150.0),
            message("tank_update", timestamp=DIVE_START + timedelta(seconds=1800), sensor=2411100050, pressure=62.0),
        )
        mixture = FitParser.parse(content).mixtures[0]

        assert mixture.start_pressure == 207.0
        assert mixture.end_pressure == 62.0

    def test_tank_telemetry_is_ordered_by_time_not_by_arrival(self):
        """Two pods interleave in the file, so "last message seen" is not "last reading"."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("tank_update", timestamp=DIVE_START + timedelta(seconds=1800), sensor=2411100050, pressure=62.0),
            message("tank_update", timestamp=DIVE_START, sensor=2411100050, pressure=207.0),
        )
        mixture = FitParser.parse(content).mixtures[0]

        assert mixture.start_pressure == 207.0
        assert mixture.end_pressure == 62.0

    def test_a_tank_summary_wins_over_the_telemetry(self):
        """The device's own summary is authoritative where it wrote one."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("tank_update", timestamp=DIVE_START, sensor=2411100050, pressure=190.0),
            message("tank_summary", sensor=2411100050, start_pressure=207.0, end_pressure=62.0),
        )
        mixture = FitParser.parse(content).mixtures[0]

        assert mixture.start_pressure == 207.0
        assert mixture.end_pressure == 62.0

    def test_leaves_tank_pressures_null_when_the_counts_disagree(self):
        """Two gases and one pod: position says nothing, and a confidently wrong start
        pressure produces a plausible, wrong RMV - worse than an empty field."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("dive_gas", message_index=1, oxygen_content=50, helium_content=0, status="enabled"),
            message("tank_summary", sensor=2411100050, start_pressure=207.0, end_pressure=62.0),
        )
        parsed = FitParser.parse(content)

        assert [mixture.start_pressure for mixture in parsed.mixtures] == [None, None]

    def test_rejects_a_fit_file_that_is_not_a_dive(self):
        """A bike ride is a FIT file this parser read successfully - it just holds no
        dive. `DiveParseError` (422, with the reason) rather than
        `UnsupportedDiveFileError`, which would tell the diver 415 "no parser available
        for this file" about a format that is very much supported."""
        content = fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("session", sport="cycling", start_time=DIVE_START, total_elapsed_time=3600.0),
        )

        with pytest.raises(DiveParseError, match="not a dive"):
            FitParser.parse(content)

    def test_accepts_a_session_without_a_sport_when_it_carries_depth(self):
        """Depth samples are stronger evidence than a missing `sport`."""
        content = fit_file(
            *_records([(0, 5.0, 25), (60, 30.0, 21)]),
            message("session", start_time=DIVE_START, total_elapsed_time=1800.0, max_depth=30.0),
        )

        assert FitParser.parse(content).max_depth == 30.0

    def test_raises_dive_parse_error_when_there_is_no_session(self):
        content = fit_file(message("file_id", type="activity", manufacturer="suunto"))

        with pytest.raises(DiveParseError, match="no session"):
            FitParser.parse(content)

    def test_raises_dive_parse_error_on_a_truncated_file(self):
        with pytest.raises(DiveParseError):
            FitParser.parse(VALID_FIT[: len(VALID_FIT) // 2])

    def test_raises_dive_parse_error_on_bytes_that_are_not_fit(self):
        with pytest.raises(DiveParseError):
            FitParser.parse(b"this is not a FIT file")


class TestParsersInventNothing:
    """No parser substitutes a plausible value for gas data a file doesn't carry.

    All three used to coerce a missing `oxygen`/`helium`/`volume` to `0.0`, which reads
    as a hypoxic gas in a cylinder of no volume - obviously-wrong values presented as
    readings, and `volume: 0.0` additionally overwrote the dive form's own 11.1 L
    default with something `ck_dive_mixture_volume_positive` rejects. `None` is the only
    honest answer for "the file didn't say"; the form fills the gap from
    `DEFAULT_MIXTURE`. See `DiveMixtureSchema`.
    """

    def test_xml_leaves_an_omitted_gas_fraction_null(self):
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures><DiveMixture><Name>Air</Name></DiveMixture></DiveMixtures>
</Dive>
""".encode()

        mixture = SuuntoXmlParser.parse(content).mixtures[0]

        assert mixture.oxygen is None
        assert mixture.helium is None
        assert mixture.volume is None

    def test_json_leaves_an_omitted_gas_fraction_null(self):
        content = json.dumps({"DeviceLog": {"Header": {"Diving": {"Gases": [{"State": "Primary"}]}}}}).encode()

        mixture = SuuntoJsonParser.parse(content).mixtures[0]

        assert mixture.oxygen is None
        assert mixture.helium is None
        assert mixture.volume is None

    def test_fit_leaves_an_omitted_gas_fraction_null(self):
        content = dive_fit_file(message("dive_gas", message_index=0, status="enabled"))

        mixture = FitParser.parse(content).mixtures[0]

        assert mixture.oxygen is None
        assert mixture.helium is None
        assert mixture.volume is None

    def test_an_explicitly_recorded_zero_is_still_a_reading(self):
        """The rule is "don't invent", not "treat zero as missing" - a nitrox export
        that records `Helium: 0` has genuinely recorded 0 % helium."""
        content = json.dumps({"DeviceLog": {"Header": {"Diving": {"Gases": [{"Oxygen": 0.32, "Helium": 0}]}}}}).encode()

        mixture = SuuntoJsonParser.parse(content).mixtures[0]

        assert mixture.helium == 0.0
        assert mixture.oxygen == 32.0


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

    def test_dispatches_to_fit_parser(self):
        parsed = parse_dive_file("dive.fit", VALID_FIT)

        assert parsed.max_depth == 45.91

    def test_raises_unsupported_for_fit_extension_without_the_magic(self):
        with pytest.raises(UnsupportedDiveFileError):
            parse_dive_file("dive.fit", b"time,depth\n0,0\n")
