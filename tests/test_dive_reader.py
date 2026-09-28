"""The one reader, and the dive form's projection of what it reads.

No database: `read_dive_file` and `prefill` are pure functions of the bytes, and the files
are real ones - the package's own fixtures, copied under `tests/fixtures/dive_files/` (see the
README there for which were recorded whole and which were built by hand).
"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import divejson
import pytest
from divejson import Conversion, Issue, NonConformingOutputError, SourceTooLargeError

from src.app.schemas.dive import DiveMode
from src.app.services import dive_reader
from src.app.services.dive_reader import (
    CONVERTER_BUG,
    DiveFileReadError,
    UnsupportedDiveFileError,
    prefill,
    read_dive_file,
    read_prefill,
    shape,
    start_of,
)
from src.app.services.export.uddf import write_uddf
from tests.helpers.dive_files import suunto_json
from tests.helpers.export import EXPORTED_AT, UUIDS, build_bundle, make_dive
from tests.helpers.fit import dive_fit_file
from tests.helpers.fit import message as fit_message

FIXTURES = Path(__file__).parent / "fixtures" / "dive_files"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


async def _one_dive_uddf(**dive: object) -> bytes:
    """A UDDF of one dive as this app's own writer produces it - the file a diver re-picks on
    the form after exporting a single dive from somewhere else."""
    bundle = build_bundle(dives=[make_dive(1, UUIDS["dive-air"], **dive)])
    return b"".join([chunk async for chunk in write_uddf(AsyncMock(), bundle, exported_at=EXPORTED_AT)])


class TestTheReader:
    def test_the_head_decides_the_format(self) -> None:
        """The reader takes bytes and no name - the route's test sends a Suunto export
        named `.txt` to say the same thing from outside."""
        assert read_dive_file(_fixture("suunto-d5.json")).format == "suunto_json"
        assert read_dive_file(_fixture("nitrox-deco.xml")).format == "suunto_xml"
        assert read_dive_file(_fixture("suunto-ocean-2026.fit")).format == "fit"

    def test_a_named_format_skips_the_sniff(self) -> None:
        """A stored file is re-read as the format it was admitted under, not sniffed again."""
        with pytest.raises(DiveFileReadError, match="could not be read"):
            read_dive_file(_fixture("suunto-d5.json"), format="fit")

    def test_the_dive_carries_its_one_recording(self) -> None:
        read = read_dive_file(_fixture("suunto-ocean-2026.json"))

        assert read.recording is not None
        assert read.recording.device is not None and read.recording.device.name == "Porvoo"
        assert read.started_at == "2026-09-08T15:17:38.670+03:00"

    @pytest.mark.asyncio
    async def test_a_dive_the_document_gives_no_recording_is_still_one_dive(self) -> None:
        """A logbook entry with no computer behind it: the form prefills from it, and the
        attach's recording holds the file."""
        read = read_dive_file(await _one_dive_uddf(dive_number=8))

        assert read.format == "uddf"
        assert read.recording is None
        assert shape(read) is None
        assert start_of(read, None) == (datetime(2026, 6, 1, 6, 15, tzinfo=UTC), 120)

    def test_a_file_past_the_readers_own_bound_reads_as_an_activity_log(self, monkeypatch) -> None:
        """Under the upload cap and past what the reader will decode for one file - which no
        single dive reaches and a watch's activity log does. A 413 would tell the diver to
        shrink a file that is already small enough."""

        def too_large(*args: object, **kwargs: object) -> Conversion:
            raise SourceTooLargeError("the FIT file holds more than 100,000 messages")

        monkeypatch.setattr(divejson, "convert", too_large)

        with pytest.raises(DiveFileReadError, match="activity log rather than a dive"):
            read_dive_file(_fixture("suunto-ocean-2026.fit"))

    def test_a_non_conforming_conversion_is_the_converters_bug(self, monkeypatch) -> None:
        def non_conforming(*args: object, **kwargs: object) -> Conversion:
            issue = Issue(path="dives[0].duration", message="-1 is less than the minimum of 0")
            raise NonConformingOutputError([issue])

        monkeypatch.setattr(divejson, "convert", non_conforming)

        with pytest.raises(DiveFileReadError) as refusal:
            read_dive_file(_fixture("suunto-d5.json"))
        assert str(refusal.value) == CONVERTER_BUG

    def test_a_document_this_app_cannot_read_is_the_converters_bug_too(self, monkeypatch) -> None:
        real = divejson.convert

        def unreadable(source: bytes, **kwargs: Any) -> Conversion:
            conversion = real(source, **kwargs)
            conversion.document["dives"][0]["recordings"][0]["device"]["serial"] = "x" * 65
            return conversion

        monkeypatch.setattr(divejson, "convert", unreadable)

        with pytest.raises(DiveFileReadError) as refusal:
            read_dive_file(_fixture("suunto-ocean-2026.json"))
        assert str(refusal.value) == CONVERTER_BUG

    def test_a_decoder_failing_outside_its_own_errors_is_still_a_422(self, monkeypatch) -> None:
        """The package promises every failure is a `ConverterError`, over bytes a stranger
        supplied. The endpoint must not 500 on a corrupt upload whatever a decoder does."""

        def exploding(*args: object, **kwargs: object) -> Conversion:
            raise AssertionError

        monkeypatch.setattr(divejson, "convert", exploding)

        with pytest.raises(DiveFileReadError, match="AssertionError"):
            read_dive_file(_fixture("suunto-d5.json"))

    def test_an_archive_is_refused_before_anything_converts(self) -> None:
        with pytest.raises(UnsupportedDiveFileError, match="one dive-computer file"):
            read_dive_file(b"PK\x03\x04" + b"\x00" * 64)


class TestThePrefill:
    """`POST /dive/parse`'s values for each file: the document's dive, as the form shows it."""

    def test_the_d5_json_rounds_its_pressures_to_the_forms_two_decimals(self) -> None:
        _, parsed = read_prefill(_fixture("suunto-d5.json"))

        assert [(mixture.start_pressure, mixture.end_pressure) for mixture in parsed.mixtures] == [
            (207.14, 122.44),
            (None, None),
        ]
        # The readouts ride the same rule, the form's preview being where they show.
        assert (parsed.otu_start, parsed.otu_end) == (23.1, 45.13)

    def test_the_rounding_is_of_the_decimal_the_file_wrote_not_of_the_binary_float(self) -> None:
        """200.675 bar is 200.674999... as a float, which `round` would take to 200.67."""
        content = suunto_json(gases=[{"Oxygen": 0.21, "Helium": 0, "StartPressure": 20067500}])

        assert read_prefill(content)[1].mixtures[0].start_pressure == 200.68

    def test_the_dm5_xml_rounds_its_pressures_the_same_way(self) -> None:
        _, parsed = read_prefill(_fixture("nitrox-deco.xml"))

        assert (parsed.mixtures[0].start_pressure, parsed.mixtures[0].end_pressure) == (211.39, 144.62)

    def test_the_cylinders_carry_the_readers_labels(self) -> None:
        """From 0 in document order, and only where a channel or a switch points at one - so
        a FIT with no transmitter labels nothing."""
        assert [row.gas_number for row in read_prefill(_fixture("suunto-d5.json"))[1].mixtures] == [0, 1]
        assert [row.gas_number for row in read_prefill(_fixture("suunto-ocean-2026.json"))[1].mixtures] == [0]
        assert [row.gas_number for row in read_prefill(_fixture("suunto-ocean-2026.fit"))[1].mixtures] == [None]

    def test_the_ocean_json_keeps_its_in_water_time_as_the_duration(self) -> None:
        """`DiveTime`'s 3 051 s, not the longer `Duration` beside it - and the dive's duration
        stays the document's, where the recording's gate figures are the samples'."""
        assert read_prefill(_fixture("suunto-ocean-2026.json"))[1].duration == 3051

    def test_a_stated_bottom_temperature_is_the_documents(self) -> None:
        assert read_prefill(_fixture("nitrox-deco.xml"))[1].bottom_temperature == 25.0

    @pytest.mark.parametrize(
        ("name", "coldest"),
        [
            ("suunto-d5.json", 18.2),
            ("suunto-ocean-2026.json", 29.2),
            ("suunto-ocean-2026.fit", 28.0),
        ],
    )
    def test_an_unstated_bottom_temperature_is_the_coldest_sample(self, name: str, coldest: float) -> None:
        """No JSON or FIT the package reads states one, and a form default is not a reading
        the converter should make up."""
        assert read_prefill(_fixture(name))[1].bottom_temperature == coldest

    def test_positions_round_to_six_places(self) -> None:
        _, parsed = read_prefill(_fixture("suunto-ocean-2026.json"))

        assert (parsed.entry_latitude, parsed.entry_longitude) == (28.496525, 34.5168)
        assert (parsed.exit_latitude, parsed.exit_longitude) == (28.496523, 34.516778)

    def test_depths_pass_as_the_document_writes_them(self) -> None:
        _, parsed = read_prefill(suunto_json(max_depth=21.8000011))

        assert parsed.max_depth == 21.8000011

    @pytest.mark.parametrize("name", ["suunto-d5.json", "nitrox-deco.xml", "suunto-ocean-2026.fit"])
    def test_no_dive_computer_format_states_the_divers_dive_number(self, name: str) -> None:
        """A device's counter lands on the recording's device, never on the dive."""
        _, parsed = read_prefill(_fixture(name))

        assert parsed.dive_number is None

    def test_the_fits_counter_is_the_devices(self) -> None:
        _, parsed = read_prefill(_fixture("suunto-ocean-2026.fit"))

        assert parsed.device is not None and parsed.device.dive_number == 3

    @pytest.mark.asyncio
    async def test_a_one_dive_uddf_states_the_divers_own_number(self) -> None:
        read = read_dive_file(await _one_dive_uddf(dive_number=8))

        assert prefill(read, shape(read)).dive_number == 8

    def test_the_recordings_device_and_settings_come_through(self) -> None:
        _, parsed = read_prefill(_fixture("suunto-d5.json"))

        assert parsed.device is not None
        assert (parsed.device.brand, parsed.device.name, parsed.device.firmware) == ("Suunto", "Suunto D5", "3.0.2143")
        assert parsed.mode is DiveMode.OPEN_CIRCUIT
        assert parsed.deco_model is not None and parsed.deco_model.name == "Suunto Fused RGBM 2"
        assert parsed.surface_pressure_bar == 1.049

    def test_a_fit_transmitter_labels_its_cylinder_and_its_channel_alike(self) -> None:
        """No recorded FIT carries tank telemetry, so the file is written: a Garmin pod's
        readings and its summary. The reader lists the cylinder and labels it `0`, and the
        pressure channel names the same label - the join the form and the chart share."""
        start = datetime(2026, 4, 17, 9, 49, 23, tzinfo=UTC)
        content = dive_fit_file(
            *(
                fit_message("record", timestamp=start + timedelta(seconds=s), depth=5.0 + s / 10)
                for s in range(0, 50, 10)
            ),
            *(
                fit_message("tank_update", timestamp=start + timedelta(seconds=s), sensor=12345, pressure=200.0 - s)
                for s in range(0, 50, 10)
            ),
            fit_message(
                "tank_summary",
                timestamp=start + timedelta(seconds=50),
                sensor=12345,
                start_pressure=200.0,
                end_pressure=150.0,
                volume_used=100.0,
            ),
        )

        read = read_dive_file(content)
        shaped = shape(read)

        assert [(row.gas_number, row.start_pressure, row.end_pressure) for row in prefill(read, shaped).mixtures] == [
            (0, 200.0, 150.0)
        ]
        assert shaped is not None and shaped.profile is not None
        assert [series.gas_number for series in shaped.profile.pressure] == [0]

    def test_a_cylinder_the_file_records_a_zero_end_for_arrives_empty(self) -> None:
        """A zero pressure is a file's absent-marker, and the form's pressure band drops it
        rather than prefilling a value its own save would refuse."""
        content = suunto_json(gases=[{"Oxygen": 0.32, "StartPressure": 0, "EndPressure": 0}])

        assert [(row.start_pressure, row.end_pressure) for row in read_prefill(content)[1].mixtures] == [(None, None)]


class TestTheTables:
    def test_the_formats_this_build_reads_are_named_in_the_readers_order(self) -> None:
        assert dive_reader.formats_this_build_reads() == ", ".join(
            dive_reader.FORMAT_LABELS[fmt] for fmt in divejson.read_formats()
        )

    def test_a_format_nothing_names_is_served_as_opaque_bytes(self) -> None:
        assert dive_reader.content_type_of("fit") == "application/vnd.ant.fit"
        assert dive_reader.content_type_of("divejson_import") == dive_reader.FALLBACK_CONTENT_TYPE
        assert dive_reader.content_type_of(None) == dive_reader.FALLBACK_CONTENT_TYPE


class TestNoParserSurvives:
    def test_nothing_under_src_imports_a_parser_of_its_own(self) -> None:
        """The deletion is the point: one reader, so no module under `src/` may import the
        app's own parsers or the XML hardening only they needed. `fitdecode` stays loaded -
        the package imports it at module scope for its FIT reader.

        In a subprocess, because this suite's own helpers import `fitdecode` to write FIT
        fixtures, and a module another test imported would say nothing about the app.
        """
        probe = (
            "import json, sys; import app.main, app.api.v1.dives, app.api.v1.logbook_import; "
            "print(json.dumps(sorted(name for name in sys.modules if name.split('.')[0] in "
            "('defusedxml', 'fitdecode') or 'dive_parsers' in name)))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=Path(__file__).parent.parent / "src",
            capture_output=True,
            text=True,
            check=True,
        )
        loaded = json.loads(result.stdout.strip().splitlines()[-1])

        assert not [name for name in loaded if "dive_parsers" in name or name.startswith("defusedxml")]
        assert "fitdecode" in loaded
