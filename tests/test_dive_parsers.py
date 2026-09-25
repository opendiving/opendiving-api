"""Unit tests for dive-computer export file parsers."""

import json
import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import CheckConstraint

from src.app.models.dive import Dive
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_recording import DiveRecording
from src.app.schemas.dive import DecoAlgorithm, DiveCreate, DiveMode, Salinity
from src.app.schemas.dive_mixture import GasRole
from src.app.schemas.parsed_dive import (
    LATITUDE_LIMIT,
    LONGITUDE_LIMIT,
    DiveMixtureSchema,
    ParsedDecoModel,
    ParsedDiveResponse,
    ParsedDiveSchema,
)
from src.app.services.dive_parsers import DiveParseError, UnsupportedDiveFileError, parse_dive_file
from src.app.services.dive_parsers.fit import _MAX_CYLINDERS, _MAX_DEVICE_INFO, FitParser
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


def _validated_fields(model: type[BaseModel]) -> set[str]:
    """Field names some `field_validator` on this schema covers.

    Read off Pydantic's own decorator registry rather than listed by hand, so the guard
    in `test_every_bounded_column_this_phase_adds_has_a_parse_side_guard` cannot pass by
    being updated alongside the thing it is checking.
    """
    return {
        field
        for decorator in model.__pydantic_decorators__.field_validators.values()
        for field in decorator.info.fields
    }


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


def _ocean_depth(offset_seconds: int, depth: float) -> dict:
    return {"TimeISO8601": _ocean_time(offset_seconds), "Depth": depth}


def _ocean_fix(offset_seconds: int, latitude: float, longitude: float) -> dict:
    """A GPS sample, in the shape the Ocean writes one: radians, and no other channel.

    The device logs these on samples of their own, sharing nothing with the depth
    readings but the timeline - which is why the extraction walks the stream for both.
    """
    return {
        "TimeISO8601": _ocean_time(offset_seconds),
        "GPSAltitude": -0.2,
        "Latitude": latitude,
        "Longitude": longitude,
    }


def _ocean_origin(offset_seconds: int, latitude: float, longitude: float) -> dict:
    """The first sample's `DiveRouteOrigin`, in the shape the Ocean writes one.

    Note the units: **degrees here, radians in `_ocean_fix`** - one export, two
    conventions. `DiveRouteQuality` rides along because the real files carry it, and
    because the parser deliberately ignores it.
    """
    return {
        "TimeISO8601": _ocean_time(offset_seconds),
        "DiveRouteOrigin": {"Altitude": 6, "Latitude": latitude, "Longitude": longitude},
        "DiveRouteQuality": 0,
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


def _dive_constraints() -> dict[str, str]:
    """`dive`'s named `CheckConstraint`s, by name, as the SQL they carry."""
    return {
        str(constraint.name): str(constraint.sqltext)
        for constraint in Dive.metadata.tables["dive"].constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def _semicircles(degrees: float) -> int:
    """Degrees into the signed 32-bit angle FIT stores, for a fixture that wants a
    coordinate it can read back. The two real corpus values are used raw instead - see
    `TestEntryAndExitPositions`."""
    return round(degrees * 2**31 / 180)


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

    def test_the_computers_own_counter_is_not_the_dives_number(self):
        """`DiveNumberInSerie` is the device's counter, which restarts on a new or
        factory-reset computer - importing it as the dive's would stamp a #5 onto a diver's
        300th dive. The dive's number comes from its date instead
        (`services/dive_numbering.py`). The counter itself is not discarded - it is read
        onto `device.dive_number`, which `TestParsersReportTheDevice` covers."""
        assert "<DiveNumberInSerie>5</DiveNumberInSerie>" in VALID_SUUNTO_XML

        assert SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode()).dive_number is None

    def test_parses_mixtures(self):
        parsed = SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode())

        assert len(parsed.mixtures) == 1
        mixture = parsed.mixtures[0]
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
        which is the order the form labels its rows by position in ("Tank 1", "Tank 2")."""
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

    def test_the_computers_own_counter_is_not_the_dives_number(self):
        """`session.dive_number` counts dives on *that device*, not in the diver's log:
        it restarts at 1 after a factory reset or a new computer. The corpus shows it
        outright - a D5 reporting `dive_number` 5 for a dive the diver labelled "#28". It
        is read onto `device.dive_number` rather than thrown away, which
        `TestParsersReportTheDevice` covers."""
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
        to decode - paid twice per import, since `/dive/parse` and `POST /dive/{uuid}/recordings`
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
    session 1 while its profile spanned the lot, and `DiveProfileInfo.duration`
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


class TestFitSalinity:
    """`dive_settings.water_type` is the only salinity evidence any supported export
    carries, and it is the recording's `salinity` - a setting of the computer, never a
    prefill of the dive's water type.

    The vocabulary is kept verbatim - a computer left on its EN13319 factory calibration
    imports as `en13319`, not as the nearest real water. Folding it into `salt` would be
    the parser substituting a plausible value for what the file said, which is the rule
    `TestParsersInventNothing` exists for.
    """

    @pytest.mark.parametrize(
        ("native", "expected"),
        [
            ("salt", Salinity.SALT),
            ("fresh", Salinity.FRESH),
            ("en13319", Salinity.EN13319),
        ],
    )
    def test_each_named_salinity_survives_the_import(self, native: str, expected: Salinity) -> None:
        content = dive_fit_file(message("dive_settings", water_type=native))

        assert FitParser.parse(content).salinity is expected

    def test_a_custom_density_is_not_a_named_setting(self) -> None:
        """`custom` means the diver dialled in a `water_density` number, which has no
        column here - so the file records no named setting, and that is what `None` says.
        Picking `salt` off a density near 1030 would be inventing the reading."""
        content = dive_fit_file(message("dive_settings", water_type="custom", water_density=1030.0))

        assert FitParser.parse(content).salinity is None

    def test_a_file_with_no_dive_settings_records_nothing(self) -> None:
        assert FitParser.parse(dive_fit_file()).salinity is None

    def test_the_dive_form_is_not_offered_a_water_type(self) -> None:
        """A calibration is not a kind of water, so nothing seeds the form's `water_type`."""
        content = dive_fit_file(message("dive_settings", water_type="en13319"))

        assert "water_type" not in FitParser.parse(content).model_dump()

    def test_dive_settings_written_after_the_session_still_count(self) -> None:
        """The branch sits *above* `_collect`'s first-session cut, which is what makes
        this work. That cut exists for samples - messages belonging to whichever dive they
        follow - and `tank_summary`'s comment records what gating a summary message there
        cost: both cylinder pressures, and the whole SAC/RMV with them."""
        content = fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=1800.0),
            message("dive_settings", water_type="fresh"),
        )

        assert FitParser.parse(content).salinity is Salinity.FRESH

    def test_the_first_setting_wins_on_a_two_dive_file(self) -> None:
        """Same rule as `session`: the first dive is the one the file is about."""
        content = fit_file(
            message("file_id", type="activity", manufacturer="garmin"),
            message("dive_settings", water_type="salt"),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=1800.0),
            message("dive_settings", water_type="fresh"),
            message(
                "session",
                sport="diving",
                start_time=DIVE_START + timedelta(seconds=7200),
                total_elapsed_time=1500.0,
            ),
        )

        assert FitParser.parse(content).salinity is Salinity.SALT

    def test_neither_suunto_export_claims_a_salinity(self) -> None:
        """Neither format carries salinity anywhere, so `None` is the honest answer - and
        `ParsedDiveSchema.salinity`'s default is what supplies it, since both Suunto
        parsers build the schema from explicit keyword arguments."""
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><Duration>1800</Duration></Dive>
""".encode()
        json_content = json.dumps({"DeviceLog": {"Header": {"Duration": 1800}}}).encode()

        assert SuuntoXmlParser.parse(xml).salinity is None
        assert SuuntoJsonParser.parse(json_content).salinity is None


class TestParsersReportTheDevice:
    """What recorded the file - which every export names, and none of them used to report.

    A logbook that can hold two records of one dive has to be able to tell them apart, and
    what distinguishes them is the computer that wrote each. So every parser now returns a
    `ParsedDevice` beside the dive. Nothing here is stored: `POST /dive/parse` returns it,
    and `DiveCreate` is `extra="forbid"`, so a prefilled form cannot hand it back.

    The figures are the corpus's own, and one pair carries the argument. A single dive off
    Dahab exists both as a Suunto Ocean JSON export and as the same computer's FIT export,
    and neither carries what the other does: the JSON has the serial `253810000400` and the
    name its owner set, `Porvoo`, with no model anywhere; the FIT has the model
    `Suunto Ocean` and no serial at all. Reading only one of the two shapes would leave
    half the identity behind.
    """

    def test_xml_reports_the_computer_that_wrote_it(self):
        """`<Source>` is the model and `<Software>` the firmware. The brand is the
        format's rather than a field's - a DM5 export has no element for it, and `can_parse`
        has already required the Suunto namespace."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveNumberInSerie>5</DiveNumberInSerie>
  <SerialNumber>192410004212</SerialNumber>
  <Software>2.5.1947</Software>
  <Source>Suunto D5</Source>
</Dive>
""".encode()

        device = SuuntoXmlParser.parse(content).device

        assert device is not None
        assert (device.brand, device.model) == ("Suunto", "Suunto D5")
        assert (device.serial, device.firmware) == ("192410004212", "2.5.1947")
        assert device.dive_number == 5
        # The format records no name its owner chose, unlike the JSON export's
        # `Header.Device.Name`. Filling it from `<Source>` would invent one.
        assert device.name is None

    def test_the_xml_counter_lands_on_the_device_and_not_on_the_dive(self):
        """`<DiveNumberInSerie>` restarts at 1 on a factory-reset computer, so it was never
        the diver's number and is still not written as one. What changed is that it is no
        longer thrown away."""
        parsed = SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode())

        assert parsed.dive_number is None
        assert parsed.device is not None
        assert parsed.device.dive_number == 5

    def test_json_reports_the_computer_that_wrote_it(self):
        """`Name` is the device's own name, not its model: this Ocean's owner called it
        `Porvoo`. `Info.SW` is the firmware - `Info.HW` beside it is a board revision and
        `Info.BSL` a bootloader, and neither of those is one."""
        content = json.dumps(
            {
                "DeviceLog": {
                    "Header": {
                        "DateTime": "2026-09-08T15:17:38+03:00",
                        "Device": {
                            "Info": {"BSL": "2.51.28", "HW": "Seal_RevA3", "SW": "2.51.28"},
                            "Name": "Porvoo",
                            "SerialNumber": "253810000400",
                        },
                    }
                }
            }
        ).encode()

        device = SuuntoJsonParser.parse(content).device

        assert device is not None
        assert (device.brand, device.model) == ("Suunto", None)
        assert (device.serial, device.firmware, device.name) == ("253810000400", "2.51.28", "Porvoo")

    def test_json_falls_back_to_the_top_level_device_block(self):
        """The 2026 Ocean export writes the same object twice, under `Header.Device` and
        under `DeviceLog.Device`. The header's is read first so the device sits beside the
        dive it describes; this is what an export carrying only the outer one yields."""
        content = json.dumps(
            {
                "DeviceLog": {
                    "Header": {"Duration": 1800},
                    "Device": {"Name": "Porvoo", "SerialNumber": "253810000400"},
                }
            }
        ).encode()

        device = SuuntoJsonParser.parse(content).device

        assert device is not None
        assert (device.serial, device.name) == ("253810000400", "Porvoo")

    def test_json_without_a_diving_block_reports_no_counter(self):
        """`NumberInSeries` lives under `Header.Diving`, and the whole 2026 Ocean shape has
        no `Diving` block at all - including the one export in the corpus that carries a
        serial. Absent is what that means; nothing reconstructs the counter from elsewhere.
        """
        content = json.dumps(
            {"DeviceLog": {"Header": {"Duration": 1800, "Device": {"SerialNumber": "253810000400"}}}}
        ).encode()
        assert "Diving" not in json.loads(content)["DeviceLog"]["Header"]

        device = SuuntoJsonParser.parse(content).device

        assert device is not None
        assert device.serial == "253810000400"
        assert device.dive_number is None

    def test_the_json_counter_lands_on_the_device_and_not_on_the_dive(self):
        content = json.dumps({"DeviceLog": {"Header": {"Diving": {"NumberInSeries": 5}}}}).encode()

        parsed = SuuntoJsonParser.parse(content)

        assert parsed.dive_number is None
        assert parsed.device is not None
        assert parsed.device.dive_number == 5

    def test_fit_reports_the_computer_that_wrote_it(self):
        """`file_id.product_name` is the model, and the brand decodes to the
        profile's lowercase `suunto` where the JSON export of the same computer writes the
        literal `Suunto` - both are kept as read. The `product` beside it is the corpus
        Ocean's own `62`, carried here because the file carries it and read by nothing.

        Not built through `dive_fit_file`, which writes a `file_id` of its own: only the
        first is kept, so a second one would say nothing.
        """
        content = fit_file(
            message("file_id", type="activity", manufacturer="suunto", product_name="Suunto Ocean", product=62),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=3473.0, dive_number=3),
        )

        device = FitParser.parse(content).device

        assert device is not None
        assert (device.brand, device.model) == ("suunto", "Suunto Ocean")
        assert device.dive_number == 3
        # The corpus's Ocean FIT carries no serial anywhere and no name its owner set,
        # which is exactly why the same dive's JSON export is worth reading too.
        assert (device.serial, device.firmware, device.name) == (None, None, None)

    @pytest.mark.parametrize(("manufacturer", "product"), [("suunto", 62), ("garmin", 3542)])
    def test_fit_reports_no_model_where_there_is_no_product_name(self, manufacturer: str, product: int) -> None:
        """`file_id.product` is a vendor id, not a name, and is never read as the model.

        Both vendors are here because the field is wrong in two different ways. `fitdecode`
        resolves `product` only for the manufacturers the profile gives a subfield, so a
        Suunto's stays the bare integer `62` and taking it would put the string `62` in a
        logbook where the model belongs. Garmin's *does* have one (`garmin_product`), so
        `3542` decodes to the readable `descent_mk2s` - and that is still declined, because
        it is a profile constant rather than the vendor's own string and the member would
        otherwise be two different kinds of thing depending on who wrote the file.

        The brand is unaffected either way: it is `file_id.manufacturer`, a member
        of its own, and is not what the model falls back to.
        """
        content = fit_file(
            message("file_id", type="activity", manufacturer=manufacturer, product=product),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=3473.0),
        )

        device = FitParser.parse(content).device

        assert device is not None
        assert (device.brand, device.model) == (manufacturer, None)

    def test_the_fit_counter_lands_on_the_device_and_not_on_the_dive(self):
        """The move `session.dive_number`'s comment used to describe as "deliberately not
        parsed". It is parsed now, onto the counter that says what it is."""
        parsed = FitParser.parse(dive_fit_file(dive_number=3))

        assert parsed.dive_number is None
        assert parsed.device is not None
        assert parsed.device.dive_number == 3

    def test_fit_takes_the_serial_from_the_device_info_for_the_computer_itself(self):
        """`device_index` 0 is the profile's `creator` - the computer writing the file, as
        against the pods and straps a `device_info` equally describes.

        The index has to be read *raw*, and this is the case that proves it: the profile
        keeps an enum in that slot, so `fitdecode` renders the 0 as the string `creator`
        and a comparison against the decoded value matches nothing. Both messages here
        carry a serial, and only the computer's may come back.
        """
        content = dive_fit_file(
            message("device_info", device_index=1, manufacturer="suunto", serial_number=2411100050),
            message("device_info", device_index=0, manufacturer="suunto", serial_number=1924100042),
        )

        device = FitParser.parse(content).device

        assert device is not None
        assert device.serial == "1924100042"

    def test_fit_falls_back_to_the_file_ids_serial(self):
        """Where nothing names index 0 - the corpus's own shape, both of the Ocean's
        `device_info` messages carrying no `device_index` at all - the file's own identity
        message is what claims a serial for the computer that wrote it."""
        content = fit_file(
            message("file_id", type="activity", manufacturer="suunto", serial_number=1924100042),
            message("device_info", manufacturer="suunto", serial_number=2411100050),
            message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=3473.0),
        )

        device = FitParser.parse(content).device

        assert device is not None
        assert device.serial == "1924100042"

    def test_fit_takes_the_firmware_from_the_computers_own_device_info(self):
        """A `device_info` naming a manufacturer the `file_id` does not is another link in
        the chain - a tank pod, a strap - and reading its `software_version` as the
        computer's would put a transmitter's firmware on the dive."""
        content = dive_fit_file(
            message("device_info", manufacturer="garmin", software_version=9.9),
            message("device_info", manufacturer="suunto", software_version=2.51),
        )

        device = FitParser.parse(content).device

        assert device is not None
        assert device.firmware == "2.51"

    def test_fit_keeps_no_more_device_info_messages_than_the_cap(self):
        """A device writes one every few minutes and nothing downstream bounds the list, so
        this parser does - the same reason `_MAX_EVENTS` exists. The cap is on what is
        *kept*, not on what the file may hold: a file over it still parses, and the
        firmware it carries past the cap is simply not read."""
        content = dive_fit_file(
            *(message("device_info", manufacturer="suunto") for _ in range(_MAX_DEVICE_INFO + 4)),
            message("device_info", manufacturer="suunto", software_version=2.51),
        )

        device = FitParser.parse(content).device

        assert device is not None
        assert device.firmware is None

    def test_a_file_that_names_no_device_reports_none(self):
        """A FIT file need not carry a `file_id` at all, and one that doesn't, whose session
        carries no counter either, has said nothing about what wrote it. A `ParsedDevice` of
        six nulls would claim otherwise, so `_drop_empty_device` throws it away."""
        content = fit_file(message("session", sport="diving", start_time=DIVE_START, total_elapsed_time=3473.0))

        assert FitParser.parse(content).device is None

    def test_a_padded_or_empty_identity_is_absent_rather_than_empty(self):
        """`""` compares unequal to `None`, so a device that named itself nothing would
        fail to match the same computer read out of another export - which is the one
        comparison a device exists to support."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <SerialNumber>   </SerialNumber>
  <Software></Software>
  <Source>  Suunto D5  </Source>
</Dive>
""".encode()

        device = SuuntoXmlParser.parse(content).device

        assert device is not None
        assert device.model == "Suunto D5"
        assert (device.serial, device.firmware) == (None, None)

    def test_a_negative_counter_is_not_a_count(self):
        """`< 0`, not `<= 0`: a computer that has recorded no dive yet counts 0, and telling
        that apart from "the file didn't say" is why the member is nullable."""
        template = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><DiveNumberInSerie>{{number}}</DiveNumberInSerie></Dive>
"""

        zero = SuuntoXmlParser.parse(template.format(number=0).encode()).device
        negative = SuuntoXmlParser.parse(template.format(number=-1).encode()).device

        assert zero is not None and zero.dive_number == 0
        assert negative is not None and negative.dive_number is None

    def test_the_parse_response_carries_the_device_through(self):
        """`ParsedDiveResponse` subclasses the parse schema, so the member reaches
        `POST /dive/parse` without the route naming it - the same way every field above it
        does."""
        parsed = SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode())

        response = ParsedDiveResponse(**parsed.model_dump(), file_token="token")

        assert response.device is not None
        assert response.device.dive_number == 5

    def test_creating_a_dive_still_forbids_the_member(self):
        """The device is a fact about a file, not a field of the logbook entry, and there is
        nowhere to put one yet. `DiveCreate` is `extra="forbid"`, so a client echoing the
        parse response straight back gets a 422 rather than a silently dropped member.

        The body is otherwise complete, and the error type is asserted rather than the bare
        raise: `DiveCreate` has three required fields, so a payload that omits any of them
        raises whether or not the extra member was rejected, and the test would pass with
        `extra="forbid"` removed.
        """
        assert "device" not in DiveCreate.model_fields
        valid = {"dive_number": 1, "duration": 1800, "start_time": "2026-04-17T11:49:23+02:00"}
        assert DiveCreate(**valid).dive_number == 1

        with pytest.raises(ValidationError) as raised:
            DiveCreate(**valid, device={"brand": "Suunto"})

        assert [(error["type"], error["loc"]) for error in raised.value.errors()] == [("extra_forbidden", ("device",))]


class TestParsersReportTheModeAndTheDecoModel:
    """How the computer was configured for this dive - the recording's, never the dive's.

    Two computers on one dive give two answers to each: a backup run in gauge mode beside a
    primary on open circuit is ordinary practice, and the dive was not a gauge dive. Both
    members ride `ParsedDiveSchema` beside the device rather than inside it, because a device
    is the hardware and these are settings - the same computer dived twice on different
    gradient factors is one device and two models.

    Every mapping below is a table drawn from files in hand, never from the vocabulary's own
    names: an unseen value leaves the member absent, which is the one thing `mode` requires -
    a reader must not read an absence as open circuit.
    """

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("0", DiveMode.OPEN_CIRCUIT), ("1", DiveMode.OPEN_CIRCUIT), ("3", DiveMode.FREEDIVE)],
    )
    def test_the_xml_mode_table_is_the_corpus_own(self, mode: str, expected: DiveMode) -> None:
        """0 and 1 are the air and nitrox modes of an open-circuit computer, which the oxygen
        fractions say: 243 of the 244 `<Mode>0</Mode>` exports carry a single 21 % mixture and
        every one of the 98 `<Mode>1</Mode>` exports carries something richer. 3 is a
        freedive, and the 42 stating it are exactly the 42 carrying no `<DiveMixture>`."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><Mode>{mode}</Mode></Dive>
""".encode()

        assert SuuntoXmlParser.parse(content).mode is expected

    def test_an_unseen_or_absent_xml_mode_leaves_the_member_absent(self):
        """An absence is not a claim, and a reader must not read one as open circuit however
        a source format's documentation glosses it."""
        template = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">{{body}}</Dive>
"""

        assert SuuntoXmlParser.parse(template.format(body="").encode()).mode is None
        assert SuuntoXmlParser.parse(template.format(body="<Mode>7</Mode>").encode()).mode is None

    def test_the_xml_personal_mode_is_the_conservatism_and_keeps_its_sign(self):
        """Suunto's own P-2 to P2 scale, which is exactly what the member holds: the device's
        number, meaningful beside the device. `0` is the P0 setting rather than an absence and
        `-1` is P-1 - the two values in the corpus - so no floor is applied. It is the one
        reading in this package where a negative is data."""
        template = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><Mode>0</Mode><PersonalMode>{{value}}</PersonalMode></Dive>
"""

        for value, expected in ((0, 0), (-1, -1)):
            model = SuuntoXmlParser.parse(template.format(value=value).encode()).deco_model
            assert model is not None and model.conservatism == expected

    def test_a_freedive_states_a_personal_mode_and_it_is_not_carried(self):
        """The model is what a device ran on *this* dive; a freedive ran none, and a model
        carrying only a conservatism would say otherwise."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><Mode>3</Mode><PersonalMode>0</PersonalMode></Dive>
""".encode()

        parsed = SuuntoXmlParser.parse(content)

        assert parsed.mode is DiveMode.FREEDIVE
        assert parsed.deco_model is None

    def test_the_xml_algorithm_enum_is_deliberately_not_the_family(self):
        """`<Algorithm>` reads `0` on every scuba export in hand and nil on every freedive,
        so nothing in the corpus says what any other value would mean. A family is a claim
        about the mathematics rather than a number nobody has decoded."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><Mode>0</Mode><Algorithm>0</Algorithm></Dive>
""".encode()

        assert SuuntoXmlParser.parse(content).deco_model is None

    @pytest.mark.parametrize("dive_mode", ["Air", "Nitrox", "Mixed"])
    def test_the_json_mode_table_is_three_gas_modes_of_one_open_circuit_computer(self, dive_mode: str) -> None:
        content = json.dumps(
            {"DeviceLog": {"Header": {"DateTime": "2025-05-31T12:59:06+02:00", "Diving": {"DiveMode": dive_mode}}}}
        ).encode()

        assert SuuntoJsonParser.parse(content).mode is DiveMode.OPEN_CIRCUIT

    def test_gauge_and_free_are_not_in_the_json_table(self):
        """No file in hand carries either - the D5's free and gauge modes write no
        `Header.Diving` this reader has seen - and a mapping no file exercises is a mapping
        nothing checks."""
        content = json.dumps(
            {"DeviceLog": {"Header": {"DateTime": "2025-05-31T12:59:06+02:00", "Diving": {"DiveMode": "Gauge"}}}}
        ).encode()

        assert SuuntoJsonParser.parse(content).mode is None

    def test_the_json_algorithm_fills_the_name_verbatim_and_the_family_from_a_table(self):
        """Neither is derived from the other: the member is the device's own name for its
        model, and vendors name and version theirs as they please."""
        content = json.dumps(
            {
                "DeviceLog": {
                    "Header": {
                        "DateTime": "2025-05-31T12:59:06+02:00",
                        "Diving": {"Algorithm": "Suunto Fused2 RGBM", "Conservatism": -1},
                    }
                }
            }
        ).encode()

        model = SuuntoJsonParser.parse(content).deco_model

        assert model is not None
        assert (model.name, model.algorithm) == ("Suunto Fused2 RGBM", DecoAlgorithm.RGBM)
        assert model.conservatism == -1

    def test_an_algorithm_string_outside_the_table_fills_the_name_and_no_family(self):
        """A family is a claim about the mathematics, not a guess off a product string."""
        content = json.dumps(
            {
                "DeviceLog": {
                    "Header": {"DateTime": "2025-05-31T12:59:06+02:00", "Diving": {"Algorithm": "Suunto Fused RGBM"}}
                }
            }
        ).encode()

        model = SuuntoJsonParser.parse(content).deco_model

        assert model is not None
        assert (model.name, model.algorithm) == ("Suunto Fused RGBM", None)

    def test_an_ocean_export_yields_no_model_at_all(self):
        """The 2026 Ocean shape has no `Header.Diving`, so it yields the channels and no
        model - which is correct rather than a gap."""
        content = json.dumps({"DeviceLog": {"Header": {"DateTime": "2025-05-31T12:59:06+02:00"}}}).encode()

        parsed = SuuntoJsonParser.parse(content)

        assert parsed.mode is None
        assert parsed.deco_model is None

    def test_fit_reads_the_gradient_factor_pair_off_dive_settings(self):
        """Already whole percent in the FIT profile, which is the member's unit, so nothing
        is scaled."""
        content = dive_fit_file(message("dive_settings", gf_low=50, gf_high=85))

        model = FitParser.parse(content).deco_model

        assert model is not None
        assert (model.gf_low, model.gf_high) == (50, 85)

    def test_fit_reads_the_family_only_where_the_file_states_one(self):
        """`tissue_model_type` has exactly one member in the FIT profile, and reading "there
        is only one value in the enum" as "the family must be Buhlmann" would be the parser
        deciding what the device ran - on a Suunto watch writing Garmin's format, whose own
        app export names an RGBM model for the same dive."""
        without = FitParser.parse(dive_fit_file(message("dive_settings", gf_low=50, gf_high=85))).deco_model
        stated = FitParser.parse(
            dive_fit_file(message("dive_settings", gf_low=50, gf_high=85, model="zhl_16c"))
        ).deco_model

        assert without is not None and without.algorithm is None
        assert stated is not None and stated.algorithm is DecoAlgorithm.BUHLMANN

    def test_a_fit_file_with_no_dive_settings_yields_no_model(self):
        """The message is the computer's *configuration* rather than a record of the dive, so
        its absence is not a device that declined to say."""
        assert FitParser.parse(VALID_FIT).deco_model is None

    def test_fit_states_no_mode(self):
        """`session.sub_sport` is the field that would carry one and no file in hand writes
        it, so a FIT dive - a freediving one included - is a dive that does not say what kind
        it is. The DM5 XML path does say, because its files state it."""
        assert FitParser.parse(VALID_FIT).mode is None

    def test_one_gradient_factor_without_its_pair_names_no_setting(self):
        """Both or neither: one alone is half a setting, and §6.4c makes the pair
        both-or-neither for that reason."""
        model = FitParser.parse(dive_fit_file(message("dive_settings", gf_low=50))).deco_model

        assert model is None

    def test_an_inverted_pair_is_dropped_rather_than_reordered(self):
        """Enforced on the parse rather than left to `ck_dive_recording_deco_gf_low_within_high`:
        a parse that dies on the database takes the whole upload with it, and one inverted
        pair is not worth a lost file. Neither number says which of the two is wrong, so both
        go and the rest of the dive stays."""
        parsed = FitParser.parse(dive_fit_file(message("dive_settings", gf_low=85, gf_high=50)))

        assert parsed.deco_model is None
        assert parsed.duration is not None

    def test_a_gradient_factor_outside_whole_percent_is_not_a_setting(self):
        """The bound the column holds, mirrored where the value is produced - and
        deliberately not the per-sample `gradient_factor` channel's rule, which is uncapped
        above because a GF99 past 100 is a real reading."""
        model = FitParser.parse(dive_fit_file(message("dive_settings", gf_low=50, gf_high=150))).deco_model

        assert model is None

    def test_the_parse_response_carries_both_members_through(self):
        """`ParsedDiveResponse` subclasses the parse schema, so they reach `POST /dive/parse`
        without the route naming them."""
        parsed = SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode())

        response = ParsedDiveResponse(**parsed.model_dump(), file_token="token")

        assert response.mode is parsed.mode
        assert response.deco_model == parsed.deco_model


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

    @staticmethod
    def _xml_with_pod_on(position: int, mixtures: int = 2) -> bytes:
        """Two cylinders, `<TransmitterId>` on exactly one of them, and one pressure
        sample. DM5 writes `0` pressures and `xsi:nil` on the untransmitted mixture."""
        rows = "".join(
            f"""    <DiveMixture>
      <Oxygen>{21 if i == 1 else 50}</Oxygen><Helium>0</Helium>
      <StartPressure>{200000 if i == position else 0}</StartPressure>
      <EndPressure>{120000 if i == position else 0}</EndPressure>
      {f"<TransmitterId>241110005{i}</TransmitterId>" if i == position else '<TransmitterId xsi:nil="true"/>'}
    </DiveMixture>
"""
            for i in range(1, mixtures + 1)
        )
        return f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <DiveMixtures>
{rows}  </DiveMixtures>
  <DiveSamples>
    <Dive.Sample><Time>60</Time><Depth>1200</Depth><Pressure>1900</Pressure></Dive.Sample>
  </DiveSamples>
</Dive>
""".encode()

    def test_the_xml_pressure_channel_is_labelled_from_the_transmitter_not_from_position(self):
        """A transmitted deco bottle behind an untransmitted back gas. Hardcoding the
        channel to 1 put the deco bottle's curve on the back gas - and the back gas is
        exactly the cylinder whose own pressures `_drop_unpressurized` nulls, so nothing
        downstream could have caught the mislabel. Unattested: all 11 two-mixture exports
        in the corpus have the pod on mixture 1, which is why it survived."""
        profile = SuuntoXmlParser.parse_profile(self._xml_with_pod_on(2))
        parsed = SuuntoXmlParser.parse(self._xml_with_pod_on(2))

        assert profile is not None
        assert [series.gas_number for series in profile.pressure] == [2]
        # And it is the mixture the pod was actually on - the join the label exists for.
        transmitted = [mix for mix in parsed.mixtures if mix.start_pressure is not None]
        assert [mix.gas_number for mix in transmitted] == [2]

    def test_the_usual_shape_still_labels_the_channel_one(self):
        """The corpus's only attested arrangement, and the fallback when the file names no
        transmitter at all - there are no pressure samples to mislabel in that case."""
        profile = SuuntoXmlParser.parse_profile(self._xml_with_pod_on(1))

        assert profile is not None
        assert [series.gas_number for series in profile.pressure] == [1]

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
        the band is the one `ck_dive_recording_surface_pressure_range` enforces, and the column is
        written inside `store_recording_file`'s transaction. A value the CHECK rejects would
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
            c
            for c in DiveRecording.__table__.constraints
            if getattr(c, "name", None) == "ck_dive_recording_surface_pressure_range"
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

        assert "0.4" in sqltext and "1.2" in sqltext
        # The bounds themselves are inclusive on both sides, as the CHECK's `>=`/`<=` are.
        assert parsed_with(0.4) == 0.4
        assert parsed_with(1.2) == 1.2
        assert parsed_with(0.39) is None
        assert parsed_with(1.21) is None

    def test_a_negative_exposure_reading_reads_as_no_reading(self):
        """Oxygen loading does not run backwards. Unguarded, a negative here violates
        `ck_dive_recording_cns_start_non_negative` *inside* `store_recording_file`'s transaction, so the
        attach rolled back and the diver got a 409 telling them to retry an upload that
        could never succeed."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <CnsStart>-4</CnsStart><CnsEnd>-1</CnsEnd><OtuStart>-9</OtuStart><OtuEnd>-0.5</OtuEnd>
</Dive>
""".encode()

        parsed = SuuntoXmlParser.parse(content)

        assert (parsed.cns_start, parsed.cns_end) == (None, None)
        assert (parsed.otu_start, parsed.otu_end) == (None, None)

    def test_a_nan_exposure_reading_reads_as_no_reading(self):
        """`NaN` is caught by neither `value < 0` nor `cns_start >= 0` - it compares false
        against every bound in Python and *true* against them in Postgres, which sorts it
        above all numbers. Stored, it makes `GET /dives` 500 for the whole list, because
        `JSONResponse` serializes with `allow_nan=False`. `backfill_tech_fields` is the
        path that reaches the column without serializing anything on the way."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <CnsStart>NaN</CnsStart><CnsEnd>NaN</CnsEnd><OtuStart>NaN</OtuStart><OtuEnd>NaN</OtuEnd>
</Dive>
""".encode()

        parsed = SuuntoXmlParser.parse(content)

        assert (parsed.cns_start, parsed.cns_end) == (None, None)
        assert (parsed.otu_start, parsed.otu_end) == (None, None)
        # The point of nulling rather than passing through: the result is serializable.
        json.dumps(parsed.model_dump(), allow_nan=False)

    def test_no_non_finite_float_leaves_a_parser_on_any_field(self):
        """The rule is on `_ParserOutput`, not on the bounds, because the bounds are
        comparisons and `NaN` compares `False` against all of them. `avg_depth`,
        `max_depth` and `bottom_temperature` carry no bound at all and are the proof -
        under a per-field fix they would still have 500'd `POST /dive/parse`. `inf` counts
        too: it passes `>= 0` honestly and is no more a reading than `NaN`."""
        dive = ParsedDiveSchema(
            avg_depth=float("nan"),
            bottom_temperature=float("nan"),
            dive_number=None,
            duration=None,
            max_depth=float("inf"),
            start_time=None,
            mixtures=[],
            cns_start=float("nan"),
            cns_end=float("inf"),
            otu_start=float("-inf"),
            otu_end=float("nan"),
            surface_pressure_bar=float("nan"),
        )
        mixture = DiveMixtureSchema(
            end_pressure=float("nan"),
            gas_number=None,
            helium=float("nan"),
            oxygen=float("inf"),
            po2_limit=float("nan"),
            role=None,
            start_pressure=float("inf"),
            volume=float("nan"),
        )

        assert all(value is None for value in dive.model_dump().values() if not isinstance(value, list))
        assert all(value is None for value in mixture.model_dump().values())
        # The point of nulling rather than passing through: both are serializable.
        json.dumps(dive.model_dump(), allow_nan=False)
        json.dumps(mixture.model_dump(), allow_nan=False)

    def test_the_finite_guard_does_not_touch_anything_else(self):
        """It runs for every field, including the ones it must leave alone - a wildcard
        validator that nulled a `str`, an `int`, an enum or the mixtures list would be a
        far worse bug than the one it fixes. `start_time` is the `str` here; the mixtures
        carry none since `name` was removed."""
        mixture = DiveMixtureSchema(
            end_pressure=120.0,
            gas_number=0,
            helium=0.0,
            oxygen=21.0,
            po2_limit=1.4,
            role=GasRole.BOTTOM,
            start_pressure=200.0,
            volume=11.1,
        )
        dive = ParsedDiveSchema(
            avg_depth=12.5,
            bottom_temperature=8.0,
            dive_number=41,
            duration=2400,
            max_depth=27.3,
            start_time="2026-06-03T12:15:00",
            mixtures=[mixture],
            cns_start=0.0,
            otu_end=53.0,
        )

        assert (mixture.gas_number, mixture.role) == (0, GasRole.BOTTOM)
        assert (dive.dive_number, dive.duration, dive.start_time) == (41, 2400, "2026-06-03T12:15:00")
        assert (dive.avg_depth, dive.max_depth, dive.bottom_temperature) == (12.5, 27.3, 8.0)
        # Zero is a reading, and the wildcard is not a truthiness test.
        assert dive.cns_start == 0.0
        assert dive.mixtures == [mixture]

    def test_a_zero_or_negative_depth_reads_as_no_depth(self):
        """`<= 0`, against `_drop_negative_exposure`'s `< 0` one method over - the two
        constraints genuinely differ (`ck_dive_max_depth_positive` is `> 0`,
        `ck_dive_recording_cns_start_non_negative` is `>= 0`), because a dive that began with no
        oxygen loading recorded a real 0 and a dive to 0 m did not happen."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <AvgDepth>0</AvgDepth><MaxDepth>-3.2</MaxDepth><CnsStart>0</CnsStart>
</Dive>
""".encode()

        parsed = SuuntoXmlParser.parse(content)

        assert (parsed.avg_depth, parsed.max_depth) == (None, None)
        # The neighbouring rule is untouched: 0 is still a reading where the column says so.
        assert parsed.cns_start == 0.0

    def test_a_real_depth_survives_the_guard(self):
        """The guard must not cost the corpus, where every recorded depth is positive."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <AvgDepth>12.3</AvgDepth><MaxDepth>25.5</MaxDepth>
</Dive>
""".encode()

        parsed = SuuntoXmlParser.parse(content)

        assert (parsed.avg_depth, parsed.max_depth) == (12.3, 25.5)

    def test_a_recorded_zero_exposure_is_still_a_reading(self):
        """`< 0`, not `<= 0`. A dive that began with no oxygen loading recorded a real 0,
        and the four constraints are `>= 0` precisely to keep that apart from null."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <CnsStart>0</CnsStart><OtuStart>0</OtuStart>
</Dive>
""".encode()

        parsed = SuuntoXmlParser.parse(content)

        assert parsed.cns_start == 0.0
        assert parsed.otu_start == 0.0

    def test_a_negative_gas_number_reads_as_no_label(self):
        """Only `_mixtures_from_cylinders` reads a number a file chose; the other three
        paths synthesize it with `enumerate`. One path is enough to 422 a form field the
        diver never chose."""
        mixture = DiveMixtureSchema(
            end_pressure=None,
            gas_number=-7,
            helium=None,
            oxygen=None,
            po2_limit=None,
            role=None,
            start_pressure=None,
            volume=None,
        )

        assert mixture.gas_number is None

    def test_gas_number_zero_is_a_real_label(self):
        """A Suunto Ocean numbers its cylinders from 0, which is why the constraint is
        `>= 0` and not the 1-based check it started as."""
        mixture = DiveMixtureSchema(
            end_pressure=None,
            gas_number=0,
            helium=None,
            oxygen=None,
            po2_limit=None,
            role=None,
            start_pressure=None,
            volume=None,
        )

        assert mixture.gas_number == 0

    def test_every_single_column_bound_a_parser_can_reach_has_a_parse_side_guard(self):
        """The drift guard for the rule itself.

        The rule was applied to two of seven columns and then written up as covering all
        of them, which is how five stayed unguarded through two review rounds. Counted
        here against the constraints rather than restated in prose, so the next column
        with a `CHECK` either gets a validator or fails this.

        Now counts the two depth columns as well. They are older than the phase that
        guarded the rest, which is exactly why they were missed - the set worth checking
        is "bounded and reachable from a parser", not "bounded and added recently".

        **Single-column bounds only, and the rest is deliberate rather than forgotten.**
        `ck_dive_mixture_oxygen_helium_sum` and `ck_dive_mixture_pressure_order` constrain
        a *pair*, so there is no "the bad value" to null - honouring them on the parse side
        means choosing which of two recorded readings to discard, which is a different
        decision from "this number is not a reading" and is not made here.
        `ck_dive_avg_depth_within_max` is the same shape and excluded on the same terms.
        The two
        `ck_dive_*_position_pair` constraints are pairs in the same sense and are excluded
        for the same reason - they are honoured by `_drop_half_positions`, a *model*
        validator, which is exactly what this counter cannot see. The four coordinate
        *ranges* under them are single-column and are counted. `duration`,
        `volume`, `oxygen` and `helium` are single-column and still unguarded; they are
        pre-existing and out of this phase's scope, and they are listed here so the gap is
        recorded rather than implied.

        **`altitude` is bounded and deliberately not here.** `ck_dive_altitude_range` is a
        single-column bound, but no parser can reach that column: it is diver-entered
        only, and nothing in any supported export carries a surface elevation. The set is
        "bounded *and reachable from a parser*", so listing it would fail the assert below
        for a value a parser cannot produce. It belongs with `duration` and friends as a
        bounded-yet-unguarded column, and joins this set the day a parser learns to fill
        it.
        """
        bounded = {
            (Dive, "avg_depth"),
            (Dive, "max_depth"),
            (DiveRecording, "cns_start"),
            (DiveRecording, "cns_end"),
            (DiveRecording, "otu_start"),
            (DiveRecording, "otu_end"),
            (DiveRecording, "surface_pressure_bar"),
            (Dive, "entry_latitude"),
            (Dive, "entry_longitude"),
            (Dive, "exit_latitude"),
            (Dive, "exit_longitude"),
            (DiveMixture, "po2_limit"),
            (DiveMixture, "gas_number"),
        }
        # Every one of those really is constrained on the model side...
        for model, column in bounded:
            constraints = " ".join(str(getattr(c, "sqltext", "")) for c in model.__table__.constraints)
            assert column in constraints, f"{model.__name__}.{column} has no CHECK"

        # ...and every one is validated on the way in, on whichever schema carries it.
        guarded = {
            *((Dive, name) for name in _validated_fields(ParsedDiveSchema)),
            *((DiveRecording, name) for name in _validated_fields(ParsedDiveSchema)),
            *((DiveMixture, name) for name in _validated_fields(DiveMixtureSchema)),
        }
        assert bounded <= guarded, f"unguarded: {bounded - guarded}"

    def test_an_out_of_band_po2_limit_reads_as_no_limit(self):
        """The last bounded field a parsed value could reach unguarded. Unattested - the
        corpus writes `<PO2>` as 1.4 or 1.6 and says "not recorded" with `i:nil` - but the
        backfill writes this column through a Core `UPDATE` that never sees Pydantic, so
        the schema is the only place a guard covers both the import and the backfill."""
        for reading in ("0", "0.39", "2.01", "140000"):
            content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures><DiveMixture><Oxygen>21</Oxygen><Helium>0</Helium><PO2>{reading}</PO2></DiveMixture></DiveMixtures>
</Dive>
""".encode()

            assert SuuntoXmlParser.parse(content).mixtures[0].po2_limit is None, reading

    def test_a_recorded_po2_limit_inside_the_band_survives(self):
        """The guard must not cost the 353 mixtures in the corpus that do record one."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures>
    <DiveMixture><Oxygen>21</Oxygen><Helium>0</Helium><PO2>1.4</PO2></DiveMixture>
    <DiveMixture><Oxygen>50</Oxygen><Helium>0</Helium><PO2>1.6</PO2></DiveMixture>
  </DiveMixtures>
</Dive>
""".encode()

        assert [m.po2_limit for m in SuuntoXmlParser.parse(content).mixtures] == [1.4, 1.6]

    def test_the_po2_bounds_are_the_ones_the_database_enforces(self):
        constraint = next(
            c
            for c in DiveMixture.__table__.constraints
            if getattr(c, "name", None) == "ck_dive_mixture_po2_limit_range"
        )

        assert "0.4" in str(constraint.sqltext) and "2.0" in str(constraint.sqltext)

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


class TestTheDecoModelShape:
    """`ParsedDecoModel`'s own validators, which every parser inherits.

    Unit tests rather than per-parser ones for the reason `_drop_unpressurized` is on
    `DiveMixtureSchema` rather than in `SuuntoXmlParser`: the rules are about the members and
    not about any one format, and putting them on the schema is what makes a fourth parser
    inherit them.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, 0), (-1, -1), (2.0, 2), (1.5, None), (True, None), ("P2", None), (None, None)],
    )
    def test_a_setting_is_a_whole_number_or_nothing(self, value: Any, expected: int | None) -> None:
        """A JSON export can write any number at all into `Conservatism`, and Pydantic's own
        coercion would raise on a fractional one and take the whole parse with it. A `bool` is
        excluded before `int` because `isinstance(True, int)` is true and a flag is not a
        setting. **And nothing is floored**: `-1` is Suunto's P-1 rather than an absence."""
        assert ParsedDecoModel(conservatism=value).conservatism == expected

    def test_a_model_name_longer_than_the_column_is_truncated_rather_than_refused(self):
        """A file this app cannot store one member of is still a file worth importing, and
        64 is the format's own bound as well as `deco_name`'s width."""
        model = ParsedDecoModel(name="  " + "Suunto Fused RGBM " * 10 + "  ")

        assert model.name is not None
        assert len(model.name) == 64
        assert model.name.startswith("Suunto Fused RGBM")

    def test_a_name_that_is_only_whitespace_is_absent(self):
        """`""` is not a model the file named, and a trimmed empty string is the same thing
        one step later - the rule `ParsedDevice._as_trimmed_text` makes for an identity."""
        assert ParsedDecoModel(name="   ").name is None

    def test_a_model_of_nothing_is_not_a_model_the_file_described(self):
        """Reachable only through the validators above: a file stating an unrecognized
        algorithm string and one gradient factor leaves an object of five nulls, and
        reporting that would claim the export named a model when it named none."""
        parsed = ParsedDiveSchema(
            avg_depth=None,
            bottom_temperature=None,
            dive_number=None,
            duration=None,
            max_depth=None,
            start_time=None,
            mixtures=[],
            deco_model=ParsedDecoModel(gf_low=50),
        )

        assert parsed.deco_model is None

    def test_a_model_carrying_one_member_survives(self):
        """The boundary the test above needs: `_drop_empty_deco_model` drops an *empty* one,
        not a sparse one. A Suunto XML export states a conservatism and nothing else."""
        parsed = ParsedDiveSchema(
            avg_depth=None,
            bottom_temperature=None,
            dive_number=None,
            duration=None,
            max_depth=None,
            start_time=None,
            mixtures=[],
            deco_model=ParsedDecoModel(conservatism=0),
        )

        assert parsed.deco_model is not None and parsed.deco_model.conservatism == 0


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

    def test_xml_leaves_an_unnamed_device_null(self):
        """Every device member but the brand, which is the format's rather than a
        field's: a DM5 export is a Suunto export by its namespace, and `can_parse` has
        already required it. A model or a serial standing in for one the export never wrote
        would be the same failure as a `volume: 0.0`."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><Duration>1800</Duration></Dive>
""".encode()

        device = SuuntoXmlParser.parse(content).device

        assert device is not None
        assert device.brand == "Suunto"
        assert (device.model, device.serial, device.firmware) == (None, None, None)
        assert (device.name, device.dive_number) == (None, None)

    def test_json_leaves_an_unnamed_device_null(self):
        """The header-only shape, which is most of the corpus: no `Device` block at all,
        and so nothing to say beyond the brand the format itself fixes."""
        content = json.dumps({"DeviceLog": {"Header": {"Duration": 1800}}}).encode()

        device = SuuntoJsonParser.parse(content).device

        assert device is not None
        assert device.brand == "Suunto"
        assert (device.model, device.serial, device.firmware) == (None, None, None)
        assert (device.name, device.dive_number) == (None, None)

    def test_fit_leaves_an_unnamed_device_null(self):
        """A `file_id` naming a maker and nothing else, which is the least a FIT
        file says about itself. FIT has no field for a name its owner chose at all, so
        `name` is null on every file rather than only on this one."""
        device = FitParser.parse(dive_fit_file()).device

        assert device is not None
        assert device.brand == "suunto"
        assert (device.model, device.serial, device.firmware) == (None, None, None)
        assert (device.name, device.dive_number) == (None, None)

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


class TestEntryAndExitPositions:
    """Which satellite fix becomes the entry point and which the exit one.

    The numbers below are the two dives in the corpus that exist as *both* a FIT and a
    Suunto JSON export - the only cross-format check available for this, and the reason
    the two conversions (semicircles, radians) are pinned to the same degrees.
    """

    # `69cfaef7`, off Dahab. The FIT export writes these two semicircle counts; the JSON
    # export of the same dive writes the same position in radians.
    DAHAB_SEMICIRCLES = (339272053, 411111844)
    DAHAB_RADIANS = (0.49632722063772405, 0.6014229493488608)
    DAHAB_DEGREES = (28.437455, 34.458997)

    # `69e21526`, the same dive's two coordinate channels. The origin block and the first
    # sample fix land 4 m apart on one jetty - and are written in *different units*, which
    # is the trap `degrees_verbatim` exists for and what these two pairs pin down.
    OCEAN_ORIGIN_DEGREES = (28.567251205444336, 34.53325653076172)
    OCEAN_ORIGIN_ROUNDED = (28.567251, 34.533257)
    OCEAN_SURFACED_RADIANS = (0.49859222167449974, 0.6027186224443467)
    OCEAN_SURFACED_ROUNDED = (28.56723, 34.533233)

    @staticmethod
    def _fit(*records: Message) -> ParsedDiveSchema:
        return FitParser.parse(dive_fit_file(*records))

    @staticmethod
    def _fix_record(offset: int, latitude: int | None, longitude: int | None, depth: float = 0.0) -> Message:
        """A `record` carrying a depth reading and, where given, a position in semicircles."""
        position = {}
        if latitude is not None:
            position["position_lat"] = latitude
        if longitude is not None:
            position["position_long"] = longitude
        return message("record", timestamp=DIVE_START + timedelta(seconds=offset), depth=depth, **position)

    def test_fit_reads_semicircles_as_degrees(self):
        parsed = self._fit(
            self._fix_record(0, *self.DAHAB_SEMICIRCLES),
            self._fix_record(600, None, None, depth=30.0),
        )

        assert (parsed.entry_latitude, parsed.entry_longitude) == self.DAHAB_DEGREES

    def test_json_reads_radians_as_the_same_degrees(self):
        """One dive, two exports, one position. Read as degrees rather than radians, the
        JSON would put a Gulf of Aqaba dive 3 000 km away in the Atlantic."""
        content = _ocean_json(
            [
                _ocean_fix(0, *self.DAHAB_RADIANS),
                _ocean_depth(600, 30.0),
            ]
        )

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.entry_latitude, parsed.entry_longitude) == self.DAHAB_DEGREES

    def test_the_entry_is_the_last_fix_before_the_deepest_sample(self):
        """Not the *first* fix: the boat motoring out to the site logs fixes too, and the
        one that says where the diver got in is the one just before the descent."""
        parsed = self._fit(
            self._fix_record(0, _semicircles(28.1), _semicircles(34.1)),
            self._fix_record(60, _semicircles(28.2), _semicircles(34.2)),
            self._fix_record(600, None, None, depth=30.0),
            self._fix_record(1200, _semicircles(28.9), _semicircles(34.9)),
        )

        assert (parsed.entry_latitude, parsed.entry_longitude) == (28.2, 34.2)

    def test_the_exit_is_the_first_fix_after_the_deepest_sample(self):
        """Mirrored, and for the mirrored reason: the diver drifts once they surface."""
        parsed = self._fit(
            self._fix_record(600, None, None, depth=30.0),
            self._fix_record(1200, _semicircles(28.5), _semicircles(34.5)),
            self._fix_record(1800, _semicircles(28.8), _semicircles(34.8)),
        )

        assert (parsed.exit_latitude, parsed.exit_longitude) == (28.5, 34.5)

    def test_a_dive_whose_fixes_all_come_after_it_has_no_entry_position(self):
        """The corpus's normal case, not an edge one: GPS does not reach a wrist under
        water, and all 19 GPS-carrying exports log their first *fix* past `DiveTime`. An
        entry position invented from those would be the exit position under another name.

        FIT, where the fixes really are the only channel. The Suunto JSON of such a dive
        gets its entry from `DiveRouteOrigin` instead - see the tests below.
        """
        parsed = self._fit(
            self._fix_record(600, None, None, depth=30.0),
            self._fix_record(3000, *self.DAHAB_SEMICIRCLES),
        )

        assert (parsed.entry_latitude, parsed.entry_longitude) == (None, None)
        assert (parsed.exit_latitude, parsed.exit_longitude) == self.DAHAB_DEGREES

    def test_the_dive_route_origin_is_the_entry_the_sample_fixes_never_carry(self):
        """`69e21526` end to end, in the shape the device writes it: an origin at t=0, a
        descent, and a fix stream that only starts once the diver is back on the surface.

        Before this the file yielded an exit and no entry, on all 19 GPS-carrying exports
        - while the Suunto app drew both pins from that same export.
        """
        content = _ocean_json(
            [
                _ocean_origin(0, *self.OCEAN_ORIGIN_DEGREES),
                _ocean_depth(600, 45.82),
                _ocean_fix(4200, *self.OCEAN_SURFACED_RADIANS),
            ]
        )

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.entry_latitude, parsed.entry_longitude) == self.OCEAN_ORIGIN_ROUNDED
        assert (parsed.exit_latitude, parsed.exit_longitude) == self.OCEAN_SURFACED_ROUNDED

    def test_the_dive_route_origin_is_degrees_where_the_sample_fixes_are_radians(self):
        """One export, two units. Run through `degrees_from_radians` like the fixes beside
        it, this origin would come out at 1 636 degrees and be dropped by `geo_fix`'s range
        check - so the bug would read as "this file has no entry", which is exactly what
        the file looked like before and would have hidden the regression completely.

        No `Latitude` key anywhere in this fixture, which also pins the early-out in
        `_positions`: a file carrying an origin and no sample fixes must not bail out
        before reading it.
        """
        content = _ocean_json([_ocean_origin(0, *self.OCEAN_ORIGIN_DEGREES), _ocean_depth(600, 30.0)])

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.entry_latitude, parsed.entry_longitude) == self.OCEAN_ORIGIN_ROUNDED
        assert math.degrees(self.OCEAN_ORIGIN_DEGREES[0]) > LATITUDE_LIMIT

    def test_a_null_island_origin_is_not_a_position(self):
        """Two files in the corpus write `0, 0` here, and `DiveRouteQuality` does not tell
        them apart from the good ones - it reads 1 on both. The coordinates do."""
        content = _ocean_json(
            [
                _ocean_origin(0, 0, 0),
                _ocean_depth(600, 30.0),
            ]
        )

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.entry_latitude, parsed.entry_longitude) == (None, None)

    def test_a_fix_taken_after_the_origin_but_before_the_descent_wins(self):
        """The origin is fed in as an ordinary fix, not assigned to the entry, so the
        module's one rule still decides across both channels: the last position before the
        descent is the entry, whichever channel it arrived on. Nothing in the corpus does
        this yet - a Garmin-style pre-descent fix would."""
        content = _ocean_json(
            [
                _ocean_origin(0, *self.OCEAN_ORIGIN_DEGREES),
                _ocean_fix(60, math.radians(28.2), math.radians(34.2)),
                _ocean_depth(600, 30.0),
            ]
        )

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.entry_latitude, parsed.entry_longitude) == (28.2, 34.2)

    def test_an_origin_sharing_the_pivots_timestamp_is_still_the_entry(self):
        """The tie `DiveRouteOrigin` made reachable. A depth channel whose readings are all
        equal pivots on its earliest sample, and the origin sits at exactly that instant -
        so with the split resolving ties towards the exit, the dive's *starting* position
        would have been written into the exit columns with the entry left empty."""
        content = _ocean_json(
            [
                _ocean_origin(0, *self.OCEAN_ORIGIN_DEGREES),
                _ocean_depth(0, 30.0),
                _ocean_depth(600, 30.0),
            ]
        )

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.entry_latitude, parsed.entry_longitude) == self.OCEAN_ORIGIN_ROUNDED
        assert (parsed.exit_latitude, parsed.exit_longitude) == (None, None)

    def test_a_sample_fix_outranks_an_origin_on_the_same_sample(self):
        """Two positions off one sample share a timestamp, and `entry_and_exit` separates
        equal timestamps by collection order - so which wins is decided by the order
        `_positions` appends them in, not by anything either value says. No corpus file
        writes both onto one sample; this pins the tie-break so a reorder cannot change it
        silently."""
        content = _ocean_json(
            [
                {
                    **_ocean_origin(0, *self.OCEAN_ORIGIN_DEGREES),
                    "Latitude": math.radians(28.2),
                    "Longitude": math.radians(34.2),
                },
                _ocean_depth(600, 30.0),
            ]
        )

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.entry_latitude, parsed.entry_longitude) == (28.2, 34.2)

    def test_a_malformed_origin_does_not_cost_the_fixes_beside_it(self):
        """Best-effort per sample, like the rest of this pass. An origin that is a string,
        or that carries half a pair, is skipped rather than taking the exit down with it.
        """
        for broken in ("not-an-object", {"Latitude": 28.5}, {}):
            content = _ocean_json(
                [
                    {"TimeISO8601": _ocean_time(0), "DiveRouteOrigin": broken},
                    _ocean_depth(600, 30.0),
                    _ocean_fix(4200, *self.OCEAN_SURFACED_RADIANS),
                ]
            )

            parsed = SuuntoJsonParser.parse(content)

            assert (parsed.entry_latitude, parsed.entry_longitude) == (None, None), broken
            assert (parsed.exit_latitude, parsed.exit_longitude) == self.OCEAN_SURFACED_ROUNDED, broken

    def test_json_fixes_are_ordered_by_their_own_timestamps_not_by_file_order(self):
        """A Suunto export's sample timestamps are not monotonic across channels - the
        sensor streams are appended out of order - so "the last one in the list" is not
        "the last one before the descent"."""
        content = _ocean_json(
            [
                _ocean_fix(60, math.radians(28.2), math.radians(34.2)),
                _ocean_fix(0, math.radians(28.1), math.radians(34.1)),
                _ocean_depth(600, 30.0),
            ]
        )

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.entry_latitude, parsed.entry_longitude) == (28.2, 34.2)

    def test_a_file_with_no_depth_channel_places_no_fix(self):
        """Nothing to pivot on. A position that cannot be told apart from its opposite is
        worth less than no position, since nothing downstream could ever discover it."""
        parsed = self._fit(message("record", timestamp=DIVE_START, position_lat=339272053, position_long=411111844))

        assert (parsed.entry_latitude, parsed.exit_latitude) == (None, None)

    def test_a_non_finite_depth_never_becomes_the_pivot(self):
        """`json.loads` accepts a bare `Infinity`, and an `inf` wins `max` outright - so
        the split would land on the surface sample it arrived on, putting a pre-descent
        fix in the exit columns with nothing downstream able to tell. A `NaN` breaks it
        the other way, winning whenever it is first, since every later `x > NaN` is
        `False`. The profile path dies loudly on the same reading; this one would not."""
        for bad in ("Infinity", "NaN"):
            content = _ocean_json(
                [
                    json.loads(f'{{"TimeISO8601": "{_ocean_time(0)}", "Depth": {bad}}}'),
                    _ocean_fix(60, math.radians(28.2), math.radians(34.2)),
                    _ocean_depth(600, 30.0),
                    _ocean_fix(1200, math.radians(28.9), math.radians(34.9)),
                ]
            )

            parsed = SuuntoJsonParser.parse(content)

            assert (parsed.entry_latitude, parsed.entry_longitude) == (28.2, 34.2), bad
            assert (parsed.exit_latitude, parsed.exit_longitude) == (28.9, 34.9), bad

    def test_one_unreadable_sample_does_not_discard_the_rest(self):
        """Skipped per sample rather than per file. A GPS-carrying export in this corpus
        yields exactly one usable position, so unwinding the whole pass on the first bad
        `TimeISO8601` would cost the entire feature for that dive."""
        content = _ocean_json(
            [
                {"TimeISO8601": "not-a-timestamp", "Latitude": 0.1, "Longitude": 0.2},
                _ocean_fix(0, *self.DAHAB_RADIANS),
                _ocean_depth(600, 30.0),
            ]
        )

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.entry_latitude, parsed.entry_longitude) == self.DAHAB_DEGREES

    def test_half_a_fix_is_not_a_fix(self):
        """A latitude with no longitude pins the dive to the prime meridian - which is
        also what `ck_dive_entry_position_pair` refuses, so this would fail the attach."""
        parsed = self._fit(
            self._fix_record(0, _semicircles(28.2), None),
            self._fix_record(600, None, None, depth=30.0),
        )

        assert (parsed.entry_latitude, parsed.entry_longitude) == (None, None)

    def test_null_island_never_displaces_a_real_fix(self):
        """A receiver with no lock reports the origin. Dropped at the fix rather than at
        the column, because `0.0, 0.0` is the *later* of these two and would otherwise be
        chosen as the entry and then nulled - costing a position the file did record."""
        parsed = self._fit(
            self._fix_record(0, *self.DAHAB_SEMICIRCLES),
            self._fix_record(60, 0, 0),
            self._fix_record(600, None, None, depth=30.0),
        )

        assert (parsed.entry_latitude, parsed.entry_longitude) == self.DAHAB_DEGREES

    def test_an_out_of_range_reading_never_displaces_a_real_fix(self):
        """Same rule, other cause: a latitude past the pole is not a reading, and taking
        it as the entry fix would lose the good one beside it as well as itself.

        A latitude, not a longitude, because a *semicircle* count cannot be out of range
        for a longitude - the unit spans exactly one circle, so 2^31 is 180 degrees and
        anything larger is not a `sint32`. Only the JSON export's radians can overshoot
        both, which is what `test_json_radians_read_as_degrees_would_be_out_of_range`
        covers.
        """
        parsed = self._fit(
            self._fix_record(0, *self.DAHAB_SEMICIRCLES),
            self._fix_record(60, _semicircles(150.0), _semicircles(34.2)),
            self._fix_record(600, None, None, depth=30.0),
        )

        assert (parsed.entry_latitude, parsed.entry_longitude) == self.DAHAB_DEGREES

    def test_json_radians_read_as_degrees_would_be_out_of_range(self):
        """The unit error this guards against, from the far side: a Suunto file's radians
        run to 2*pi, so a value that overshoots once converted was already nonsense."""
        content = _ocean_json([_ocean_fix(0, 1.6, 7.0), _ocean_depth(600, 30.0)])

        parsed = SuuntoJsonParser.parse(content)

        assert (parsed.entry_latitude, parsed.entry_longitude) == (None, None)

    def test_fit_reads_the_formats_own_absent_marker_as_no_fix(self):
        """0x7FFFFFFF is `sint32`'s invalid sentinel, and `fitdecode` resolves it to
        `None`. Read as a number it is 180.000000 degrees - out of range for a latitude,
        but a perfectly valid longitude that no bound would ever catch."""
        parsed = self._fit(
            self._fix_record(0, 0x7FFFFFFF, 0x7FFFFFFF),
            self._fix_record(600, None, None, depth=30.0),
        )

        assert (parsed.entry_latitude, parsed.entry_longitude) == (None, None)

    def test_xml_records_no_position_at_all(self):
        """No DM5 export in the 384-file corpus carries a coordinate anywhere."""
        parsed = SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode())

        assert (parsed.entry_latitude, parsed.entry_longitude) == (None, None)
        assert (parsed.exit_latitude, parsed.exit_longitude) == (None, None)

    def test_a_lone_ordinate_left_by_a_field_validator_takes_its_partner_with_it(self):
        """The one way a half pair can reach the schema: `_drop_non_finite` nulls a `NaN`
        latitude and leaves a perfectly good longitude behind. Both formats can express a
        non-finite float - `json.loads` accepts a bare `NaN` - and the surviving half
        would pin the dive to the equator."""
        parsed = ParsedDiveSchema(
            avg_depth=None,
            bottom_temperature=None,
            dive_number=None,
            duration=None,
            max_depth=None,
            start_time=None,
            mixtures=[],
            entry_latitude=float("nan"),
            entry_longitude=34.2,
        )

        assert (parsed.entry_latitude, parsed.entry_longitude) == (None, None)

    def test_the_coordinate_bounds_are_the_ones_the_database_enforces(self):
        constraints = _dive_constraints()

        assert f"{LATITUDE_LIMIT:g}" in constraints["ck_dive_entry_latitude_range"]
        assert f"{LATITUDE_LIMIT:g}" in constraints["ck_dive_exit_latitude_range"]
        assert f"{LONGITUDE_LIMIT:g}" in constraints["ck_dive_entry_longitude_range"]
        assert f"{LONGITUDE_LIMIT:g}" in constraints["ck_dive_exit_longitude_range"]

    def test_neither_position_can_be_half_stored(self):
        """The rule the schema drops a half pair for, stated by the database as well -
        see `test_dive_check_constraints.py` for it being exercised against a real one."""
        constraints = _dive_constraints()

        assert constraints["ck_dive_entry_position_pair"] == "(entry_latitude IS NULL) = (entry_longitude IS NULL)"
        assert constraints["ck_dive_exit_position_pair"] == "(exit_latitude IS NULL) = (exit_longitude IS NULL)"


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
