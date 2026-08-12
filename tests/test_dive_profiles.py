"""Unit tests for per-sample dive profile extraction, normalization and downsampling.

Inline fixtures and no database, in the style of `test_dive_parsers.py`: everything
worth testing here is either a parser reading bytes or a pure function reshaping arrays.
"""

import json
from datetime import timedelta

import pytest

from src.app.services.dive_parsers import _PARSERS, DiveParseError
from src.app.services.dive_parsers.fit import FitParser
from src.app.services.dive_parsers.suunto_json import SuuntoJsonParser
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser
from src.app.services.dive_profiles import (
    MAX_POINTS_PER_CHANNEL,
    ExistingProfileRow,
    NormalizedProfile,
    ProfilePressureSeries,
    ProfileSeries,
    downsample,
    extract_profile,
    normalize,
    should_extract,
)
from tests.helpers.fit import dive_fit_file
from tests.helpers.fit import message as fit_message
from tests.test_dive_parsers import (
    BILLION_LAUGHS_XML,
    SUUNTO_NS,
    VALID_FIT,
    VALID_SUUNTO_JSON,
    VALID_SUUNTO_XML,
    XSI_NS,
    XXE_XML,
)
from tests.test_dive_parsers import (
    DIVE_START as FIT_DIVE_START,
)
from tests.test_dive_parsers import (
    _records as _fit_records,
)


def _xml_with_samples(samples: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}" xmlns:i="{XSI_NS}">
  <MaxDepth>25.5</MaxDepth>
  <DiveSamples>{samples}</DiveSamples>
</Dive>
""".encode()


def _sample(time: int, *, depth: str | None = None, temperature: str | None = None, pressure: str | None = None) -> str:
    def element(tag: str, value: str | None) -> str:
        return f"<{tag}>{value}</{tag}>" if value is not None else f'<{tag} i:nil="true" />'

    return (
        "<Dive.Sample>"
        + element("Depth", depth)
        + element("Pressure", pressure)
        + element("Temperature", temperature)
        + f"<Time>{time}</Time>"
        + "</Dive.Sample>"
    )


def _json_with_samples(samples: list[dict], date_time: str = "2025-05-31T12:59:06.310+02:00") -> bytes:
    return json.dumps({"DeviceLog": {"Header": {"DateTime": date_time}, "Samples": samples}}).encode()


class TestSuuntoXmlParseProfile:
    def test_extracts_each_channel_as_its_own_series(self):
        content = _xml_with_samples(
            _sample(1, depth="1.24", temperature="26.0", pressure="205200")
            + _sample(11, depth="2.56", temperature="25.9", pressure="204760")
            + _sample(21, depth="3.11", temperature="25.7", pressure="204310")
        )

        profile = SuuntoXmlParser.parse_profile(content)

        assert profile is not None
        assert profile.depth.t == [1.0, 11.0, 21.0]
        # Centimeters, tenths of a degree, tenths of a bar - see `schemas/dive_profile.py`.
        assert profile.depth.v == [124, 256, 311]
        assert profile.temperature.v == [260, 259, 257]
        assert len(profile.pressure) == 1
        assert profile.pressure[0].gas_number == 1
        assert profile.pressure[0].v == [2052, 2048, 2043]

    def test_pressure_is_read_as_millibar(self):
        """`<Pressure>205200</Pressure>` is 205.2 bar, i.e. 2052 tenths - the same value
        the JSON twin of this dive reports as 20520000 Pascal."""
        content = _xml_with_samples(_sample(1, depth="1.24", pressure="205200"))

        profile = SuuntoXmlParser.parse_profile(content)

        assert profile.pressure[0].v == [2052]

    def test_scaling_goes_through_decimal_rather_than_float_multiplication(self):
        """25.85 C is 258.5 tenths, which rounds to 259.

        `round(25.85 * 10)` is 258: the binary product is really 258.49999999999997. This
        is the whole reason the parsers scale through `Decimal(str(value))`, and it would
        otherwise be wrong several thousand times per dive.
        """
        content = _xml_with_samples(_sample(1, depth="0.05", temperature="25.85"))

        profile = SuuntoXmlParser.parse_profile(content)

        assert profile.temperature.v == [259]
        assert profile.depth.v == [5]

    def test_omits_the_pressure_channel_when_every_reading_is_nil(self):
        """Every export from before the transmitter era looks like this."""
        content = _xml_with_samples(
            _sample(1, depth="1.24", temperature="26.0") + _sample(11, depth="2.56", temperature="25.9")
        )

        profile = SuuntoXmlParser.parse_profile(content)

        assert profile.pressure == []
        assert profile.depth is not None
        assert profile.temperature is not None

    def test_a_pressure_dropout_is_a_gap_in_that_channel_only(self):
        """Real: `Dive_2025-06-02-1155.xml` records pressure on 224 of 441 samples."""
        content = _xml_with_samples(
            _sample(1, depth="1.24", pressure="205200")
            + _sample(11, depth="2.56")
            + _sample(21, depth="3.11")
            + _sample(31, depth="3.61", pressure="203650")
        )

        profile = SuuntoXmlParser.parse_profile(content)

        assert profile.pressure[0].t == [1.0, 31.0]
        # 203650 mbar is 2036.5 tenths of a bar, rounded half *up* rather than to even.
        assert profile.pressure[0].v == [2052, 2037]
        # Depth is untouched by the other channel's dropout.
        assert profile.depth.t == [1.0, 11.0, 21.0, 31.0]

    def test_a_missing_depth_is_a_gap_not_a_null(self):
        content = _xml_with_samples(
            _sample(1, depth="1.24", temperature="26.0")
            + _sample(11, temperature="25.9")
            + _sample(21, depth="3.11", temperature="25.7")
        )

        profile = SuuntoXmlParser.parse_profile(content)

        assert profile.depth.t == [1.0, 21.0]
        assert profile.depth.v == [124, 311]
        assert None not in profile.depth.v
        # The channel that kept recording keeps all three.
        assert profile.temperature.t == [1.0, 11.0, 21.0]

    def test_returns_none_when_the_file_has_no_dive_samples_element(self):
        assert SuuntoXmlParser.parse_profile(VALID_SUUNTO_XML.encode()) is None

    def test_returns_none_when_dive_samples_is_empty(self):
        assert SuuntoXmlParser.parse_profile(_xml_with_samples("")) is None

    def test_returns_none_when_no_sample_carries_a_reading(self):
        assert SuuntoXmlParser.parse_profile(_xml_with_samples(_sample(1) + _sample(11))) is None

    def test_raises_dive_parse_error_on_a_non_numeric_reading(self):
        content = _xml_with_samples(_sample(1, depth="abc"))

        with pytest.raises(DiveParseError):
            SuuntoXmlParser.parse_profile(content)

    def test_raises_dive_parse_error_on_billion_laughs_entity_expansion(self):
        """`parse_profile` is a second entry point into XML parsing, so it needs its own
        proof that it goes through `defusedxml` - a guard is only as good as its coverage."""
        with pytest.raises(DiveParseError):
            SuuntoXmlParser.parse_profile(BILLION_LAUGHS_XML)

    def test_raises_dive_parse_error_on_xxe(self):
        with pytest.raises(DiveParseError):
            SuuntoXmlParser.parse_profile(XXE_XML)


class TestSuuntoJsonParseProfile:
    def test_extracts_each_channel_as_its_own_series(self):
        content = _json_with_samples(
            [
                {
                    "Depth": 1.24,
                    "Temperature": 299.15,
                    "Cylinders": [{"GasNumber": 1, "Pressure": 20520000}],
                    "TimeISO8601": "2025-05-31T12:59:07.250+02:00",
                },
                {
                    "Depth": 2.56,
                    "Temperature": 299.05,
                    "Cylinders": [{"GasNumber": 1, "Pressure": 20476000}],
                    "TimeISO8601": "2025-05-31T12:59:17.100+02:00",
                },
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert profile.depth.v == [124, 256]
        # 299.15 K is 26.0 C; 299.05 K is 25.9 C.
        assert profile.temperature.v == [260, 259]
        assert profile.pressure[0].gas_number == 1
        assert profile.pressure[0].v == [2052, 2048]
        # Seconds from `Header.DateTime`, fractional at this stage - `normalize` rounds.
        assert profile.depth.t == pytest.approx([0.94, 10.79])

    def test_skips_entries_that_carry_no_readings(self):
        """A Suunto Ocean export is mostly these: events, GPS fixes, battery telemetry."""
        content = _json_with_samples(
            [
                {
                    "Events": [{"State": {"Type": "Dive Active", "Active": True}}],
                    "TimeISO8601": "2025-05-31T12:59:06.310+02:00",
                },
                {"DiveRoute": 3, "TimeISO8601": "2025-05-31T12:59:08.000+02:00"},
                {"Depth": 1.24, "TimeISO8601": "2025-05-31T12:59:16.310+02:00"},
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert profile.depth.t == [10.0]
        assert profile.temperature is None

    def test_groups_cylinders_by_gas_number_and_drops_the_empty_slots(self):
        """A Suunto Ocean reports five slots on every sample with one populated."""
        content = _json_with_samples(
            [
                {
                    "Cylinders": [
                        {"GasNumber": 0, "Pressure": 20739062},
                        {"GasNumber": 1, "Pressure": None},
                        {"GasNumber": 2, "Pressure": None},
                    ],
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                },
                {
                    "Cylinders": [
                        {"GasNumber": 0, "Pressure": 20512000},
                        {"GasNumber": 1, "Pressure": None},
                        {"GasNumber": 2, "Pressure": None},
                    ],
                    "TimeISO8601": "2025-05-31T12:59:26.310+02:00",
                },
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert [cylinder.gas_number for cylinder in profile.pressure] == [0]
        assert profile.pressure[0].v == [2074, 2051]

    def test_keeps_several_populated_cylinders_apart(self):
        content = _json_with_samples(
            [
                {
                    "Cylinders": [
                        {"GasNumber": 0, "Pressure": 20000000},
                        {"GasNumber": 3, "Pressure": 15000000},
                    ],
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                }
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert {cylinder.gas_number: cylinder.v for cylinder in profile.pressure} == {0: [2000], 3: [1500]}

    def test_never_reads_device_internal_abs_pressure_as_tank_pressure(self):
        """It is the device's own ambient sensor (~96 400 Pa at the surface). Labelling it
        "tank pressure" on a chart divers plan gas from would be actively wrong."""
        content = _json_with_samples(
            [
                {
                    "Depth": 1.24,
                    "DeviceInternalAbsPressure": 96400,
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                }
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert profile.pressure == []

    def test_sorts_each_channel_despite_a_non_monotonic_union_axis(self):
        """A 2026-ocean export appends separate sensor streams out of order, so adjacent
        entries can go *backwards* in time by up to 0.7 s. That is not a parse error."""
        content = _json_with_samples(
            [
                {"Depth": 1.24, "TimeISO8601": "2025-05-31T12:59:16.310+02:00"},
                {"Temperature": 299.15, "TimeISO8601": "2025-05-31T12:59:16.900+02:00"},
                # Out of order relative to the entry above, but in order for its channel.
                {"Depth": 2.56, "TimeISO8601": "2025-05-31T12:59:16.500+02:00"},
                {"Temperature": 299.05, "TimeISO8601": "2025-05-31T12:59:17.900+02:00"},
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert profile.depth.t == sorted(profile.depth.t)
        assert profile.depth.v == [124, 256]
        assert profile.temperature.t == sorted(profile.temperature.t)

    def test_returns_none_for_a_header_only_export(self):
        assert SuuntoJsonParser.parse_profile(VALID_SUUNTO_JSON.encode()) is None

    def test_raises_dive_parse_error_on_malformed_json(self):
        with pytest.raises(DiveParseError):
            SuuntoJsonParser.parse_profile(b'{"DeviceLog": {')


class TestFitParseProfile:
    def test_extracts_depth_and_temperature_in_their_stored_scales(self):
        content = dive_fit_file(
            *_fit_records([(0, 1.45, 25), (10, 12.3, 24), (20, 45.91, 22)]),
        )
        profile = FitParser.parse_profile(content)

        assert profile.depth.t == [0.0, 10.0, 20.0]
        # Centimeters and tenths of a degree - see `schemas/dive_profile.py`.
        assert profile.depth.v == [145, 1230, 4591]
        assert profile.temperature.v == [250, 240, 220]

    def test_gives_each_channel_its_own_axis(self):
        """FIT channels are sampled independently: a Suunto Ocean dive writes 4 295
        records of which only 431 carry depth, while 4 294 carry temperature. A shared
        axis would be 90 % null in the depth column."""
        content = dive_fit_file(
            fit_message("record", timestamp=FIT_DIVE_START, depth=1.45),
            fit_message("record", timestamp=FIT_DIVE_START + timedelta(seconds=1), temperature=25),
            fit_message("record", timestamp=FIT_DIVE_START + timedelta(seconds=2), temperature=24),
        )
        profile = FitParser.parse_profile(content)

        assert profile.depth.t == [0.0]
        assert profile.temperature.t == [1.0, 2.0]

    def test_keeps_a_zero_depth_reading(self):
        """0.0 m is a real reading at the surface, not a missing one - a Suunto Ocean
        records it. It must survive the integer scaling rather than being treated as
        absent."""
        content = dive_fit_file(*_fit_records([(0, 0.0, 25), (10, 5.0, 25)]))
        profile = FitParser.parse_profile(content)

        assert profile.depth.v == [0, 500]

    def test_scales_depth_without_binary_floating_point_noise(self):
        """`fitdecode` divides a raw integer by the profile's scale factor in binary
        floating point, so 25.85 can arrive fractionally below itself and
        `round(25.85 * 100)` would land on 2584."""
        content = dive_fit_file(*_fit_records([(0, 25.85, 24)]))

        assert FitParser.parse_profile(content).depth.v == [2585]

    def test_rebases_samples_onto_the_session_start(self):
        content = dive_fit_file(*_fit_records([(30, 5.0, 25), (90, 12.0, 24)]))

        assert FitParser.parse_profile(content).depth.t == [30.0, 90.0]

    def test_extracts_garmin_transmitter_pressure(self):
        """`tank_update.pressure` is already bar - the FIT profile scales it - unlike the
        Pascal and millibar the two Suunto exports use."""
        content = dive_fit_file(
            fit_message("tank_update", timestamp=FIT_DIVE_START, sensor=2411100050, pressure=207.0),
            fit_message(
                "tank_update", timestamp=FIT_DIVE_START + timedelta(seconds=60), sensor=2411100050, pressure=198.5
            ),
        )
        profile = FitParser.parse_profile(content)

        assert len(profile.pressure) == 1
        assert profile.pressure[0].t == [0.0, 60.0]
        # Tenths of a bar.
        assert profile.pressure[0].v == [2070, 1985]

    def test_labels_cylinders_by_position_not_by_ant_sensor_id(self):
        """`sensor` is the pod's ANT serial (e.g. 2411100050). Same reasoning as the XML
        parser's refusal to use `<TransmitterId>`: it would read as nonsense in a chart
        legend, and it keeps a single-cylinder dive labelled gas 1 in both parsers."""
        content = dive_fit_file(
            fit_message("tank_update", timestamp=FIT_DIVE_START, sensor=2411100050, pressure=207.0),
            fit_message("tank_update", timestamp=FIT_DIVE_START, sensor=1900500123, pressure=180.0),
        )
        profile = FitParser.parse_profile(content)

        assert [cylinder.gas_number for cylinder in profile.pressure] == [1, 2]

    def test_returns_none_when_the_file_carries_no_samples(self):
        assert FitParser.parse_profile(dive_fit_file()) is None

    def test_raises_dive_parse_error_on_a_truncated_file(self):
        with pytest.raises(DiveParseError):
            FitParser.parse_profile(VALID_FIT[: len(VALID_FIT) // 2])


class TestParserRegistryProfileSupport:
    """`parse_profile` is non-abstract on purpose, so a new format can ship header-only
    and grow an extraction later. That freedom is only safe if every registered parser
    still answers the call rather than blowing up on its own valid fixture."""

    @pytest.mark.parametrize("parser", _PARSERS, ids=lambda parser: parser.key)
    def test_every_registered_parser_answers_parse_profile(self, parser):
        assert callable(parser.parse_profile)

    def test_neither_suunto_parser_raises_on_its_own_header_only_fixture(self):
        assert SuuntoXmlParser.parse_profile(VALID_SUUNTO_XML.encode()) is None
        assert SuuntoJsonParser.parse_profile(VALID_SUUNTO_JSON.encode()) is None


class TestNormalize:
    def test_rebases_every_channel_onto_a_shared_zero(self):
        """One origin for all channels, not one each: they share the chart's x axis, so
        shifting them independently would slide temperature off the depth curve."""
        parsed = SuuntoXmlParser.parse_profile(
            _xml_with_samples(
                _sample(1, depth="1.0", temperature="26.0") + _sample(11, depth="2.0") + _sample(21, temperature="25.0")
            )
        )

        profile = normalize(parsed)

        assert profile.depth.t == [0, 10]
        assert profile.temperature.t == [0, 20]

    def test_rounds_onto_integer_seconds_keeping_the_last_reading_per_second(self):
        parsed = SuuntoJsonParser.parse_profile(
            _json_with_samples(
                [
                    {"Temperature": 299.15, "TimeISO8601": "2025-05-31T12:59:06.310+02:00"},
                    {"Temperature": 299.05, "TimeISO8601": "2025-05-31T12:59:06.410+02:00"},
                    {"Temperature": 298.95, "TimeISO8601": "2025-05-31T12:59:07.310+02:00"},
                ]
            )
        )

        profile = normalize(parsed)

        assert profile.temperature.t == [0, 1]
        assert profile.temperature.v == [259, 258]

    def test_returns_none_for_a_profile_with_no_readings(self):
        from src.app.schemas.dive_profile import ParsedProfileSchema

        assert normalize(ParsedProfileSchema()) is None

    def test_summary_properties_describe_the_recorded_span(self):
        parsed = SuuntoXmlParser.parse_profile(
            _xml_with_samples(
                _sample(1, depth="1.0", temperature="26.0", pressure="205200")
                + _sample(101, depth="2.0", temperature="25.0", pressure="204000")
            )
        )

        profile = normalize(parsed)

        assert profile.duration_seconds == 100
        assert profile.depth_sample_count == 2
        assert profile.channels == ["depth", "temperature", "pressure"]


class TestDownsample:
    def _sawtooth(self, count: int) -> ProfileSeries:
        # A shape with its extremes buried in the middle, so a naive "every nth sample"
        # thinning would miss them.
        return ProfileSeries(
            t=list(range(count)),
            v=[(index * 37) % 500 + (10_000 if index == count // 3 else 0) for index in range(count)],
        )

    def test_returns_a_channel_under_the_cap_unchanged(self):
        series = self._sawtooth(50)
        profile = downsample(NormalizedProfile(depth=series), max_points=1200)

        assert profile.depth.t == series.t
        assert profile.depth.v == series.v

    def test_preserves_the_extremes_exactly(self):
        """The one thing a depth profile must never lose is its deepest sample - which on
        a real dive can be a single point. Min/max bucketing guarantees it; LTTB doesn't."""
        series = self._sawtooth(9_000)
        profile = downsample(NormalizedProfile(depth=series), max_points=1200)

        assert max(profile.depth.v) == max(series.v)
        assert min(profile.depth.v) == min(series.v)

    def test_stays_within_the_cap(self):
        series = self._sawtooth(9_000)
        profile = downsample(NormalizedProfile(depth=series), max_points=1200)

        assert len(profile.depth.t) <= 1200
        assert len(profile.depth.t) == len(profile.depth.v)

    def test_keeps_time_increasing(self):
        series = self._sawtooth(9_000)
        profile = downsample(NormalizedProfile(depth=series), max_points=1200)

        assert profile.depth.t == sorted(profile.depth.t)
        assert len(set(profile.depth.t)) == len(profile.depth.t)

    def test_buckets_on_time_so_an_irregular_axis_is_not_unevenly_weighted(self):
        # Half the samples crammed into the first tenth of the dive.
        series = ProfileSeries(
            t=list(range(100)) + [1_000 + index * 100 for index in range(100)],
            v=list(range(200)),
        )
        profile = downsample(NormalizedProfile(depth=series), max_points=20)

        assert len(profile.depth.t) <= 20
        # The sparse tail survives rather than being crowded out by the dense head.
        assert max(profile.depth.t) == series.t[-1]

    def test_caps_every_channel_independently(self):
        profile = downsample(
            NormalizedProfile(
                depth=self._sawtooth(300),
                temperature=self._sawtooth(9_000),
                pressure=[ProfilePressureSeries(gas_number=1, t=self._sawtooth(9_000).t, v=self._sawtooth(9_000).v)],
            ),
            max_points=1200,
        )

        assert len(profile.depth.t) == 300
        assert len(profile.temperature.t) <= 1200
        assert len(profile.pressure[0].t) <= 1200
        assert profile.pressure[0].gas_number == 1

    def test_the_real_cap_is_the_module_default(self):
        assert MAX_POINTS_PER_CHANNEL == 1200


class TestShouldExtract:
    @pytest.mark.parametrize(
        ("existing", "sha256", "version", "expected"),
        [
            (None, "abc", 1, "extract"),
            (ExistingProfileRow(source_sha256="abc", extractor_version=1), "abc", 1, "skip"),
            (ExistingProfileRow(source_sha256="def", extractor_version=1), "abc", 1, "extract"),
            (ExistingProfileRow(source_sha256="abc", extractor_version=1), "abc", 2, "extract"),
            # A row written by a *newer* extractor than this build is also "not current",
            # and re-extracting is the honest answer - it is what this build can vouch for.
            (ExistingProfileRow(source_sha256="abc", extractor_version=3), "abc", 2, "extract"),
        ],
    )
    def test_table(self, existing, sha256, version, expected):
        assert should_extract(existing, sha256=sha256, version=version) == expected


class TestExtractProfile:
    def test_swallows_a_parse_error_and_returns_none(self):
        """A failed extraction must not fail the upload it rode in on: the file is the
        durable artifact and can be re-extracted once the extractor is fixed."""
        assert extract_profile(SuuntoXmlParser, _xml_with_samples(_sample(1, depth="abc"))) is None

    def test_returns_none_for_a_file_with_no_samples(self):
        assert extract_profile(SuuntoXmlParser, VALID_SUUNTO_XML.encode()) is None

    def test_returns_a_capped_normalized_profile(self):
        content = _xml_with_samples(
            "".join(_sample(1 + index * 10, depth=f"{index / 10:.1f}", pressure="205200") for index in range(5))
        )

        profile = extract_profile(SuuntoXmlParser, content)

        assert profile.depth.t == [0, 10, 20, 30, 40]
        assert profile.pressure[0].v == [2052] * 5

    def test_the_same_dive_exported_as_xml_and_json_agrees_with_itself(self):
        """The corpus has this pair for real (`Dive_2025-05-31-1259.xml` /
        `685013accbecd72812f3d840.json`); this is the shape of that check, inline."""
        xml = extract_profile(
            SuuntoXmlParser, _xml_with_samples(_sample(1, depth="1.24", temperature="26.0", pressure="205200"))
        )
        js = extract_profile(
            SuuntoJsonParser,
            _json_with_samples(
                [
                    {
                        "Depth": 1.24,
                        "Temperature": 299.15,
                        "Cylinders": [{"GasNumber": 1, "Pressure": 20520000}],
                        "TimeISO8601": "2025-05-31T12:59:07.250+02:00",
                    }
                ]
            ),
        )

        assert xml.depth.v == js.depth.v
        assert xml.temperature.v == js.temperature.v
        assert xml.pressure[0].v == js.pressure[0].v
        assert xml.pressure[0].gas_number == js.pressure[0].gas_number


class TestToData:
    def test_omits_absent_channels_rather_than_writing_nulls(self):
        data = NormalizedProfile(depth=ProfileSeries(t=[0, 1], v=[10, 20])).to_data()

        assert data == {"depth": {"t": [0, 1], "v": [10, 20]}}
        assert "temperature" not in data
        assert "pressure" not in data
