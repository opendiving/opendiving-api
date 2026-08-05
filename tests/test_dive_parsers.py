"""Unit tests for dive-computer export file parsers."""

import pytest

from src.app.services.dive_parsers import DiveParseError, UnsupportedDiveFileError, parse_dive_file
from src.app.services.dive_parsers.suunto import SuuntoXmlParser

SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

VALID_SUUNTO_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}" xmlns:i="{XSI_NS}">
  <Algorithm>2</Algorithm>
  <AvgDepth>12.3</AvgDepth>
  <Duration>1800</Duration>
  <MaxDepth>25.5</MaxDepth>
  <SerialNumber i:nil="true" />
  <Software>DM5</Software>
  <StartTime>2024-05-01T09:00:00</StartTime>
  <DiveMixtures>
    <DiveMixture>
      <EndPressure>50</EndPressure>
      <Helium>0</Helium>
      <Name>Air</Name>
      <Oxygen>21</Oxygen>
      <PO2>1.4</PO2>
      <Size>12</Size>
      <StartPressure>200</StartPressure>
      <TransmitterId i:nil="true" />
      <Type>0</Type>
      <DiveGasChanges>
        <DiveGasChange>
          <GasChangeTime>0</GasChangeTime>
          <PO2>1.4</PO2>
          <SetPointType>0</SetPointType>
        </DiveGasChange>
      </DiveGasChanges>
    </DiveMixture>
  </DiveMixtures>
  <DiveSamples>
    <Dive.Sample>
      <Time>0</Time>
      <Depth>0.0</Depth>
      <Temperature>28.0</Temperature>
    </Dive.Sample>
    <Dive.Sample>
      <Time>60</Time>
      <Depth>10.5</Depth>
      <Temperature>27.0</Temperature>
    </Dive.Sample>
  </DiveSamples>
</Dive>
"""

NOT_A_DIVE_XML = """<?xml version="1.0" encoding="utf-8"?>
<SomethingElse></SomethingElse>
"""

MALFORMED_XML = b"<Dive><Unclosed>"

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


class TestSuuntoXmlParserCanParse:
    """`can_parse` is a cheap filename check only; it never inspects XML content."""

    def test_recognizes_xml_extension(self):
        assert SuuntoXmlParser.can_parse("export.xml", VALID_SUUNTO_XML.encode()) is True

    def test_rejects_non_xml_extension(self):
        assert SuuntoXmlParser.can_parse("export.txt", VALID_SUUNTO_XML.encode()) is False

    def test_extension_check_is_case_insensitive(self):
        assert SuuntoXmlParser.can_parse("EXPORT.XML", VALID_SUUNTO_XML.encode()) is True


class TestSuuntoXmlParserParse:
    def test_parses_top_level_fields(self):
        parsed = SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode())

        assert parsed.algorithm == 2
        assert parsed.avg_depth == 12.3
        assert parsed.duration == 1800
        assert parsed.max_depth == 25.5
        assert parsed.serial_number is None
        assert parsed.software == "DM5"
        assert parsed.start_time == "2024-05-01T09:00:00"

    def test_parses_mixtures(self):
        parsed = SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode())

        assert len(parsed.mixtures) == 1
        mixture = parsed.mixtures[0]
        assert mixture.name == "Air"
        assert mixture.oxygen == 21
        assert mixture.helium == 0
        assert mixture.transmitter_id is None
        assert len(mixture.gas_changes) == 1
        assert mixture.gas_changes[0].po2 == 1.4

    def test_parses_samples(self):
        parsed = SuuntoXmlParser.parse(VALID_SUUNTO_XML.encode())

        assert len(parsed.samples) == 2
        assert parsed.samples[0].time == 0
        assert parsed.samples[0].depth == 0.0
        assert parsed.samples[1].time == 60
        assert parsed.samples[1].depth == 10.5

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


class TestParseDiveFile:
    def test_dispatches_to_suunto_parser(self):
        parsed = parse_dive_file("export.xml", VALID_SUUNTO_XML.encode())

        assert parsed.max_depth == 25.5

    def test_raises_unsupported_for_unrecognized_file(self):
        with pytest.raises(UnsupportedDiveFileError):
            parse_dive_file("export.csv", b"time,depth\n0,0\n")

    def test_raises_unsupported_for_xml_with_unknown_root(self):
        with pytest.raises(UnsupportedDiveFileError):
            parse_dive_file("export.xml", NOT_A_DIVE_XML.encode())
