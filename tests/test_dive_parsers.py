"""Unit tests for dive-computer export file parsers."""

import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from src.app.models.dive import Dive
from src.app.schemas.dive_mixture import GasRole
from src.app.schemas.parsed_dive import ParsedDiveSchema
from src.app.services.dive_parsers import DiveParseError, UnsupportedDiveFileError, parse_dive_file
from src.app.services.dive_parsers.fit import _MAX_CYLINDERS, FitParser
from src.app.services.dive_parsers.fit import _MAX_FRAMES as MAX_FRAMES
from src.app.services.dive_parsers.suunto_json import _MAX_CYLINDERS as JSON_MAX_CYLINDERS
from src.app.services.dive_parsers.suunto_json import SuuntoJsonParser
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser
from tests.helpers.fit import (
    DevField,
    Message,
    dense_record_stream,
    dive_fit_file,
    fit_file,
    message,
)

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


def _ocean_json(samples: list[dict], dive_time: float = 300.0) -> bytes:
    """A 2026 Suunto Ocean-shaped export: no `Header.Diving`, gas data only in the
    samples' `Cylinders` and `DiveEvents`. Five cylinder slots per sample with one
    paired, as the device writes them. `DiveTime` is the in-water time, which the
    samples routinely outlast."""
    return json.dumps(
        {
            "DeviceLog": {
                "Header": {
                    "DateTime": "2026-04-03T12:04:11.390+02:00",
                    "DiveTime": dive_time,
                    "Depth": {"Max": 21.1},
                },
                "Samples": samples,
            }
        }
    ).encode()


# Real arithmetic rather than string interpolation of `4 + minute`/`11 + second`, which
# didn't carry: an offset of 49 s produced `12:04:60` and one of 3 360 s `12:60:11`. The
# fixtures below happened to dodge it, and the next one to pick a natural offset would have
# got a `ValueError` from `datetime.fromisoformat` deep inside the parser, reading as a
# parser bug rather than a broken fixture.
OCEAN_ORIGIN = datetime.fromisoformat("2026-04-03T12:04:11.390+02:00")


def _ocean_time(offset_seconds: int) -> str:
    return (OCEAN_ORIGIN + timedelta(seconds=offset_seconds)).isoformat()


def _ocean_sample(offset_seconds: int, pressure: int | None, gas_number: int = 0) -> dict:
    """One sample carrying a cylinder reading on `gas_number`, and null in every other slot."""
    return {
        "TimeISO8601": _ocean_time(offset_seconds),
        "Cylinders": [
            {
                "GasNumber": n,
                "GasTime": 2789 if n == gas_number else 0,
                "Pressure": pressure if n == gas_number else None,
                "Ventilation": 0.00018 if n == gas_number else 0,
            }
            for n in range(5)
        ],
    }


def _ocean_gas_switch(offset_seconds: int, gas_number: int) -> dict:
    """The only record this export keeps of *which* cylinders were on the dive."""
    return {
        "TimeISO8601": _ocean_time(offset_seconds),
        "DiveEvents": {"GasSwitch": {"GasNumber": gas_number}},
    }


OCEAN_JSON_WITH_CYLINDERS = _ocean_json(
    [
        _ocean_gas_switch(0, 0),
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

# A real two-gas dive: the diver starts on gas 0 (transmitted) and switches to a deco
# cylinder with no pod on it. Only gas 0 ever reports a pressure.
OCEAN_JSON_MULTI_GAS = _ocean_json(
    [
        _ocean_gas_switch(0, 0),
        _ocean_sample(0, 21162500),
        _ocean_sample(60, 12727000),
        _ocean_gas_switch(120, 1),
    ]
)

# The dive ends at `DiveTime`, but the computer keeps logging on the boat - where the
# diver purges the regulator and the transmitter reports an empty tank.
OCEAN_JSON_WITH_POST_DIVE_PURGE = _ocean_json(
    [
        _ocean_gas_switch(0, 0),
        _ocean_sample(0, 20469000),
        _ocean_sample(240, 5334000),
        _ocean_sample(360, 12000),
    ],
    dive_time=300.0,
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

    def test_reads_a_zero_mixture_pressure_as_no_reading(self):
        """Pre-transmitter exports write `0` - 255 of the 353 `StartPressure` values in
        the local corpus - and `DiveMixtureSchema` nulls it: 0 bar is DM5's way of saying
        no transmitter, not a cylinder that was breathed from empty. See that validator
        for the corpus evidence, and `TestParsersInventNothing` for why this is not the
        same as treating a recorded `Helium: 0` as missing.

        This used to assert `0.0`, guarding the millibar conversion against turning an
        exact zero into a tiny non-zero number. That guard survives the change: `None`
        here still requires the conversion to land on exactly zero.
        """
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

        assert mixture.start_pressure is None
        assert mixture.end_pressure is None

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
        """An Ocean reports five cylinder slots on every sample with only one paired, and
        a slot nothing was ever breathed from is not a cylinder."""
        parsed = SuuntoJsonParser.parse(OCEAN_JSON_WITH_CYLINDERS)

        assert len(parsed.mixtures) == 1

    def test_lists_a_switched_to_cylinder_that_had_no_transmitter(self):
        """The cylinder list comes from `DiveEvents.GasSwitch`, not from which tanks
        transmitted - so a deco bottle with no pod still appears.

        Building it from telemetry alone would emit a single mixture carrying both
        pressures, which is exactly the shape `compute_gas_use` derives an RMV from: a
        stage bottle's pressure drop would have been attributed to the whole dive. With
        two cylinders present the RMV correctly declines to compute.
        """
        parsed = SuuntoJsonParser.parse(OCEAN_JSON_MULTI_GAS)

        assert len(parsed.mixtures) == 2
        # The pressures land on the cylinder that actually reported them - `Cylinders[]`
        # and `GasSwitch` share one gas numbering, so this is read, not guessed.
        assert parsed.mixtures[0].start_pressure == 211.62
        assert parsed.mixtures[0].end_pressure == 127.27
        assert parsed.mixtures[1].start_pressure is None
        assert parsed.mixtures[1].end_pressure is None

    def test_cylinders_follow_the_order_they_were_breathed_in(self):
        """Switch order is chronological, so the back gas is first and deco gases follow -
        matching how the form names the rows (`getDefaultMixtureName`)."""
        parsed = SuuntoJsonParser.parse(OCEAN_JSON_MULTI_GAS)

        assert [mixture.start_pressure is not None for mixture in parsed.mixtures] == [True, False]

    def test_a_broken_sample_stream_does_not_fail_the_import(self):
        """Cylinder reconstruction is best-effort enrichment, not part of the contract.

        It runs for *every* export with no `Gases` block - including ones that never had
        cylinder data - and walks a barely-documented sample stream, so a structural
        surprise there used to turn a previously fine import into a 422. The header fields
        are what the diver came for and must survive.
        """
        odd_samples = [
            # A naive `Header.DateTime` against offset-aware sample timestamps: comparing
            # them raises, and it did so before any cylinder was even inspected.
            json.dumps(
                {
                    "DeviceLog": {
                        "Header": {"DateTime": "2026-04-03T12:04:11.390", "DiveTime": 300, "Depth": {"Max": 21.1}},
                        "Samples": [{"TimeISO8601": "2026-04-03T12:04:12.390+02:00", "Depth": 5.0}],
                    }
                }
            ).encode(),
            # A cylinder reading with no gas number to attach it to.
            json.dumps(
                {
                    "DeviceLog": {
                        "Header": {"DateTime": "2026-04-03T12:04:11.390+02:00", "Depth": {"Max": 21.1}},
                        "Samples": [
                            {"TimeISO8601": "2026-04-03T12:04:12.390+02:00", "Cylinders": [{"Pressure": 20000000}]}
                        ],
                    }
                }
            ).encode(),
        ]

        for content in odd_samples:
            parsed = SuuntoJsonParser.parse(content)
            assert parsed.max_depth == 21.1
            assert parsed.mixtures == []

    @pytest.mark.parametrize(
        ("label", "dive_time", "pressure"),
        [
            # `json.loads` accepts bare `Infinity`, which reaches `Decimal(...).quantize`
            # in `_round2_or_none` and raises `decimal.InvalidOperation`.
            ("an infinite cylinder pressure", "1800", "Infinity"),
            # Finite, so `round()` outside the guard survives - but `timedelta(seconds=...)`
            # inside it raises `OverflowError`. Both are `ArithmeticError` subclasses, and
            # neither was in the guard's original tuple.
            ("an overflowing DiveTime", "1e300", "20000000"),
        ],
    )
    def test_arithmetic_in_a_bad_sample_does_not_fail_the_import(self, label, dive_time, pressure):
        """The guard promises "best-effort enrichment", and a narrower tuple let that lapse.

        `fit.py` had already widened its equivalent to include `ArithmeticError` after a
        corrupt float32 arrived as NaN; this one kept the old tuple while running the same
        `Decimal` arithmetic. The header fields are what the diver came for and have
        nothing to do with the samples.
        """
        content = json.dumps(
            {
                "DeviceLog": {
                    "Header": {
                        "DateTime": "2026-04-03T12:04:11.390+02:00",
                        "Depth": {"Max": 30.0},
                    },
                    "Samples": [
                        {
                            "TimeISO8601": "2026-04-03T12:04:12.390+02:00",
                            "Cylinders": [{"GasNumber": 0, "Pressure": 1}],
                        }
                    ],
                }
            }
        )
        # Substituted rather than passed through `json.dumps`, which refuses to emit a
        # bare `Infinity` and would render 1e300 as a float literal.
        content = content.replace('"Pressure": 1', f'"Pressure": {pressure}').replace(
            '"Depth": {"Max": 30.0}', f'"DiveTime": {dive_time}, "Depth": {{"Max": 30.0}}'
        )

        parsed = SuuntoJsonParser.parse(content.encode())

        assert parsed.max_depth == 30.0
        assert parsed.mixtures == []

    def test_caps_the_number_of_reconstructed_cylinders(self):
        """Each becomes a `DiveMixtureSchema` in the `/dive/parse` response, and a
        `GasSwitch` event is a few dozen bytes - so without a cap a file under the upload
        limit returned tens of thousands of mixtures. Membership is set-based for the same
        reason: `not in` against a growing list ran once per sample and made this
        quadratic.
        """
        samples = [
            {
                "TimeISO8601": "2026-04-03T12:04:11.390+02:00",
                "DiveEvents": {"GasSwitch": {"GasNumber": number}},
            }
            for number in range(200)
        ]
        content = json.dumps(
            {
                "DeviceLog": {
                    "Header": {"DateTime": "2026-04-03T12:04:11.390+02:00", "DiveTime": 300},
                    "Samples": samples,
                }
            }
        ).encode()

        assert len(SuuntoJsonParser.parse(content).mixtures) == JSON_MAX_CYLINDERS

    def test_ignores_transmitter_readings_from_after_the_dive(self):
        """The computer keeps logging on the boat, where the diver purges the regulator.

        Two dives in the corpus end that way, and taking the file's final reading gave
        them an end pressure of 0.14 bar instead of 53 and 76 - which `compute_gas_use`
        turns into a diver who breathed their cylinder dry.
        """
        parsed = SuuntoJsonParser.parse(OCEAN_JSON_WITH_POST_DIVE_PURGE)
        mixture = parsed.mixtures[0]

        assert mixture.start_pressure == 204.69
        # 53.34 bar at the end of the dive, not the 0.12 bar the purged hose reported
        # a minute after it.
        assert mixture.end_pressure == 53.34

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

    def test_a_developer_field_never_stands_in_for_a_missing_native_one(self):
        """The case that actually separates `_native_value` from `fitdecode.get_value`.

        A valid FIT file cannot order a developer field ahead of a native one - the
        definition record carries native field definitions first and developer ones after,
        so `get_value`'s "first match by position" is always the native field when both
        exist, and the test above passes either way. What `get_value` gets wrong is a
        message carrying *only* the developer duplicate: it hands back a vendor's float32
        as though it were the profile's scaled `uint32`, units, semantics and all.

        Here the session declares Suunto's `float32` `max_depth` and no native one, so the
        honest answer is that this file records no max depth - not 32.40999984741211.
        """
        content = fit_file(
            message(
                "session",
                DevField(name="max_depth", value=32.41, field_number=5, units="m"),
                sport="diving",
                start_time=DIVE_START,
                total_elapsed_time=2001.0,
            ),
        )

        assert FitParser.parse(content).max_depth is None

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

    def test_fills_a_summary_s_missing_end_pressure_from_the_telemetry(self):
        """The realistic dropout: a pod that stops reporting near the end writes a summary
        with a start pressure and no end.

        Falling back per *branch* - "any summary at all beats the telemetry" - keyed off
        the frame existing rather than carrying numbers, so that null won and the last real
        reading was discarded. It is the reading the whole SAC/RMV turns on, and dropout is
        routine: `suunto_xml.py` records 224 of 441 samples missing it in the corpus.
        """
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("tank_update", timestamp=DIVE_START, sensor=2411100050, pressure=207.0),
            message("tank_update", timestamp=DIVE_START + timedelta(seconds=1800), sensor=2411100050, pressure=62.0),
            message("tank_summary", sensor=2411100050, start_pressure=207.0),
        )
        mixture = FitParser.parse(content).mixtures[0]

        assert mixture.start_pressure == 207.0
        assert mixture.end_pressure == 62.0

    def test_a_summary_with_no_pressures_does_not_shadow_the_telemetry(self):
        """A `tank_summary` carrying only `volume_used` says nothing about pressure. The
        join is by the pod's ANT `sensor` id, which both messages carry, so it is exact."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("tank_update", timestamp=DIVE_START, sensor=2411100050, pressure=207.0),
            message("tank_update", timestamp=DIVE_START + timedelta(seconds=1800), sensor=2411100050, pressure=62.0),
            message("tank_summary", sensor=2411100050, volume_used=1500.0),
        )
        mixture = FitParser.parse(content).mixtures[0]

        assert (mixture.start_pressure, mixture.end_pressure) == (207.0, 62.0)

    def test_reads_a_tank_summary_written_after_the_session(self):
        """`tank_summary` summarizes the dive rather than sampling it, so a device may
        write it after the `session` - exactly as `dive_summary` is.

        It sat below `_collect`'s first-session cut and was dropped, taking both
        pressures with it and costing the dive its SAC/RMV. Every fixture missed this
        because `dive_fit_file` appends the session last, so they all placed the summary
        before it - and the corpus can't rule it out either, since no file in it has any
        tank telemetry at all.
        """
        content = fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=1800.0),
            message("tank_summary", sensor=2411100050, start_pressure=207.0, end_pressure=62.0),
        )
        mixture = FitParser.parse(content).mixtures[0]

        assert (mixture.start_pressure, mixture.end_pressure) == (207.0, 62.0)

    def test_caps_the_number_of_cylinders(self):
        """Every distinct ANT id becomes a mixture in the parse response and a channel in
        the stored profile, and nothing else bounded how many there could be: a 1 MB file
        of `tank_update` records with unique sensors produced 99 000 mixtures and a 9.9 MB
        response. No device pairs more than a handful of pods."""
        content = dive_fit_file(
            *(
                message("tank_update", timestamp=DIVE_START, sensor=sensor, pressure=200.0)
                for sensor in range(_MAX_CYLINDERS + 20)
            ),
        )

        profile = FitParser.parse_profile(content)

        assert profile is not None
        assert len(profile.pressure) == _MAX_CYLINDERS

    def test_the_form_and_the_chart_number_cylinders_the_same_way(self):
        """A pod's position must mean the same thing on the dive form and the chart.

        Mixtures were ordered summaries-first while profile channels were numbered by the
        order pods started streaming, so a device enumerating its summaries in a different
        order than its telemetry arrived made "Gas 1" on the chart and the first cylinder
        on the form describe different tanks.
        """
        content = fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("dive_gas", message_index=1, oxygen_content=50, helium_content=0, status="enabled"),
            # Pod 111 streams first...
            message("tank_update", timestamp=DIVE_START, sensor=111, pressure=207.0),
            message("tank_update", timestamp=DIVE_START + timedelta(seconds=10), sensor=222, pressure=150.0),
            # ...but the summaries are written the other way round.
            message("tank_summary", sensor=222, start_pressure=150.0, end_pressure=90.0),
            message("tank_summary", sensor=111, start_pressure=207.0, end_pressure=62.0),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=1800.0),
        )

        mixtures = FitParser.parse(content).mixtures
        profile = FitParser.parse_profile(content)

        assert profile is not None
        # Cylinder 1 is pod 222 on both: 150 bar on the form, 1500 (tenths) on the chart.
        assert [mixture.start_pressure for mixture in mixtures] == [150.0, 207.0]
        assert [(channel.gas_number, channel.v[0]) for channel in profile.pressure] == [(1, 1500), (2, 2070)]

    def test_a_named_pod_that_reported_nothing_is_not_a_cylinder(self):
        """Naming a pod isn't on its own evidence of a cylinder.

        A summary carrying a `sensor` and no pressures, with no telemetry from that pod to
        merge in, produced an entirely null mixture - a phantom empty cylinder row in the
        dive form for a file that recorded no cylinder data at all. The sensor-less branch
        already refused exactly this.
        """
        content = fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("tank_summary", sensor=2411100050, volume_used=1500.0),
            message("session", sport="diving", start_time=DIVE_START, max_depth=30.0),
        )

        assert FitParser.parse(content).mixtures == []

    def test_ignores_a_summary_that_describes_nothing(self):
        """No sensor to join on and no pressures to contribute - counting it as a cylinder
        would push the tank count past the gas list and null out the pod that did report."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("tank_update", timestamp=DIVE_START, sensor=2411100050, pressure=207.0),
            message("tank_update", timestamp=DIVE_START + timedelta(seconds=1800), sensor=2411100050, pressure=62.0),
            message("tank_summary", volume_used=1500.0),
        )
        mixture = FitParser.parse(content).mixtures[0]

        assert (mixture.start_pressure, mixture.end_pressure) == (207.0, 62.0)

    def test_keeps_tank_pressures_when_the_file_has_no_gas_list(self):
        """A Descent dive logged in gauge mode writes no `dive_gas`, and a paired pod
        still reports throughout. Building mixtures only from `dive_gas` left nothing to
        hang the pressures on and discarded every reading - the opposite of the rule the
        JSON parser follows, where evidence of a tank is evidence of a tank."""
        content = dive_fit_file(
            message("tank_summary", sensor=2411100050, start_pressure=207.0, end_pressure=62.0),
            message("tank_update", timestamp=DIVE_START, sensor=2411100050, pressure=207.0),
        )
        mixtures = FitParser.parse(content).mixtures

        assert len(mixtures) == 1
        assert (mixtures[0].start_pressure, mixtures[0].end_pressure) == (207.0, 62.0)
        # Nothing recorded the gas, so nothing is claimed about it.
        assert mixtures[0].oxygen is None
        assert mixtures[0].helium is None

    def test_ignores_a_backup_only_cylinder(self):
        """`dive_gas_status` is `{disabled, enabled, backup_only}`, and a `backup_only`
        cylinder is by definition one that was carried and not breathed. Importing a pony
        bottle as a second mixture costs the dive its SAC/RMV, since `compute_gas_use`
        requires exactly one."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("dive_gas", message_index=1, oxygen_content=21, helium_content=0, status="backup_only"),
        )

        assert len(FitParser.parse(content).mixtures) == 1

    def test_keeps_an_unindexed_gas_apart_from_message_index_zero(self):
        """A positional fallback must not share a key space with a real `message_index`.

        Keying both into one dict made a gas at position 0 collide with a gas declaring
        `message_index=0`, so one of the two silently vanished - here the 50 % deco gas -
        and sorted positions and indices together as if they were on one scale.
        """
        content = dive_fit_file(
            message("dive_gas", oxygen_content=21, helium_content=0, status="enabled"),
            message("dive_gas", message_index=0, oxygen_content=50, helium_content=0, status="enabled"),
        )

        assert sorted(mixture.oxygen for mixture in FitParser.parse(content).mixtures) == [21.0, 50.0]

    def test_a_bare_repeat_cannot_wipe_a_real_summary(self):
        """Duplicates merge per field rather than the last frame winning outright.

        Overwriting made the dedup order-dependent in exactly the way its own docstring
        says it exists to prevent: a `volume_used`-only repeat *after* a real summary wiped
        a genuine 207 -> 62 bar, while the same two frames the other way round kept it.
        The existing dedup test gives both duplicates identical pressures, so it cannot see
        the asymmetry.
        """
        real = message("tank_summary", sensor=2411100050, start_pressure=207.0, end_pressure=62.0)
        bare = message("tank_summary", sensor=2411100050, volume_used=1500.0)

        for order in ([real, bare], [bare, real]):
            content = fit_file(
                message("file_id", type="activity", manufacturer="garmin"),
                message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
                *order,
                message("session", sport="diving", start_time=DIVE_START),
            )
            mixture = FitParser.parse(content).mixtures[0]

            assert (mixture.start_pressure, mixture.end_pressure) == (207.0, 62.0)

    def test_caps_the_number_of_gases(self):
        """`dive_gas` is the primary mixture source and was the one list `_MAX_CYLINDERS`
        didn't bound. Its payload is two bytes, so `_MAX_FRAMES` alone let a 220 KB file
        return 20 000 mixtures - a larger amplification through the same response field
        than the `tank_update` case the cap was introduced for."""
        content = dive_fit_file(
            *(message("dive_gas", oxygen_content=21) for _ in range(_MAX_CYLINDERS + 500)),
        )

        assert len(FitParser.parse(content).mixtures) == _MAX_CYLINDERS

    def test_dedupes_repeated_tank_summaries_for_one_pod(self):
        """A device that writes the summary twice for one transmitter would otherwise
        count as two cylinders, and the exact-count pairing rule then throws away every
        pressure in the file."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("tank_summary", sensor=2411100050, start_pressure=207.0, end_pressure=62.0),
            message("tank_summary", sensor=2411100050, start_pressure=207.0, end_pressure=62.0),
        )
        mixture = FitParser.parse(content).mixtures[0]

        assert (mixture.start_pressure, mixture.end_pressure) == (207.0, 62.0)

    def test_still_pairs_two_pods_to_two_gases(self):
        """The dedupe must not collapse genuinely different transmitters."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            message("dive_gas", message_index=1, oxygen_content=50, helium_content=0, status="enabled"),
            message("tank_summary", sensor=111, start_pressure=207.0, end_pressure=62.0),
            message("tank_summary", sensor=222, start_pressure=180.0, end_pressure=90.0),
        )
        mixtures = FitParser.parse(content).mixtures

        assert [(m.oxygen, m.start_pressure) for m in mixtures] == [(21.0, 207.0), (50.0, 180.0)]

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

    @pytest.mark.parametrize("entry_point", [FitParser.parse, FitParser.parse_profile])
    def test_refuses_a_file_with_more_records_than_any_dive(self, entry_point):
        """Decoding is linear in frames, and the file size cap alone doesn't bound it.

        A device writes one definition record and then a long run of 10-byte `record`
        messages, so a file at `MAX_DIVE_FILE_SIZE` holds ~524 000 of them and takes ~10 s
        to decode - paid twice per import, since `/dive/parse` and `PUT /dive/{uuid}/file`
        each read the file. Capping *collected samples* would not have helped: bare
        decoding is 8 s of that 10 s, and the collection is under 1 s.

        Refused rather than truncated, because a FIT file's `session` is written after the
        samples it summarizes - keeping the first 100 000 frames would discard the start
        time, duration and depths, and import a confidently empty dive.
        """
        with pytest.raises(DiveParseError, match="more than"):
            entry_point(dense_record_stream(MAX_FRAMES + 1))

    def test_accepts_a_file_right_up_to_the_cap(self):
        """The cap is ~23x the largest real file in the corpus (a 72-minute multi-channel
        Suunto Ocean dive at 4 339 frames), so it must not be anywhere near a real dive."""
        # A little under the cap rather than exactly at it: the budget also covers the two
        # definition records, the `file_id` data record, and the header and CRC frames
        # `fitdecode` emits, so exact arithmetic here would pin an encoding detail rather
        # than the behaviour.
        records = MAX_FRAMES - 100
        profile = FitParser.parse_profile(dense_record_stream(records))

        assert profile is not None
        assert profile.depth is not None
        assert len(profile.depth.t) == records

    # `fitdecode` warns its way through a corrupt file ("invalid field size 77 ...") before
    # deciding it cannot continue. That is the library working as intended on garbage, and
    # 500-odd of them would drown the suite's warning summary.
    @pytest.mark.filterwarnings("ignore::UserWarning")
    def test_a_message_less_decoder_failure_still_gives_a_reason(self):
        """Flipping byte 143 to 0x00 makes `fitdecode` fail one of the 15 message-less
        `assert`s in its reader, which formats to the empty string.

        The fallback for that was written as `exc or type(exc).__name__` - dead code, since
        `BaseException` defines neither `__bool__` nor `__len__` and an exception instance
        is therefore always truthy. Fuzzing put ~0.4 % of corrupt uploads on a 422 whose
        detail was `Invalid FIT file: ` and nothing else.
        """
        corrupt = bytearray(VALID_FIT)
        corrupt[143] = 0x00

        with pytest.raises(DiveParseError) as raised:
            FitParser.parse(bytes(corrupt))

        assert not str(raised.value).endswith(": ")
        assert "AssertionError" in str(raised.value)

    @pytest.mark.parametrize("entry_point", [FitParser.parse, FitParser.parse_profile])
    def test_raises_dive_parse_error_on_corrupt_but_untruncated_files(self, entry_point):
        """Corruption in the body, which is not the same failure as truncation.

        The truncated-file test above passes against a catch of `fitdecode.FitError`
        alone, because truncation happens to raise `FitEOFError` - so it gave false
        confidence. Flipping bytes past the header does not: within 400 mutations this
        found `AssertionError` from `reader.py`, `ValueError: size` from a bad field
        definition, and `TypeError: '>=' not supported between instances of 'tuple' and
        'int'` from `processors.py`, each of which escaped `parse()` and became a 500
        from `POST /dive/parse`, which handles only the two parser errors.

        Fixed seed, so a failure here is reproducible rather than a flake.
        """
        rng = random.Random(7)
        for _ in range(400):
            corrupt = bytearray(VALID_FIT)
            for _ in range(rng.randint(1, 4)):
                # Past the 12-byte header, so `can_parse`'s magic check still matches and
                # the bytes reach the decoder rather than being rejected as "not FIT".
                corrupt[rng.randrange(12, len(corrupt))] = rng.randrange(256)
            try:
                entry_point(bytes(corrupt))
            except DiveParseError:
                pass
            except Exception as exc:  # noqa: BLE001 - the point of the test
                pytest.fail(f"{type(exc).__name__} escaped as a 500 instead of DiveParseError: {exc}")


class TestFitBitfieldFields:
    """`message_index` and `sensor` are identities and bitfields, not readings.

    `fitdecode` renders a field whose profile type carries an enum by exact value match,
    and FIT keeps bitfield masks in that same enum slot: `message_index` maps
    `{4095: 'mask', 28672: 'reserved', 32768: 'selected'}`, and `ant_channel_id` - the type
    behind `sensor` - maps `{65535: 'ant_device_number', ...}`. A gas index or ANT id that
    lands on one of those comes back as a **string**, and `int()` on it raises.
    """

    @staticmethod
    def _gas(index: int, oxygen: int):
        return message("dive_gas", message_index=index, oxygen_content=oxygen, helium_content=0, status="enabled")

    def test_a_gas_index_carrying_the_selected_bit_is_importable(self):
        """`message_index = 0x8000` is gas index 0 with the spec's "selected" flag set -
        an ordinary thing to write about the first configured gas, not corruption. It made
        the whole dive un-importable with a 422."""
        parsed = FitParser.parse(dive_fit_file(self._gas(0x8000, 21)))

        assert [mixture.oxygen for mixture in parsed.mixtures] == [21.0]

    def test_flagged_and_unflagged_gas_indices_sort_together(self):
        """Without the `0x0FFF` mask a flagged index is a five-digit number: gas 1 arrives
        as 32769 and sorts *after* gas 2, reordering the mixtures and so misaligning
        `_tanks_for`'s positional pairing."""
        content = dive_fit_file(self._gas(0, 21), self._gas(0x8000 | 1, 50), self._gas(2, 80))

        assert [mixture.oxygen for mixture in FitParser.parse(content).mixtures] == [21.0, 50.0, 80.0]

    def test_a_tank_summary_sensor_on_an_enum_boundary_is_read(self):
        content = dive_fit_file(
            self._gas(0, 21),
            message("tank_summary", sensor=65535, start_pressure=207.0, end_pressure=62.0),
        )
        mixture = FitParser.parse(content).mixtures[0]

        assert (mixture.start_pressure, mixture.end_pressure) == (207.0, 62.0)

    def test_a_tank_update_sensor_on_an_enum_boundary_is_read(self):
        content = dive_fit_file(
            self._gas(0, 21),
            message("tank_update", timestamp=DIVE_START, sensor=65535, pressure=207.0),
            message("tank_update", timestamp=DIVE_START + timedelta(seconds=1800), sensor=65535, pressure=62.0),
        )
        profile = FitParser.parse_profile(content)

        assert profile is not None
        assert len(profile.pressure) == 1


class TestFitParserMultiSession:
    """A file holding more than one dive is described by the first, samples included.

    `_collect` already took that position for `session`, but collected `record`,
    `tank_update` and `dive_gas` from every dive in the file - so the dive came from
    session 1 while its profile spanned the lot, and `DiveProfileInfo.duration_seconds`
    disagreed with the dive's own `duration`.
    """

    @staticmethod
    def _two_dives() -> bytes:
        def record(offset: int, depth: float):
            return message("record", timestamp=DIVE_START + timedelta(seconds=offset), depth=depth, temperature=22)

        return fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("dive_gas", message_index=0, oxygen_content=21, helium_content=0, status="enabled"),
            record(0, 1.0),
            record(60, 30.0),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=1800.0, max_depth=30.0),
            # Everything below belongs to the second dive.
            message("dive_gas", message_index=0, oxygen_content=32, helium_content=0, status="enabled"),
            record(7200, 1.0),
            record(7260, 18.0),
            message(
                "session",
                sport="diving",
                start_time=DIVE_START + timedelta(seconds=7200),
                total_elapsed_time=1500.0,
                max_depth=18.0,
            ),
        )

    def test_the_profile_stops_at_the_first_session(self):
        profile = FitParser.parse_profile(self._two_dives())

        assert profile is not None
        assert profile.depth is not None
        # Not [0, 60, 7200, 7260]: a two-hour span with a surface interval in the middle,
        # against a dive that says it lasted 1 800 seconds.
        assert profile.depth.t == [0.0, 60.0]
        assert profile.depth.v == [100, 3000]

    def test_the_dive_and_its_profile_agree(self):
        content = self._two_dives()

        parsed = FitParser.parse(content)
        profile = FitParser.parse_profile(content)

        assert parsed.duration == 1800
        assert parsed.max_depth == 30.0
        assert profile is not None and profile.depth is not None
        assert max(profile.depth.v) == 3000  # 30.00 m, the first dive's depth

    def test_gas_from_a_later_dive_is_not_imported(self):
        """`dive_gas` carries no timestamp, which is why the cut is positional rather than
        by the session's time window."""
        parsed = FitParser.parse(self._two_dives())

        assert [mixture.oxygen for mixture in parsed.mixtures] == [21.0]


class TestFitParserDiveSummary:
    def test_prefers_the_session_level_summary(self):
        """A Garmin freediving activity writes a `dive_summary` per individual descent
        plus a session-level one, and `reference_mesg` names which message each refers to.
        Taking the first would read one descent's depth as the whole dive's."""
        content = fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=3600.0),
            message("dive_summary", reference_mesg="lap", reference_index=0, max_depth=12.0, bottom_time=45.0),
            message("dive_summary", reference_mesg="session", reference_index=0, max_depth=31.0, bottom_time=2400.0),
        )

        assert FitParser.parse(content).max_depth == 31.0

    def test_a_later_dive_s_summary_cannot_describe_the_first(self):
        """`dive_summary` is exempt from the first-session cut - it is written *after* the
        session it refers to, so gating it there would discard every one - but the
        exemption was unbounded.

        `_dive_summary` prefers a summary whose `reference_mesg` names a session, so a file
        where dive 1's omits that field and dive 2's carries it handed dive 2's depth and
        bottom time to dive 1. `TestFitParserMultiSession` establishes "the first dive,
        samples included" as an invariant, and this was the one message class escaping it.
        """
        content = fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("session", sport="diving", start_time=DIVE_START, max_depth=30.0),
            message("dive_summary", max_depth=30.0, bottom_time=1700.0),
            message(
                "session",
                sport="diving",
                start_time=DIVE_START + timedelta(seconds=7200),
                max_depth=18.0,
            ),
            message("dive_summary", reference_mesg="session", max_depth=18.0, bottom_time=1400.0),
        )

        parsed = FitParser.parse(content)

        assert parsed.max_depth == 30.0
        assert parsed.duration == 1700

    def test_falls_back_to_the_only_summary_when_none_names_a_session(self):
        """A single-dive export commonly writes one with no `reference_mesg` at all."""
        content = fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=3600.0),
            message("dive_summary", max_depth=27.5),
        )

        assert FitParser.parse(content).max_depth == 27.5


class TestTechScalars:
    """CNS/OTU/surface pressure and the per-mixture ppO2, role and gas number.

    The units are the whole story here - each format is internally consistent and
    plausible on its own, and only a dive exported two ways shows that they disagree.
    Every figure below is taken from a real cross-format pair in the corpus; see
    DECISIONS.md for the table.
    """

    def test_xml_reads_cns_as_percent_and_surface_pressure_as_pascal(self):
        """105700 is 1.057 bar, not 105.7. `SurfacePressure` is the one pressure in a
        DM5 export that isn't millibar - read as millibar it would put a hundred metres
        of seawater above a diver standing on a boat."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <CnsStart>0</CnsStart>
  <CnsEnd>20</CnsEnd>
  <OtuStart>0</OtuStart>
  <OtuEnd>53</OtuEnd>
  <SurfacePressure>105700</SurfacePressure>
</Dive>
""".encode()

        parsed = SuuntoXmlParser.parse(content)

        assert (parsed.cns_start, parsed.cns_end) == (0.0, 20.0)
        assert (parsed.otu_start, parsed.otu_end) == (0.0, 53.0)
        assert parsed.surface_pressure_bar == 1.057

    def test_json_reads_cns_as_a_fraction_of_the_same_number(self):
        """The same dive: `<CnsEnd>7</CnsEnd>` in the XML export is `EndTissue.CNS:
        0.069` in the JSON one. Unconverted, a 69 % oxygen clock would read 0.069 %."""
        content = json.dumps(
            {
                "DeviceLog": {
                    "Header": {
                        "Diving": {
                            "SurfacePressure": 106100,
                            "StartTissue": {"CNS": 0, "OTU": 0},
                            "EndTissue": {"CNS": 0.069, "OTU": 17.89002799987793},
                        }
                    }
                }
            }
        ).encode()

        parsed = SuuntoJsonParser.parse(content)

        assert parsed.cns_start == 0.0
        assert parsed.cns_end == 6.9
        # OTU needs no conversion - it is the same absolute count in both formats - but
        # it is rounded, since no computer accounts exposure to a float32's last digit.
        assert parsed.otu_end == 17.89
        assert parsed.surface_pressure_bar == 1.061

    def test_fit_reads_o2_toxicity_as_the_ending_total_not_the_dive_s_share(self):
        """The 2025-03-06 08:29 dive exists as both a FIT and an XML export. The XML
        records `OtuStart 22 -> OtuEnd 23`; the FIT writes `o2_toxicity = 23`. Read as
        the dive's own increment it would have been 1, and every repetitive dive's OTU
        would be understated by its own history."""
        content = dive_fit_file(end_cns=9, o2_toxicity=23)

        parsed = FitParser.parse(content)

        assert parsed.cns_end == 9.0
        assert parsed.otu_end == 23.0

    def test_fit_leaves_otu_start_and_surface_pressure_null(self):
        """Neither exists in the FIT profile. `record.absolute_pressure` is the ambient
        pressure per sample, so the nearest stand-in would be a guess about when the
        diver got in the water."""
        parsed = FitParser.parse(dive_fit_file(end_cns=9, o2_toxicity=23))

        assert parsed.otu_start is None
        assert parsed.surface_pressure_bar is None

    def test_an_out_of_band_surface_pressure_reads_as_no_reading(self):
        """Unattested in the corpus - all 384 XML exports land in 1.031-1.067 bar - but
        the band is the one `ck_dive_surface_pressure_range` enforces, and the column is
        written inside `store_dive_file`'s transaction. A value the CHECK rejects would
        therefore fail the *attach* of an otherwise importable file, reported to the diver
        as a concurrent-upload conflict that no retry can clear. Nulled here instead."""
        for reading in ("0", "105700000", "-105700"):
            content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <SurfacePressure>{reading}</SurfacePressure>
</Dive>
""".encode()

            assert SuuntoXmlParser.parse(content).surface_pressure_bar is None, reading

    def test_the_bounds_are_the_ones_the_database_enforces(self):
        """Not a looser sanity check that happens to sit inside the CHECK: the point is
        that nothing can reach the column having passed a weaker test than the column's."""
        constraint = next(
            c for c in Dive.__table__.constraints if getattr(c, "name", None) == "ck_dive_surface_pressure_range"
        )
        sqltext = str(constraint.sqltext)

        def parsed_with(pressure: float) -> float | None:
            return ParsedDiveSchema(
                avg_depth=None,
                bottom_temperature=None,
                dive_number=None,
                duration=None,
                max_depth=None,
                start_time=None,
                mixtures=[],
                surface_pressure_bar=pressure,
            ).surface_pressure_bar

        assert "0.5" in sqltext and "1.2" in sqltext
        # The bounds themselves are inclusive on both sides, as the CHECK's `>=`/`<=` are.
        assert parsed_with(0.5) == 0.5
        assert parsed_with(1.2) == 1.2
        assert parsed_with(0.49) is None
        assert parsed_with(1.21) is None

    def test_fit_prefers_the_dive_summary_over_the_session(self):
        """The mirror of `_depth`'s preference, reversed on purpose: on a multi-dive file
        the session totals cover the whole activity, while `_dive_summary` has already
        picked the summary describing the dive being imported."""
        content = fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=3600.0, end_cns=40),
            message("dive_summary", reference_mesg="session", end_cns=12, o2_toxicity=29),
        )

        parsed = FitParser.parse(content)

        assert parsed.cns_end == 12.0
        assert parsed.otu_end == 29.0

    def test_xml_reads_po2_per_mixture_in_bar(self):
        """`<PO2>` is the only field separating the two cylinders of the corpus's one
        two-gas dive: 1.4 on the 21/0 back gas, 1.6 on the 49/0 deco bottle."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures>
    <DiveMixture><Oxygen>21</Oxygen><Helium>0</Helium><PO2>1.4</PO2><Size>22</Size><Type>1</Type></DiveMixture>
    <DiveMixture><Oxygen>49</Oxygen><Helium>0</Helium><PO2>1.6</PO2><Size>11</Size><Type>1</Type></DiveMixture>
  </DiveMixtures>
</Dive>
""".encode()

        mixtures = SuuntoXmlParser.parse(content).mixtures

        assert [m.po2_limit for m in mixtures] == [1.4, 1.6]
        assert [m.gas_number for m in mixtures] == [1, 2]

    def test_xml_never_reads_type_as_a_role(self):
        """`<Type>` is 1 for all 353 mixtures in the corpus *including both cylinders of
        the two-gas dive*, where one is a 21/0 back gas and the other a 49/0 deco bottle.
        Whatever it encodes it is not what the cylinder was carried for, and mapping it
        would confidently label a deco bottle "bottom"."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures>
    <DiveMixture><Oxygen>21</Oxygen><Type>1</Type></DiveMixture>
    <DiveMixture><Oxygen>49</Oxygen><Type>1</Type></DiveMixture>
  </DiveMixtures>
</Dive>
""".encode()

        assert [m.role for m in SuuntoXmlParser.parse(content).mixtures] == [None, None]

    def test_json_reads_po2_in_pascal_and_maps_state_to_a_role(self):
        mixtures = SuuntoJsonParser.parse(VALID_SUUNTO_JSON_WITH_GASES.encode()).mixtures

        assert [m.po2_limit for m in mixtures] == [1.4, 1.6]
        assert [m.role for m in mixtures] == [GasRole.BOTTOM, GasRole.BOTTOM]
        assert [m.gas_number for m in mixtures] == [1, 2]

    def test_json_leaves_an_unrecognized_state_null_rather_than_guessing(self):
        """The vocabulary is Suunto's, so a value the table doesn't know has to come out
        `None` - not raise, and not become a role we picked."""
        content = json.dumps(
            {"DeviceLog": {"Header": {"Diving": {"Gases": [{"Oxygen": 0.21, "State": "Something New"}]}}}}
        ).encode()

        assert SuuntoJsonParser.parse(content).mixtures[0].role is None

    def test_ocean_json_keeps_the_gas_number_the_file_states(self):
        """The one export shape that numbers its own cylinders. These are the numbers the
        pressure channels are labelled with, so a mixture row and its curve on the chart
        name the same tank."""
        samples = [
            {"TimeISO8601": "2026-04-17T11:49:23+02:00", "DiveEvents": [{"GasSwitch": {"GasNumber": 3}}]},
            {"TimeISO8601": "2026-04-17T11:49:24+02:00", "Cylinders": [{"GasNumber": 3, "Pressure": 20000000}]},
        ]

        mixtures = SuuntoJsonParser.parse(_ocean_json(samples)).mixtures

        assert [m.gas_number for m in mixtures] == [3]

    def test_fit_reads_a_closed_circuit_diluent_as_one(self):
        """`dive_gas.mode` is the only field on the message that speaks to role, and it
        answers half the question: a diluent identifies itself."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, status="enabled", mode="closed_circuit_diluent", oxygen_content=21)
        )

        assert FitParser.parse(content).mixtures[0].role == GasRole.DILUENT

    def test_fit_reads_open_circuit_as_no_role_at_all(self):
        """`open_circuit` covers a back gas and a stage bottle alike, so it says nothing
        about what the cylinder was carried for."""
        content = dive_fit_file(
            message("dive_gas", message_index=0, status="enabled", mode="open_circuit", oxygen_content=21)
        )

        assert FitParser.parse(content).mixtures[0].role is None

    def test_fit_never_reads_status_as_a_role(self):
        """`status` is whether the gas was breathed - `_breathed_gases` already uses it
        for that - and reading `backup_only` as a role would relabel a pony bottle as
        though the file had described its purpose. (A `backup_only` gas is dropped
        outright, so the surviving mixture is the enabled one, with no role.)"""
        content = dive_fit_file(
            message("dive_gas", message_index=0, status="enabled", oxygen_content=21),
            message("dive_gas", message_index=1, status="backup_only", oxygen_content=32),
        )

        mixtures = FitParser.parse(content).mixtures

        assert [(m.oxygen, m.role) for m in mixtures] == [(21.0, None)]

    def test_fit_numbers_mixtures_from_one(self):
        content = dive_fit_file(
            message("dive_gas", message_index=0, status="enabled", oxygen_content=21),
            message("dive_gas", message_index=1, status="enabled", oxygen_content=50),
        )

        assert [m.gas_number for m in FitParser.parse(content).mixtures] == [1, 2]


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

    def test_xml_leaves_omitted_tech_fields_null(self):
        """Every pre-2013 export in the corpus predates half of these. A dive with no
        `<CnsEnd>` has not recorded a CNS of 0 - it recorded nothing."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures><DiveMixture><Oxygen>21</Oxygen></DiveMixture></DiveMixtures>
</Dive>
""".encode()

        parsed = SuuntoXmlParser.parse(content)

        assert (parsed.cns_start, parsed.cns_end) == (None, None)
        assert (parsed.otu_start, parsed.otu_end) == (None, None)
        assert parsed.surface_pressure_bar is None
        assert parsed.mixtures[0].po2_limit is None
        assert parsed.mixtures[0].role is None

    def test_json_leaves_omitted_tech_fields_null(self):
        content = json.dumps({"DeviceLog": {"Header": {"Diving": {"Gases": [{"Oxygen": 0.21}]}}}}).encode()

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.cns_start, parsed.cns_end) == (None, None)
        assert (parsed.otu_start, parsed.otu_end) == (None, None)
        assert parsed.surface_pressure_bar is None
        assert parsed.mixtures[0].po2_limit is None
        assert parsed.mixtures[0].role is None

    def test_fit_leaves_omitted_tech_fields_null(self):
        content = dive_fit_file(message("dive_gas", message_index=0, status="enabled"))

        parsed = FitParser.parse(content)

        assert (parsed.cns_start, parsed.cns_end) == (None, None)
        assert (parsed.otu_start, parsed.otu_end) == (None, None)
        assert parsed.surface_pressure_bar is None
        # `dive_gas` has no ppO2 field at all; `dive_settings.po2_warn` is the device's
        # threshold for the whole dive rather than this cylinder's plan, so reading it
        # here would attribute a global setting to every gas.
        assert parsed.mixtures[0].po2_limit is None

    def test_an_explicitly_recorded_zero_is_still_a_reading(self):
        """The rule is "don't invent", not "treat zero as missing" - a nitrox export
        that records `Helium: 0` has genuinely recorded 0 % helium."""
        content = json.dumps({"DeviceLog": {"Header": {"Diving": {"Gases": [{"Oxygen": 0.32, "Helium": 0}]}}}}).encode()

        mixture = SuuntoJsonParser.parse(content).mixtures[0]

        assert mixture.helium == 0.0
        assert mixture.oxygen == 32.0

    def test_xml_zero_cylinder_pressures_are_not_a_fill(self):
        """The one place a *pressure* zero is the exception to the rule above, and why
        `DiveMixtureSchema` nulls it: DM5 writes `0` for a cylinder with no transmitter,
        where the same dive's JSON omits the keys. The 49 % bottle here is the real one
        from `Dive_2025-06-03-1215.xml`, whose `<TransmitterId>` is nil.

        Both halves in one mixture: the pressures go, `Helium: 0` stays.
        """
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures><DiveMixture>
    <EndPressure>0</EndPressure>
    <Helium>0</Helium>
    <Oxygen>49</Oxygen>
    <Size>11</Size>
    <StartPressure>0</StartPressure>
  </DiveMixture></DiveMixtures>
</Dive>
""".encode()

        mixture = SuuntoXmlParser.parse(content).mixtures[0]

        assert mixture.start_pressure is None
        assert mixture.end_pressure is None
        assert mixture.helium == 0.0
        assert (mixture.oxygen, mixture.volume) == (49.0, 11.0)

    def test_json_zero_cylinder_pressures_are_not_a_fill(self):
        """Unattested in the JSON corpus - which says "no transmitter" by omitting the
        keys - but the rule lives on the schema, so the parsers cannot disagree about the
        same cylinder depending on which export it arrived in."""
        content = json.dumps(
            {"DeviceLog": {"Header": {"Diving": {"Gases": [{"Oxygen": 0.49, "StartPressure": 0, "EndPressure": 0}]}}}}
        ).encode()

        mixture = SuuntoJsonParser.parse(content).mixtures[0]

        assert (mixture.start_pressure, mixture.end_pressure) == (None, None)
        assert mixture.oxygen == 49.0


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
