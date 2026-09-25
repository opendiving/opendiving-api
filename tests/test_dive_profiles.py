"""Unit tests for per-sample dive profile extraction, normalization and downsampling.

Inline fixtures and no database, in the style of `test_dive_parsers.py`: everything
worth testing here is either a parser reading bytes or a pure function reshaping arrays.
"""

import json
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.app.schemas.dive_profile import (
    MILLISECONDS_PER_SECOND,
    GasAttribution,
    ParsedPressureSeries,
    ParsedProfileEvent,
    ParsedProfileSchema,
    ParsedSeries,
    ProfileEventType,
    ProfileProvenance,
)
from src.app.services.dive_parsers import _PARSERS, DiveParseError
from src.app.services.dive_parsers.fit import FitParser
from src.app.services.dive_parsers.suunto_json import SuuntoJsonParser
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser
from src.app.services.dive_profiles import (
    _PROVENANCE_BY_PARSER_KEY,
    IMPORT_PARSER_KEY,
    MAX_EVENTS,
    MAX_LABEL_CHARS,
    MAX_POINTS_PER_CHANNEL,
    MERGE_PARSER_KEY,
    UNREPRODUCIBLE_PROVENANCES,
    ExistingProfileRow,
    LoadedProfile,
    NormalizedProfile,
    ProfileEvent,
    ProfileGasAttribution,
    ProfilePressureSeries,
    ProfileSeries,
    derive_gas_attribution,
    downsample,
    extract_profile,
    fill_channels,
    finalize_profile,
    get_gas_attribution_for_dives,
    join_profiles,
    normalize,
    profile_from_data,
    provenance_of,
    shift_profile,
    should_extract,
    summary_extremes,
    to_read_schema,
    to_recording_read_schema,
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


def _xml_with_samples(samples: str, mixtures: str = "") -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}" xmlns:i="{XSI_NS}">
  <MaxDepth>25.5</MaxDepth>
  <DiveMixtures>{mixtures}</DiveMixtures>
  <DiveSamples>{samples}</DiveSamples>
</Dive>
""".encode()


def _mixture(*gas_change_times: int, oxygen: str = "21") -> str:
    """One `<DiveMixture>`, with its gas changes nested inside it as the format keeps them."""
    changes = "".join(
        f"<DiveGasChange><GasChangeTime>{time}</GasChangeTime></DiveGasChange>" for time in gas_change_times
    )
    return f"<DiveMixture><DiveGasChanges>{changes}</DiveGasChanges><Oxygen>{oxygen}</Oxygen></DiveMixture>"


def _sample(
    time: int,
    *,
    depth: str | None = None,
    temperature: str | None = None,
    pressure: str | None = None,
    ceiling: str | None = None,
) -> str:
    def element(tag: str, value: str | None) -> str:
        return f"<{tag}>{value}</{tag}>" if value is not None else f'<{tag} i:nil="true" />'

    return (
        "<Dive.Sample>"
        + element("Ceiling", ceiling)
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

    def test_extracts_the_ceiling_channel_in_centimeters(self):
        """The same scale as depth, deliberately: the ceiling is drawn against the depth
        axis, so 3 m of ceiling has to be the same number as 3 m of depth."""
        content = _xml_with_samples(
            _sample(1, depth="12.4", ceiling="3") + _sample(11, depth="9.0", ceiling="3.02") + _sample(21, depth="4.0")
        )

        profile = SuuntoXmlParser.parse_profile(content)

        assert profile.ceiling.t == [1.0, 11.0]
        assert profile.ceiling.v == [300, 302]
        # The stretch with no obligation is a gap in the channel, not a zero - and depth
        # keeps all three readings.
        assert profile.depth.t == [1.0, 11.0, 21.0]

    def test_omits_the_ceiling_channel_when_the_dive_owed_no_stop(self):
        """373 of the corpus's 384 exports, which is every no-deco dive in it."""
        content = _xml_with_samples(_sample(1, depth="12.4") + _sample(11, depth="9.0"))

        profile = SuuntoXmlParser.parse_profile(content)

        assert profile.ceiling is None

    def test_a_zero_ceiling_is_no_ceiling(self):
        """This format writes `xsi:nil` rather than a zero, so this case is unattested
        here - but the rule is shared with the JSON export, which writes `0` for the same
        fact, and the two must not disagree about the same dive."""
        content = _xml_with_samples(_sample(1, depth="12.4", ceiling="0"))

        assert SuuntoXmlParser.parse_profile(content).ceiling is None

    def test_gas_switches_are_numbered_by_the_cylinder_they_are_nested_in(self):
        """`<DiveGasChanges>` sits *inside* each `<DiveMixture>`, so the switch and the
        cylinder need no join - the real two-gas dive is `Dive_2025-06-03-1215.xml`."""
        content = _xml_with_samples(
            _sample(1, depth="12.4"), mixtures=_mixture(0, oxygen="21") + _mixture(2356, oxygen="49")
        )

        events = SuuntoXmlParser.parse_profile(content).events

        assert [(event.t, event.type, event.gas_number) for event in events] == [
            (0.0, ProfileEventType.GAS_SWITCH, 1),
            (2356.0, ProfileEventType.GAS_SWITCH, 2),
        ]

    def test_marks_are_not_read_as_events(self):
        """`<Type>` is an undocumented numeric code - 29 distinct values across the corpus,
        two of which appear in all 384 files. Mapping them onto stops or bookmarks would be
        the `<Type>`-as-gas-role mistake again."""
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}" xmlns:i="{XSI_NS}">
  <DiveSamples>{_sample(1, depth="12.4")}</DiveSamples>
  <Marks>
    <Mark><Heading i:nil="true" /><MarkTime>949</MarkTime><Type>268</Type></Mark>
    <Mark><Heading i:nil="true" /><MarkTime>2148</MarkTime><Type>266</Type></Mark>
  </Marks>
</Dive>
""".encode()

        assert SuuntoXmlParser.parse_profile(content).events == []

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

    def test_a_zero_ceiling_is_no_ceiling(self):
        """**The difference that makes `ceiling_cm` necessary.** This export writes
        `"Ceiling": 0` on every no-deco sample where the DM5 XML of the same dive writes
        `xsi:nil`. Read as a reading, one file would give a dive a ceiling channel flat
        along the surface and its twin none at all."""
        content = _json_with_samples(
            [
                {"Depth": 12.4, "Ceiling": 0, "TimeISO8601": "2025-05-31T12:59:16.310+02:00"},
                {"Depth": 9.0, "Ceiling": 0, "TimeISO8601": "2025-05-31T12:59:26.310+02:00"},
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert profile.ceiling is None
        assert profile.depth is not None

    def test_extracts_the_ceiling_channel_where_the_dive_owed_a_stop(self):
        content = _json_with_samples(
            [
                {"Depth": 30.0, "Ceiling": 0, "TimeISO8601": "2025-05-31T12:59:16.310+02:00"},
                {"Depth": 24.0, "Ceiling": 3, "TimeISO8601": "2025-05-31T12:59:26.310+02:00"},
                {"Depth": 12.0, "Ceiling": 6.05, "TimeISO8601": "2025-05-31T12:59:36.310+02:00"},
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert profile.ceiling.v == [300, 605]
        # Only the samples that had one - the no-obligation head of the dive is a gap.
        assert profile.ceiling.t == pytest.approx([20.0, 30.0])

    def test_reads_gas_switches_from_either_event_key(self):
        """The D5 shapes write `Events`; the 2026 Ocean writes `DiveEvents`."""
        content = _json_with_samples(
            [
                {
                    "Depth": 1.0,
                    "Events": [{"GasSwitch": {"GasNumber": 1}}],
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                },
                {
                    "Depth": 30.0,
                    "DiveEvents": [{"GasSwitch": {"GasNumber": 0}}],
                    "TimeISO8601": "2025-05-31T12:59:26.310+02:00",
                },
            ]
        )

        events = SuuntoJsonParser.parse_profile(content).events

        assert [(event.type, event.gas_number) for event in events] == [
            (ProfileEventType.GAS_SWITCH, 1),
            # An Ocean numbers from 0, and that is the label its cylinders carry too.
            (ProfileEventType.GAS_SWITCH, 0),
        ]

    def test_maps_the_two_stop_notifications_and_ignores_the_prompts_around_them(self):
        """ "Deep Stop Ahead" is the warning before and "Stop done" the confirmation after;
        marking all three would put three ticks on the chart for one stop."""
        content = _json_with_samples(
            [
                {
                    "Depth": 20.0,
                    "Events": [{"Notify": {"Type": "Deep Stop Ahead", "Active": True}}],
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                },
                {
                    "Depth": 18.0,
                    "Events": [{"Notify": {"Type": "Deep Stop", "Active": True}}],
                    "TimeISO8601": "2025-05-31T12:59:26.310+02:00",
                },
                {
                    "Depth": 5.0,
                    "Events": [{"Notify": {"Type": "Safety Stop", "Active": True}}],
                    "TimeISO8601": "2025-05-31T12:59:36.310+02:00",
                },
                {
                    "Depth": 5.0,
                    "Events": [{"Notify": {"Type": "Stop done", "Active": True}}],
                    "TimeISO8601": "2025-05-31T12:59:46.310+02:00",
                },
            ]
        )

        events = SuuntoJsonParser.parse_profile(content).events

        assert [event.type for event in events] == [ProfileEventType.DEEP_STOP, ProfileEventType.SAFETY_STOP]

    def test_emits_only_the_active_edge_of_a_paired_notification(self):
        """`Deep Stop` true at 1 424 s and false at 1 454 s is one 30-second stop. A tick
        can't show which half of a pair it is, so it marks the start."""
        content = _json_with_samples(
            [
                {
                    "Depth": 18.0,
                    "Events": [{"Notify": {"Type": "Deep Stop", "Active": True}}],
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                },
                {
                    "Depth": 18.0,
                    "Events": [{"Notify": {"Type": "Deep Stop", "Active": False}}],
                    "TimeISO8601": "2025-05-31T12:59:46.310+02:00",
                },
            ]
        )

        events = SuuntoJsonParser.parse_profile(content).events

        assert [(event.t, event.type) for event in events] == [(10.0, ProfileEventType.DEEP_STOP)]

    def test_alarms_and_warnings_are_classified_and_keep_the_device_s_own_wording(self):
        """The wording is what earns the type, and it travels beside it.

        Both, never one or the other: the type is what a chart draws a glyph from, and
        "Ceiling Broken" is what the diver was actually shown.
        """
        content = _json_with_samples(
            [
                {
                    "Depth": 30.0,
                    "Events": [
                        {"Warning": {"Type": "Ceiling Broken", "Active": True}},
                        {"Alarm": {"Type": "PO2 High", "Active": True}},
                    ],
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                }
            ]
        )

        events = SuuntoJsonParser.parse_profile(content).events

        assert [(event.type, event.label) for event in events] == [
            (ProfileEventType.CEILING_VIOLATION, "Ceiling Broken"),
            (ProfileEventType.PPO2_HIGH, "PO2 High"),
        ]

    def test_two_spellings_of_one_occurrence_share_a_type(self):
        """The type says what class of thing happened; the label says which words the
        device used for it."""
        content = _json_with_samples(
            [
                {
                    "Depth": 30.0,
                    "Events": [
                        {"Alarm": {"Type": "Safety Stop Broken", "Active": True}},
                        {"Alarm": {"Type": "Mandatory Safety Stop Broken", "Active": True}},
                    ],
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                }
            ]
        )

        events = SuuntoJsonParser.parse_profile(content).events

        assert [(event.type, event.label) for event in events] == [
            (ProfileEventType.SAFETY_STOP_VIOLATION, "Safety Stop Broken"),
            (ProfileEventType.SAFETY_STOP_VIOLATION, "Mandatory Safety Stop Broken"),
        ]

    def test_an_alert_this_table_has_no_value_for_stays_unclassified(self):
        """The vocabulary grows when a file names something it has no word for, and nothing
        is forced into the nearest one meanwhile - so the marker survives with its wording."""
        content = _json_with_samples(
            [
                {
                    "Depth": 30.0,
                    "Events": [{"Alarm": {"Type": "Battery Critically Low", "Active": True}}],
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                }
            ]
        )

        events = SuuntoJsonParser.parse_profile(content).events

        assert [(event.type, event.label) for event in events] == [(ProfileEventType.OTHER, "Battery Critically Low")]

    def test_two_notify_values_carry_an_occurrence_and_no_wording(self):
        """`Deco` is the moment the dive became a decompression dive and `Safety Stop Broken`
        is a required stop left early - both occurrences the vocabulary has a word for. Their
        `Type` is the device's name for its own *state*, so writing it as the wording of an
        occurrence would put "Deco" on a marker the diver never read."""
        content = _json_with_samples(
            [
                {
                    "Depth": 30.0,
                    "Events": [
                        {"Notify": {"Type": "Deco", "Active": True}},
                        {"Notify": {"Type": "Safety Stop Broken", "Active": True}},
                    ],
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                }
            ]
        )

        events = SuuntoJsonParser.parse_profile(content).events

        assert [(event.type, event.label) for event in events] == [
            (ProfileEventType.NDL_REACHED, None),
            (ProfileEventType.SAFETY_STOP_VIOLATION, None),
        ]

    def test_reads_the_four_decompression_channels_this_format_carries(self):
        content = _json_with_samples(
            [
                {
                    "Depth": 30.0,
                    "NoDecTime": 6000,
                    "TimeToSurface": 268,
                    "RtGradientFactors": {"gf99": 64, "gfSurface": 116},
                    "TimeISO8601": "2025-05-31T12:59:06.310+02:00",
                }
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert profile.ndl.v == [6000]
        assert profile.tts.v == [268]
        assert profile.gradient_factor.v == [64]
        assert profile.surface_gradient_factor.v == [116]
        # No sample object in this format carries a computed partial pressure or a running
        # CNS clock - the dive-level `EndTissue.CNS` is two scalars rather than a curve.
        assert profile.ppo2 is None
        assert profile.cns is None

    def test_reads_the_earlier_firmwares_spelling_of_the_surface_gradient_factor(self):
        """`gtSurface` is `gfSurface` one firmware earlier - a vendor typo fixed in an
        update. Both files are real, so a reader that knew one would lose the channel on
        whichever half of the installed base wrote the other."""
        content = _json_with_samples(
            [
                {
                    "Depth": 30.0,
                    "RtGradientFactors": {"gf99": 64, "gtSurface": 116},
                    "TimeISO8601": "2025-05-31T12:59:06.310+02:00",
                }
            ]
        )

        assert SuuntoJsonParser.parse_profile(content).surface_gradient_factor.v == [116]

    def test_a_negative_no_deco_time_is_the_absent_marker_and_a_zero_is_a_reading(self):
        """`-1` is the device showing a stop depth in place of a clock it no longer has -
        1 031 samples across the 19 Ocean exports in hand. A zero is the moment the dive
        stopped being a no-decompression dive, which is the reading a deco dive most needs.
        """
        content = _json_with_samples(
            [
                {"Depth": 30.0, "NoDecTime": 0, "TimeISO8601": "2025-05-31T12:59:06.310+02:00"},
                {"Depth": 31.0, "NoDecTime": -1, "TimeISO8601": "2025-05-31T12:59:16.310+02:00"},
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert profile.ndl.t == [0.0]
        assert profile.ndl.v == [0]

    def test_a_negative_gradient_factor_is_the_absent_marker_and_a_zero_is_a_reading(self):
        """`gf99: -100` is where no compartment leads - 5 531 of 7 194 samples - and `0` is a
        leading tissue at ambient, on 585 of them."""
        content = _json_with_samples(
            [
                {
                    "Depth": 3.0,
                    "RtGradientFactors": {"gf99": 0},
                    "TimeISO8601": "2025-05-31T12:59:06.310+02:00",
                },
                {
                    "Depth": 30.0,
                    "RtGradientFactors": {"gf99": -100},
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                },
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert profile.gradient_factor.t == [0.0]
        assert profile.gradient_factor.v == [0]

    def test_a_zero_time_to_surface_is_this_formats_absent_marker(self):
        """Only the file could settle it: the Ocean writes a zero on 199 of one dive's 364
        samples that carry the member, at every depth from 0 to 19 m - including two rows
        from a sample at 14.63 m that says 88. A TTS of zero at 14 m is not a time."""
        content = _json_with_samples(
            [
                {"Depth": 14.63, "TimeToSurface": 0, "TimeISO8601": "2025-05-31T12:59:06.310+02:00"},
                {"Depth": 14.63, "TimeToSurface": 88, "TimeISO8601": "2025-05-31T12:59:16.310+02:00"},
            ]
        )

        profile = SuuntoJsonParser.parse_profile(content)

        assert profile.tts.t == [10.0]
        assert profile.tts.v == [88]

    def test_a_gradient_factor_past_100_is_carried_as_written(self):
        """`ocean-deco-ppo2.json` keeps two such samples, so a clamping reader fails a pair
        rather than passing quietly."""
        content = _json_with_samples(
            [
                {
                    "Depth": 7.62,
                    "RtGradientFactors": {"gf99": 398},
                    "TimeISO8601": "2025-05-31T12:59:06.310+02:00",
                }
            ]
        )

        assert SuuntoJsonParser.parse_profile(content).gradient_factor.v == [398]

    def test_drops_the_device_narrating_its_own_state(self):
        """Five of these land on t=0 of every dive in the corpus. "Wet Outside" is not an
        event on a dive."""
        content = _json_with_samples(
            [
                {
                    "Depth": 1.0,
                    "Events": [
                        {"State": {"Type": "Dive Active", "Active": True}},
                        {"State": {"Type": "Below Surface", "Active": True}},
                        {"State": {"Type": "Tank pressure available", "Active": True}},
                    ],
                    "DiveEvents": [{"DiveState": "Diving"}, {"DiveStatus": True}],
                    "TimeISO8601": "2025-05-31T12:59:16.310+02:00",
                }
            ]
        )

        assert SuuntoJsonParser.parse_profile(content).events == []

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

    def test_extracts_the_ceiling_from_next_stop_depth(self):
        """FIT's deco ceiling is `record.next_stop_depth` - the depth of the next required
        stop. Unattested in the corpus, like `tank_update` was, so it is written here.

        Its three neighbours in the profile (`next_stop_time`, `time_to_surface`,
        `ndl_time`) all measure durations, and reading one of those as a depth is the
        mistake this test exists to catch.
        """
        content = dive_fit_file(
            fit_message("record", timestamp=FIT_DIVE_START, depth=30.0, next_stop_depth=0.0),
            fit_message("record", timestamp=FIT_DIVE_START + timedelta(seconds=10), depth=24.0, next_stop_depth=6.0),
            fit_message("record", timestamp=FIT_DIVE_START + timedelta(seconds=20), depth=9.0, next_stop_depth=3.0),
        )
        profile = FitParser.parse_profile(content)

        # Centimeters, and the zero is the absence of an obligation rather than a reading.
        assert profile.ceiling.t == [10.0, 20.0]
        assert profile.ceiling.v == [600, 300]
        assert profile.depth.t == [0.0, 10.0, 20.0]

    def test_omits_the_ceiling_channel_on_a_no_deco_dive(self):
        content = dive_fit_file(*_fit_records([(0, 12.0, 25), (10, 9.0, 25)]))

        assert FitParser.parse_profile(content).ceiling is None

    def test_maps_a_gas_switch_onto_the_cylinder_position_not_the_message_index(self):
        """`event.data` holds the switched-to gas's `message_index`, which is the device's
        key for a `dive_gas` and not the 1-based position mixtures are numbered by."""
        content = dive_fit_file(
            fit_message("dive_gas", message_index=0, oxygen_content=21, status="enabled"),
            fit_message("dive_gas", message_index=1, oxygen_content=54, status="enabled"),
            fit_message("event", timestamp=FIT_DIVE_START, event="dive_gas_switched", event_type="marker", data=0),
            fit_message(
                "event",
                timestamp=FIT_DIVE_START + timedelta(seconds=2356),
                event="dive_gas_switched",
                event_type="marker",
                data=1,
            ),
            *_fit_records([(0, 12.0, 25)]),
        )

        events = FitParser.parse_profile(content).events

        assert [(event.t, event.type, event.gas_number) for event in events] == [
            (0.0, ProfileEventType.GAS_SWITCH, 1),
            (2356.0, ProfileEventType.GAS_SWITCH, 2),
        ]

    def test_a_switch_to_a_gas_the_file_does_not_describe_keeps_a_null_number(self):
        """A `disabled` gas is dropped from the mixture list, so a switch naming it has no
        position to resolve to. The switch still happened - guessing a cylinder for it
        would be the join-by-hope `_tanks_for` refuses to make."""
        content = dive_fit_file(
            fit_message("dive_gas", message_index=0, oxygen_content=21, status="enabled"),
            fit_message("dive_gas", message_index=1, oxygen_content=54, status="disabled"),
            fit_message("event", timestamp=FIT_DIVE_START, event="dive_gas_switched", event_type="marker", data=1),
            *_fit_records([(0, 12.0, 25)]),
        )

        events = FitParser.parse_profile(content).events

        assert [(event.type, event.gas_number) for event in events] == [(ProfileEventType.GAS_SWITCH, None)]

    def test_reads_a_user_marker_as_a_bookmark_and_an_alert_as_its_own_words(self):
        content = dive_fit_file(
            fit_message(
                "event", timestamp=FIT_DIVE_START + timedelta(seconds=30), event="user_marker", event_type="marker"
            ),
            fit_message(
                "event",
                timestamp=FIT_DIVE_START + timedelta(seconds=60),
                event="dive_alert",
                event_type="marker",
                # 9 is `deco_ceiling_broken` in the profile's own `dive_alert` enum, which
                # is what `data` renders through for this event.
                data=9,
            ),
            *_fit_records([(0, 12.0, 25)]),
        )

        events = FitParser.parse_profile(content).events

        assert [(event.t, event.type, event.label) for event in events] == [
            (30.0, ProfileEventType.BOOKMARK, None),
            (60.0, ProfileEventType.OTHER, "deco_ceiling_broken"),
        ]

    def test_ignores_the_timer_events_that_bound_every_file(self):
        """`timer` start/stop is the only `event` any file in the corpus writes, and it
        says where the dive begins and ends - which the profile's own axis already does."""
        content = dive_fit_file(
            fit_message("event", timestamp=FIT_DIVE_START, event="timer", event_type="start"),
            fit_message("event", timestamp=FIT_DIVE_START + timedelta(seconds=600), event="timer", event_type="stop"),
            *_fit_records([(0, 12.0, 25)]),
        )

        assert FitParser.parse_profile(content).events == []

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

        assert profile.depth.t == [0, 10_000]
        assert profile.temperature.t == [0, 20_000]

    def test_rounds_onto_integer_milliseconds_keeping_the_last_reading_per_millisecond(self):
        parsed = SuuntoJsonParser.parse_profile(
            _json_with_samples(
                [
                    {"Temperature": 299.15, "TimeISO8601": "2025-05-31T12:59:06.310100+02:00"},
                    {"Temperature": 299.05, "TimeISO8601": "2025-05-31T12:59:06.310400+02:00"},
                    {"Temperature": 298.95, "TimeISO8601": "2025-05-31T12:59:06.410000+02:00"},
                ]
            )
        )

        profile = normalize(parsed)

        assert profile.temperature.t == [0, 100]
        assert profile.temperature.v == [259, 258]

    def test_from_the_file_start_a_first_reading_keeps_the_offset_its_file_states(self):
        """The axis counts from the start the header states, which is the recording's
        `started_at` - so a Suunto app export's first depth 160 ms after `Header.DateTime` is
        at 160, not at zero."""
        parsed = SuuntoJsonParser.parse_profile(
            _json_with_samples(
                [
                    {"Depth": 1.2, "TimeISO8601": "2025-05-31T12:59:06.160+02:00"},
                    {"Depth": 3.4, "TimeISO8601": "2025-05-31T13:19:06.160+02:00"},
                ],
                date_time="2025-05-31T12:59:06.000+02:00",
            )
        )

        profile = normalize(parsed, from_file_start=True)

        assert profile.depth.t == [160, 1_200_160]
        assert profile.duration == 1_200_160

    def test_a_reading_before_the_file_start_is_clamped_to_it_keeping_the_later_one(self):
        """A FIT record can be stamped before its session's start. There is nowhere else on
        the axis for it, and where the clamp puts two readings on zero the later one wins,
        `_rebase`'s rule for any collision."""
        parsed = ParsedProfileSchema(depth=ParsedSeries(t=[-2.0, -0.5, 0.0, 10.0], v=[100, 110, 120, 130]))

        profile = normalize(parsed, from_file_start=True)

        assert profile.depth.t == [0, 10_000]
        assert profile.depth.v == [120, 130]

    def test_returns_none_for_a_profile_with_no_readings(self):
        assert normalize(ParsedProfileSchema()) is None

    def test_returns_none_for_a_file_of_events_and_no_samples(self):
        """Events don't make a profile. There is no axis for them to be drawn against, and
        letting them stand alone would give a dive a chart with nothing on it."""
        parsed = ParsedProfileSchema(
            events=[ParsedProfileEvent(t=10.0, type=ProfileEventType.GAS_SWITCH, gas_number=1)]
        )

        assert normalize(parsed) is None

    def test_rebases_events_against_the_samples_origin_not_their_own(self):
        """A marker annotates the curve, so it has to move with it. Given its own origin,
        the first event would always sit at t=0 regardless of when it happened."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[100.0, 110.0], v=[124, 256]),
            events=[ParsedProfileEvent(t=130.0, type=ProfileEventType.SAFETY_STOP)],
        )

        profile = normalize(parsed)

        assert profile.depth.t == [0, 10_000]
        assert [event.t for event in profile.events] == [30_000]

    def test_an_event_before_the_first_sample_lands_at_the_start(self):
        """The ordinary case, not a corrupt one: a Suunto XML export numbers samples from
        `<Time>1</Time>` while recording the opening gas selection at `GasChangeTime` 0.
        Dropping it would lose which gas the dive *started* on."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[1.0, 11.0], v=[124, 256]),
            events=[ParsedProfileEvent(t=0.0, type=ProfileEventType.GAS_SWITCH, gas_number=1)],
        )

        profile = normalize(parsed)

        assert [(event.t, event.gas_number) for event in profile.events] == [(0, 1)]

    def test_events_do_not_move_the_origin_the_channels_are_rebased_onto(self):
        """One mistimed marker must not slide every curve away from its own axis."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[100.0, 110.0], v=[124, 256]),
            events=[ParsedProfileEvent(t=-500.0, type=ProfileEventType.BOOKMARK)],
        )

        profile = normalize(parsed)

        assert profile.depth.t == [0, 10_000]
        assert [event.t for event in profile.events] == [0]

    def test_sorts_events_that_the_file_listed_out_of_order(self):
        """The XML export nests gas changes inside each `<DiveMixture>`, so file order is
        cylinder order rather than time order."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 3000.0], v=[124, 256]),
            events=[
                ParsedProfileEvent(t=2356.0, type=ProfileEventType.GAS_SWITCH, gas_number=2),
                ParsedProfileEvent(t=0.0, type=ProfileEventType.GAS_SWITCH, gas_number=1),
            ],
        )

        profile = normalize(parsed)

        assert [(event.t, event.gas_number) for event in profile.events] == [(0, 1), (2_356_000, 2)]

    def test_collapses_two_identical_events_that_round_onto_one_millisecond(self):
        """Rounding onto integer milliseconds is what makes this necessary - the same
        `GasSwitch` under both `Events` and `DiveEvents` would stack two ticks on one pixel."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 100.0], v=[124, 256]),
            events=[
                ParsedProfileEvent(t=50.1001, type=ProfileEventType.SAFETY_STOP),
                ParsedProfileEvent(t=50.1004, type=ProfileEventType.SAFETY_STOP),
                # A different type at the same instant is a different event and survives.
                ParsedProfileEvent(t=50.1002, type=ProfileEventType.OTHER, label="Ceiling Broken"),
            ],
        )

        profile = normalize(parsed)

        assert [(event.t, event.type) for event in profile.events] == [
            (50_100, ProfileEventType.SAFETY_STOP),
            (50_100, ProfileEventType.OTHER),
        ]

    def test_truncates_a_label_rather_than_rejecting_it(self):
        """`label` is the only field in the payload carrying text straight off an uploaded
        file, so it is the only one nothing else bounds - `MAX_EVENTS` counts markers and
        every channel is capped by point count. Unbounded, a 5 MB export of long alert
        strings becomes a 5 MB JSONB row on a table designed for tens of KB.

        Truncated rather than refused: `extract_profile` must never fail the upload it rode
        in on, so a file whose one long alert took its depth curve with it is the outcome
        this avoids.
        """
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0], v=[10]),
            events=[ParsedProfileEvent(t=0.0, type=ProfileEventType.OTHER, label="x" * 5_000)],
        )

        profile = normalize(parsed)

        assert profile.events[0].label == "x" * MAX_LABEL_CHARS
        # Far past any device's wording - the longest in the corpus is 28 characters.
        assert MAX_LABEL_CHARS == 120

    def test_an_event_after_the_last_sample_keeps_its_own_time(self):
        """The clamp is deliberately one-sided. Zero is where every format's dive begins, so
        pinning to it lands on a real boundary; there is no such boundary at the other end,
        and dragging a late marker back onto the last sample would invent a time for it."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 10.0], v=[124, 256]),
            events=[ParsedProfileEvent(t=90.0, type=ProfileEventType.BOOKMARK)],
        )

        profile = normalize(parsed)

        assert [event.t for event in profile.events] == [90_000]
        assert profile.duration == 10_000

    def test_the_ceiling_shares_the_depth_channels_origin(self):
        parsed = SuuntoXmlParser.parse_profile(
            _xml_with_samples(_sample(1, depth="30.0") + _sample(11, depth="24.0", ceiling="3"))
        )

        profile = normalize(parsed)

        assert profile.depth.t == [0, 10_000]
        assert profile.ceiling.t == [10_000]
        assert profile.channels == ["depth", "ceiling"]

    def test_summary_properties_describe_the_recorded_span(self):
        parsed = SuuntoXmlParser.parse_profile(
            _xml_with_samples(
                _sample(1, depth="1.0", temperature="26.0", pressure="205200")
                + _sample(101, depth="2.0", temperature="25.0", pressure="204000")
            )
        )

        profile = normalize(parsed)

        assert profile.duration == 100_000
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

    def test_keeps_the_first_and_last_sample(self):
        """Min/max bucketing does not give this for free. `min` returns the first of equal
        values, so a channel ending in a run of identical readings - a diver floating at the
        surface, which is how a 1 Hz recording usually ends - would pick the start of that
        run and drop the true final sample, leaving the channel short of the dive.
        """
        flat_ending = ProfileSeries(
            t=list(range(9_000)),
            v=[(index * 37) % 500 for index in range(8_500)] + [0] * 500,
        )

        profile = downsample(NormalizedProfile(depth=flat_ending), max_points=1200)

        assert profile.depth.t[0] == 0
        assert profile.depth.t[-1] == 8_999
        assert len(profile.depth.t) <= 1200

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

    def test_caps_the_ceiling_channel_like_depth(self):
        profile = downsample(NormalizedProfile(ceiling=self._sawtooth(9_000)), max_points=1200)

        assert len(profile.ceiling.t) <= 1200
        assert max(profile.ceiling.v) == max(self._sawtooth(9_000).v)

    def test_truncates_events_rather_than_bucketing_them(self):
        """Min/max over a window of markers means nothing - there is no "highest" gas
        switch - so the cap is a plain head-of-list."""
        events = [ProfileEvent(t=second, type=ProfileEventType.BOOKMARK) for second in range(500)]

        profile = downsample(NormalizedProfile(depth=self._sawtooth(50), events=events), max_events=10)

        assert [event.t for event in profile.events] == list(range(10))

    def test_leaves_events_under_the_cap_alone(self):
        events = [ProfileEvent(t=second, type=ProfileEventType.BOOKMARK) for second in range(17)]

        profile = downsample(NormalizedProfile(depth=self._sawtooth(50), events=events))

        assert len(profile.events) == 17

    def test_the_real_caps_are_the_module_defaults(self):
        assert MAX_POINTS_PER_CHANNEL == 1200
        # Far above any real dive: the worst in the corpus produces 17 markers.
        assert MAX_EVENTS == 200


def _switch(t: float, gas_number: int | None) -> ParsedProfileEvent:
    return ParsedProfileEvent(t=t, type=ProfileEventType.GAS_SWITCH, gas_number=gas_number)


def _dive_on_two_gases() -> ParsedProfileSchema:
    """The corpus's commonest tech shape, in miniature: a back gas breathed deep, a switch
    to a deco bottle, and a shallow stop on it. Depth every 100 s, in centimeters.
    """
    return ParsedProfileSchema(
        depth=ParsedSeries(t=[0.0, 100.0, 200.0, 300.0, 400.0], v=[3000, 3000, 3000, 600, 600]),
        events=[_switch(0.0, 1), _switch(300.0, 2)],
    )


class TestDeriveGasAttribution:
    """Which cylinder was breathed for how long, and how deep - the fact Phase 4 turns
    into per-tank consumption. See `derive_gas_attribution` for why gas-switch events are
    the only source it will take.
    """

    def test_splits_a_two_gas_dive_at_the_switch(self):
        profile = normalize(_dive_on_two_gases())

        attribution = derive_gas_attribution(profile)

        # 0-300 s on gas 1 at a flat 30 m; 300-400 s on gas 2 at 6 m. The dive's own
        # average depth - 24 m - describes neither, which is the whole point.
        assert [(entry.gas_number, entry.seconds, entry.mean_depth_cm) for entry in attribution] == [
            (1, 300, 3000),
            (2, 100, 600),
        ]

    def test_a_gas_returned_to_is_one_entry_with_the_time_added_up(self):
        """A diver who goes back to their back gas for the ascent has two stretches on it
        and one cylinder, and it is the cylinder the pressures belong to."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 100.0, 200.0, 300.0], v=[3000, 600, 3000, 1500]),
            events=[_switch(0.0, 1), _switch(100.0, 2), _switch(200.0, 1)],
        )

        attribution = derive_gas_attribution(normalize(parsed))

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(1, 200), (2, 100)]
        # Mean over both stretches on gas 1 - 30 m, then 30 m and 15 m after the switch
        # back - and not over the 6 m spent on the deco bottle in between.
        assert attribution[0].mean_depth_cm == 2500

    def test_the_same_gas_selected_twice_running_is_one_stretch(self):
        """Suunto writes this whenever a diver browses the gas list without changing
        anything - `Dive_2025-03-08-1440` records switches to gas 2 at 3 081 s and 3 087 s."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 100.0, 200.0], v=[3000, 3000, 3000]),
            events=[_switch(0.0, 1), _switch(100.0, 1)],
        )

        attribution = derive_gas_attribution(normalize(parsed))

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(1, 200)]

    def test_a_switch_before_the_first_sample_is_the_gas_the_dive_started_on(self):
        """The ordinary case for a Suunto: the opening selection is recorded at t=0 while
        samples are numbered from `<Time>1</Time>`."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[10.0, 110.0], v=[3000, 3000]),
            events=[_switch(0.0, 1)],
        )

        attribution = derive_gas_attribution(normalize(parsed))

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(1, 100)]

    def test_time_before_the_first_switch_is_left_unattributed(self):
        """Nothing says what was breathed then. The shortfall is what
        `compute_multi_tank_gas_use` reports as `attributed_seconds` rather than dividing a
        cylinder's gas by less time than it was breathed for."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 100.0, 200.0, 300.0], v=[3000, 3000, 3000, 3000]),
            events=[_switch(200.0, 2)],
        )

        attribution = derive_gas_attribution(normalize(parsed))

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(2, 100)]

    def test_a_file_with_no_switches_attributes_nothing(self):
        """No single-gas fallback: a one-cylinder dive already yields a figure through
        `compute_gas_use`, so inferring one here would be a second answer to a question
        that already has one."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 100.0], v=[3000, 3000]),
            pressure=[ParsedPressureSeries(gas_number=1, t=[0.0, 100.0], v=[2052, 1800])],
        )

        assert derive_gas_attribution(normalize(parsed)) == []

    def test_a_switch_that_does_not_say_which_gas_attributes_nothing(self):
        """FIT records this when a switch names a gas the file's own list dropped, and it
        keeps a null `gas_number` rather than guessing (see `_breathed_gases`). There is
        nothing here to join a cylinder to."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 100.0], v=[3000, 3000]),
            events=[_switch(50.0, None)],
        )

        assert derive_gas_attribution(normalize(parsed)) == []

    def test_a_switch_after_the_last_sample_is_ignored(self):
        """A `user_marker` can be pressed after the final `record` (see `_rebase_events`),
        and there is no dive left after the recording stops to attribute to it."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 100.0], v=[3000, 3000]),
            events=[_switch(0.0, 1), _switch(500.0, 2)],
        )

        attribution = derive_gas_attribution(normalize(parsed))

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(1, 100)]

    def test_two_switches_on_one_second_keep_the_later_gas(self):
        """The same last-reading-wins rule `_rebase` applies to a channel: what the diver
        ended up on is what they breathed."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 100.0], v=[3000, 3000]),
            events=[_switch(0.0, 1), _switch(0.4, 2)],
        )

        attribution = derive_gas_attribution(normalize(parsed))

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(2, 100)]

    def test_a_gas_with_no_depth_sample_of_its_own_is_dropped(self):
        """It has a time but no depth to normalize it against, and a borrowed one would be
        a number the file never recorded."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 100.0], v=[3000, 3000]),
            events=[_switch(0.0, 1), _switch(50.0, 2), _switch(60.0, 1)],
        )

        attribution = derive_gas_attribution(normalize(parsed))

        assert [entry.gas_number for entry in attribution] == [1]

    def test_a_switch_landing_on_the_last_depth_sample_attributes_nothing_to_it(self):
        """There is no dive left after the last sample, so the stretch is zero seconds and
        the gas is left out of the attribution entirely rather than entered with a time of
        nothing. What makes that the right place to drop it is downstream: an entry
        claiming a cylinder while accounting for none of the dive reads to
        `compute_multi_tank_gas_use` as a cylinder that merely produced no figure, and the
        remaining tanks would then be reported as covering the whole dive. A switch one
        second later is already handled by the `break`, and one second must not decide
        between a refusal and a wrong figure.
        """
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 100.0, 200.0], v=[3000, 3000, 3000]),
            events=[_switch(0.0, 1), _switch(200.0, 2)],
        )

        attribution = derive_gas_attribution(normalize(parsed))

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(1, 200)]

    def test_a_profile_with_no_depth_channel_attributes_nothing(self):
        """A pressure-and-temperature-only export has no depth for a mean to be taken of,
        and every figure downstream is normalized against depth."""
        parsed = ParsedProfileSchema(
            temperature=ParsedSeries(t=[0.0, 100.0], v=[260, 259]),
            events=[_switch(0.0, 1)],
        )

        assert derive_gas_attribution(normalize(parsed)) == []

    def test_reads_the_switches_a_two_gas_xml_export_nests_in_its_mixtures(self):
        """End to end from the bytes, on the shape the corpus actually holds: the XML
        export keeps each cylinder's gas changes inside its own `<DiveMixture>`, so
        `Dive_2025-06-03-1215` reads as gas 1 from the start and gas 2 at 2 355 s.

        The seconds are 299 and 101 rather than 300 and 100 because this format numbers its
        samples from `<Time>1</Time>` and its gas changes from 0, so rebasing moves every
        switch a second earlier - the same off-by-one that makes the opening selection land
        at -1 before it is clamped.
        """
        content = _xml_with_samples(
            "".join(_sample(1 + index * 100, depth="30.0" if index < 3 else "6.0") for index in range(5)),
            mixtures=_mixture(0) + _mixture(300, oxygen="49"),
        )

        profile = extract_profile(SuuntoXmlParser, content)

        assert [(entry.gas_number, entry.seconds, entry.mean_depth_cm) for entry in profile.gas_attribution] == [
            (1, 299, 3000),
            (2, 101, 600),
        ]


class TestFinalizeProfile:
    def test_attributes_before_downsampling_so_the_mean_is_of_the_dive(self):
        """The load-bearing ordering in `finalize_profile`. Min/max bucketing keeps each
        bucket's extremes and discards what lies between them, so a mean taken afterwards
        would be a mean of the dive's peaks and troughs. This dive spends most of its time
        at 30 m with two brief excursions to 40 m and 20 m per bucket; the true mean is far
        nearer 30 m than the 30 m the extremes would average to by luck, so the sawtooth is
        deliberately lopsided.
        """
        depths = []
        for index in range(2000):
            depths.append(4000 if index % 100 == 0 else 1000)
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[float(index) for index in range(2000)], v=depths),
            events=[_switch(0.0, 1)],
        )

        profile = finalize_profile(parsed)

        # The honest mean: 1% of the samples at 40 m, the rest at 10 m.
        assert profile.gas_attribution[0].mean_depth_cm == round(sum(depths) / len(depths))
        # And the channel really was thinned, so the mean could not have been taken from it.
        assert len(profile.depth.t) < len(depths)
        assert profile.gas_attribution[0].mean_depth_cm != round(sum(profile.depth.v) / len(profile.depth.v))

    def test_attributed_time_never_exceeds_the_span_it_is_a_fraction_of(self):
        """The invariant `attributed_seconds`/`duration` exists to state, and the
        one place the two halves can disagree: attribution is derived from the
        full-resolution channel while the stored span comes off the thinned one. A 77-minute
        1 Hz dive - the cadence every FIT export uses, and past `MAX_POINTS_PER_CHANNEL`
        within twenty minutes - ending in a flat stretch at the surface is what used to make
        the denominator the shorter of the two, and a client print `100.2%`.
        """
        depths = [min(3000, second * 10) if second < 4_500 else 0 for second in range(4_620)]
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[float(second) for second in range(4_620)], v=depths),
            events=[_switch(0.0, 1), _switch(3000.0, 2)],
        )

        profile = finalize_profile(parsed)

        # `seconds` is whole seconds and the span milliseconds, so the fraction divides the span.
        span = profile.duration / MILLISECONDS_PER_SECOND
        assert sum(entry.seconds for entry in profile.gas_attribution) <= span
        # And exactly equal here, since the dive begins on a gas and never stops being on one.
        assert sum(entry.seconds for entry in profile.gas_attribution) == span


def _row(source_sha256: str, extractor_version: int, parser_key: str = "suunto_xml") -> ExistingProfileRow:
    return ExistingProfileRow(source_sha256=source_sha256, extractor_version=extractor_version, parser_key=parser_key)


class TestShouldExtract:
    @pytest.mark.parametrize(
        ("existing", "sha256", "version", "expected"),
        [
            (None, "abc", 1, "extract"),
            (_row("abc", 1), "abc", 1, "skip"),
            (_row("def", 1), "abc", 1, "extract"),
            (_row("abc", 1), "abc", 2, "extract"),
            # A row written by a *newer* extractor than this build is also "not current",
            # and re-extracting is the honest answer - it is what this build can vouch for.
            (_row("abc", 3), "abc", 2, "extract"),
            # **Provenance beats both.** A profile a document supplied or a merge produced is
            # never re-extracted, whatever its digest and version say, because nothing on this
            # instance can produce those samples a second time - and for a merged recording
            # what the files *would* yield is one half of what the profile describes.
            (_row("abc", 1, parser_key="divejson_import"), "different", 1, "skip"),
            (_row("abc", 1, parser_key="merge"), "different", 99, "skip"),
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

        assert profile.depth.t == [0, 10_000, 20_000, 30_000, 40_000]
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

    def test_the_two_exports_spell_no_ceiling_differently_and_still_agree(self):
        """The XML writes `xsi:nil` and the JSON writes `0` for the same fact. Verified
        against the real pairs: all three deco dives in the corpus that exist as both files
        produce identical ceiling channels, to the reading (`Dive_2025-06-03-1215.xml` is
        266 samples peaking at 13.11 m either way).
        """
        xml = extract_profile(
            SuuntoXmlParser,
            _xml_with_samples(_sample(1, depth="30.0") + _sample(11, depth="24.0", ceiling="3")),
        )
        js = extract_profile(
            SuuntoJsonParser,
            _json_with_samples(
                [
                    {"Depth": 30.0, "Ceiling": 0, "TimeISO8601": "2025-05-31T12:59:07.000+02:00"},
                    {"Depth": 24.0, "Ceiling": 3, "TimeISO8601": "2025-05-31T12:59:17.000+02:00"},
                ]
            ),
        )

        assert xml.ceiling.t == js.ceiling.t == [10_000]
        assert xml.ceiling.v == js.ceiling.v == [300]


class TestToData:
    def test_omits_absent_channels_rather_than_writing_nulls(self):
        data = NormalizedProfile(depth=ProfileSeries(t=[0, 1], v=[10, 20])).to_data()

        assert data == {"depth": {"t": [0, 1], "v": [10, 20]}}
        assert "ceiling" not in data
        assert "temperature" not in data
        assert "pressure" not in data
        assert "events" not in data

    def test_writes_the_ceiling_as_a_channel_beside_depth(self):
        data = NormalizedProfile(
            depth=ProfileSeries(t=[0, 1], v=[3000, 2400]), ceiling=ProfileSeries(t=[1], v=[300])
        ).to_data()

        assert data["ceiling"] == {"t": [1], "v": [300]}

    def test_leaves_out_the_keys_an_event_has_no_value_for(self):
        """So `gas_number` present-and-null can't come to mean something different from
        absent - the same rule the channels follow."""
        data = NormalizedProfile(
            depth=ProfileSeries(t=[0], v=[10]),
            events=[
                ProfileEvent(t=0, type=ProfileEventType.GAS_SWITCH, gas_number=1),
                ProfileEvent(t=60, type=ProfileEventType.OTHER, label="Ceiling Broken"),
                ProfileEvent(t=90, type=ProfileEventType.SAFETY_STOP),
            ],
        ).to_data()

        assert data["events"] == [
            {"t": 0, "type": "gas_switch", "gas_number": 1},
            {"t": 60, "type": "other", "label": "Ceiling Broken"},
            {"t": 90, "type": "safety_stop"},
        ]


class TestTheDecompressionChannels:
    """The six channels the computer worked out for itself, through the storage pipeline.

    Grouped rather than folded into `TestToData`/`TestNormalize` above because the point is
    that every stage treats them exactly as it treats depth - and a stage that named the
    four original channels and forgot the new ones is what these would catch.
    """

    def _parsed(self) -> ParsedProfileSchema:
        return ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0, 10.0], v=[3000, 2400]),
            ndl=ParsedSeries(t=[0.0, 10.0], v=[5940, 0]),
            tts=ParsedSeries(t=[10.0], v=[268]),
            ppo2=ParsedSeries(t=[0.0], v=[96]),
            cns=ParsedSeries(t=[10.0], v=[800]),
            gradient_factor=ParsedSeries(t=[10.0], v=[398]),
            surface_gradient_factor=ParsedSeries(t=[10.0], v=[116]),
        )

    def test_normalize_rebases_every_channel_against_one_origin(self):
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[10.0, 20.0], v=[3000, 2400]),
            ndl=ParsedSeries(t=[20.0], v=[0]),
            gradient_factor=ParsedSeries(t=[30.0], v=[398]),
        )

        profile = normalize(parsed)

        assert profile.depth.t == [0, 10_000]
        assert profile.ndl.t == [10_000]
        assert profile.gradient_factor.t == [20_000]

    def test_a_file_of_only_a_computed_channel_still_has_a_profile(self):
        """The origin is the earliest reading across *all* channels, not across the four a
        sensor produces - so a recording that carried an NDL and no depth is a profile."""
        profile = normalize(ParsedProfileSchema(ndl=ParsedSeries(t=[5.0, 15.0], v=[5940, 1260])))

        assert profile is not None
        assert profile.ndl.t == [0, 10_000]
        assert profile.channels == ["ndl"]

    def test_to_data_and_back_is_the_same_profile(self):
        profile = normalize(self._parsed())

        assert profile_from_data(profile.to_data()) == profile

    def test_a_zero_is_a_reading_and_survives_the_round_trip(self):
        """An NDL of zero is the moment a dive stopped being a no-decompression dive, which
        is the one reading a decompression dive most needs."""
        profile = normalize(self._parsed())

        assert profile.ndl.v == [5940, 0]
        assert profile_from_data(profile.to_data()).ndl.v == [5940, 0]

    def test_channels_lists_them_in_the_formats_order(self):
        """`pressure` between `temperature` and the computed channels, which is where §6.4
        puts it - and what a chart stacks its curves in."""
        profile = normalize(
            ParsedProfileSchema(
                depth=ParsedSeries(t=[0.0], v=[3000]),
                temperature=ParsedSeries(t=[0.0], v=[219]),
                pressure=[ParsedPressureSeries(gas_number=1, t=[0.0], v=[2052])],
                ndl=ParsedSeries(t=[0.0], v=[5940]),
                gradient_factor=ParsedSeries(t=[0.0], v=[64]),
            )
        )

        assert profile.channels == ["depth", "temperature", "pressure", "ndl", "gradient_factor"]

    def test_each_channel_is_capped_independently(self):
        long = ParsedSeries(t=[float(second) for second in range(9_000)], v=list(range(9_000)))
        profile = downsample(normalize(ParsedProfileSchema(depth=long, ndl=long, cns=long)))

        assert len(profile.ndl.t) <= MAX_POINTS_PER_CHANNEL
        assert len(profile.cns.t) <= MAX_POINTS_PER_CHANNEL
        assert max(profile.ndl.v) == 8_999

    def test_a_second_file_fills_a_channel_the_first_did_not_carry(self):
        base = normalize(ParsedProfileSchema(depth=ParsedSeries(t=[0.0], v=[3000])))
        addition = normalize(ParsedProfileSchema(ppo2=ParsedSeries(t=[0.0], v=[96])))

        filled = fill_channels(base, addition)

        assert filled.depth.v == [3000]
        assert filled.ppo2.v == [96]

    def test_a_channel_the_first_file_carried_is_never_overwritten(self):
        base = normalize(ParsedProfileSchema(ndl=ParsedSeries(t=[0.0], v=[5940])))
        addition = normalize(ParsedProfileSchema(ndl=ParsedSeries(t=[0.0], v=[10])))

        assert fill_channels(base, addition).ndl.v == [5940]

    def test_shifting_moves_every_channel_onto_one_clock(self):
        profile = normalize(self._parsed())

        moved = shift_profile(profile, 223_000)

        assert moved.ndl.t == [223_000, 233_000]
        assert moved.cns.t == [233_000]

    def test_shifting_back_past_zero_clamps_there_keeping_the_later_reading(self):
        """A file whose clock started before the recording's: what lands before zero is
        kept at zero, and where two land there the later reading stands."""
        profile = normalize(self._parsed())

        moved = shift_profile(profile, -10_000)

        assert moved.ndl.t == [0]
        assert moved.ndl.v == [0]
        assert moved.depth.v == [2400]

    def test_joining_two_records_of_one_dive_joins_every_channel(self):
        earlier = normalize(ParsedProfileSchema(tts=ParsedSeries(t=[0.0], v=[120])))
        later = shift_profile(normalize(ParsedProfileSchema(tts=ParsedSeries(t=[0.0], v=[60]))), 300)

        joined = join_profiles(earlier, later)

        assert joined.tts.t == [0, 300]
        assert joined.tts.v == [120, 60]

    def test_the_summary_extremes_are_per_quantity(self):
        """`min` for the no-decompression clock, because the *maximum* is the device's
        display cap on almost every recreational dive and says nothing; `max` for the rest.
        """
        extremes = summary_extremes(normalize(self._parsed()))

        assert extremes["min_ndl_s"] == 0
        assert extremes["max_tts_s"] == 268
        assert extremes["max_ppo2_bar100"] == 96
        assert extremes["max_cns_pct10"] == 800
        assert extremes["max_gradient_factor_pct"] == 398
        assert extremes["max_surface_gradient_factor_pct"] == 116

    def test_a_channel_this_profile_has_none_of_clears_its_column(self):
        """Every column is written, `None` included: a rewrite that left one out would leave
        the previous profile's extreme standing beside samples that no longer contain it."""
        extremes = summary_extremes(normalize(ParsedProfileSchema(depth=ParsedSeries(t=[0.0], v=[3000]))))

        assert extremes["min_ndl_s"] is None
        assert extremes["max_surface_gradient_factor_pct"] is None
        assert extremes["max_depth_cm"] == 3000

    def test_a_gradient_factor_past_100_is_stored_as_written(self):
        """A GF99 runs into four figures on a real Suunto decompression ascent. Suunto
        publishes no definition of the field and nothing in the file accounts for the size,
        so a cap would be a guess wearing a plausible number."""
        profile = normalize(ParsedProfileSchema(gradient_factor=ParsedSeries(t=[0.0], v=[12_575])))

        assert profile.gradient_factor.v == [12_575]
        assert summary_extremes(profile)["max_gradient_factor_pct"] == 12_575


class TestToReadSchema:
    """The stored payload back out as the wire shape. Pure, so it is tested here rather
    than through the endpoint."""

    def test_round_trips_every_channel_and_event(self):
        profile = NormalizedProfile(
            depth=ProfileSeries(t=[0, 10], v=[3000, 2400]),
            ceiling=ProfileSeries(t=[10], v=[300]),
            temperature=ProfileSeries(t=[0], v=[219]),
            pressure=[ProfilePressureSeries(gas_number=1, t=[0], v=[2052])],
            events=[
                ProfileEvent(t=0, type=ProfileEventType.GAS_SWITCH, gas_number=1),
                ProfileEvent(t=60, type=ProfileEventType.OTHER, label="Ceiling Broken"),
            ],
        )

        read = to_read_schema(LoadedProfile(duration=10, data=profile.to_data(), parser_key="suunto_xml"))

        assert read.ceiling.values == [300]
        # **`other` does not reach the wire.** The format spells "unclassified" as an absent
        # `type` beside the label that is then required, and storage spells it `OTHER`
        # because a JSONB key wants a value; `to_read_schema` is where the two meet. This
        # schema is what an exported document's `profile` object is serialized from, so a
        # `"type": "other"` here would make every document invalid.
        assert [(event.time, event.type, event.gas_number, event.label) for event in read.events] == [
            (0, ProfileEventType.GAS_SWITCH, 1, None),
            (60, None, None, "Ceiling Broken"),
        ]

    def test_an_event_type_this_build_does_not_know_reads_as_unclassified(self):
        """A row a later build wrote, read by this one. The column carries no `CHECK`, so it
        really can hold a value this enum has not heard of - and the label beside it is still
        a marker worth drawing, which is why this is `None` rather than a raise."""
        read = to_read_schema(
            LoadedProfile(
                duration=10,
                data={
                    "depth": {"t": [0], "v": [3000]},
                    "events": [{"t": 5, "type": "bailout", "label": "Bailout"}],
                },
                parser_key="suunto_xml",
            )
        )

        assert [(event.type, event.label) for event in read.events] == [(None, "Bailout")]

    def test_reads_a_payload_written_before_ceilings_and_events_existed(self):
        """Extractor version 1's rows, which a backfill has not reached yet. The optional
        keys are read with `.get` for exactly this: a `KeyError` here would 500 the profile
        endpoint for every dive imported before the bump."""
        read = to_read_schema(
            LoadedProfile(duration=10, data={"depth": {"t": [0], "v": [3000]}}, parser_key="suunto_xml")
        )

        assert read.depth.values == [3000]
        assert read.ceiling is None
        assert read.events == []


class TestProvenance:
    """The stored `parser_key` as the wire's closed three-way answer.

    `parser_key` is overloaded on purpose - a parser's key, or one of two sentinels - which
    is right for a column that also has to answer "can this be extracted again" and wrong
    for a member a client switches on.
    """

    def test_a_parsers_key_reads_as_file(self):
        """Every parser, rather than the one this suite happens to write: the wire member
        must not grow a value when a parser is added."""
        assert {provenance_of(parser.key) for parser in _PARSERS} == {ProfileProvenance.FILE}

    def test_each_sentinel_reads_as_itself(self):
        assert provenance_of(IMPORT_PARSER_KEY) is ProfileProvenance.DIVEJSON_IMPORT
        assert provenance_of(MERGE_PARSER_KEY) is ProfileProvenance.MERGE

    def test_every_unreproducible_provenance_has_a_wire_value(self):
        """The guard on the fallback. Anything not named in the mapping reads as `FILE` -
        "read off this recording's files, and re-readable from them" - which is the one
        answer that is never true of a member of this set, so a third sentinel added without
        a spelling would publish a lie rather than raise.
        """
        assert set(_PROVENANCE_BY_PARSER_KEY) == UNREPRODUCIBLE_PROVENANCES

    def test_the_recording_route_carries_it_and_the_formats_object_does_not(self):
        """Two shapes off one payload. `DiveProfileRead` is the DiveJSON `profile` object,
        whose schema is `additionalProperties: false`, so the member rides the subclass the
        route serves and the exported document keeps exactly the format's members.
        """
        loaded = LoadedProfile(duration=10, data={"depth": {"t": [0], "v": [3000]}}, parser_key=MERGE_PARSER_KEY)

        read = to_recording_read_schema(loaded)

        assert read.provenance is ProfileProvenance.MERGE
        assert read.depth.values == [3000]
        assert "provenance" not in to_read_schema(loaded).model_dump()


class TestParsedProfileValidation:
    """`_validate_events` is a separate validator from `_validate_series` on purpose - the
    two shapes share no invariants worth checking together."""

    def test_rejects_an_other_with_no_label(self):
        """The escape hatch exists to carry the device's own wording. Without it the marker
        is a tick that tells the diver nothing, which means a parser dropped the one thing
        it had to keep."""
        with pytest.raises(ValueError, match="no label"):
            ParsedProfileSchema(
                depth=ParsedSeries(t=[0.0], v=[10]),
                events=[ParsedProfileEvent(t=1.0, type=ProfileEventType.OTHER)],
            )

    def test_accepts_the_typed_events_without_a_label(self):
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0], v=[10]),
            events=[ParsedProfileEvent(t=1.0, type=ProfileEventType.BOOKMARK)],
        )

        assert parsed.events[0].label is None

    def test_does_not_require_events_to_arrive_sorted(self):
        """Unlike a series: there is one stream of events and `normalize` sorts it, where
        only a parser knows which sample timestamps belong to which sensor."""
        parsed = ParsedProfileSchema(
            depth=ParsedSeries(t=[0.0], v=[10]),
            events=[
                ParsedProfileEvent(t=2356.0, type=ProfileEventType.GAS_SWITCH, gas_number=2),
                ParsedProfileEvent(t=0.0, type=ProfileEventType.GAS_SWITCH, gas_number=1),
            ],
        )

        assert [event.t for event in parsed.events] == [2356.0, 0.0]

    def test_still_rejects_an_unsorted_ceiling_series(self):
        with pytest.raises(ValueError, match="ceiling: timestamps are not sorted"):
            ParsedProfileSchema(ceiling=ParsedSeries(t=[10.0, 1.0], v=[300, 600]))


class TestGetGasAttributionForDives:
    """The one DB-facing piece in this module, mocked at the session.

    Against the grain of the file's no-database style, and deliberately: what is worth
    pinning is not the SQL but the three shapes a stored column can hand back, one of
    which - a payload an older extractor wrote - is *guaranteed* to occur between a deploy
    and the backfill that follows it, on the dive detail page.
    """

    def _db(self, rows: list[SimpleNamespace]) -> AsyncMock:
        db = AsyncMock()
        db.execute = AsyncMock(return_value=rows)
        return db

    def _row(self, gas_attribution: object) -> SimpleNamespace:
        return SimpleNamespace(dive_id=7, duration=4300, gas_attribution=gas_attribution)

    @pytest.mark.asyncio
    async def test_reads_a_stored_attribution_back_with_the_span_it_was_derived_over(self):
        rows = [self._row([{"gas_number": 0, "seconds": 2075, "mean_depth_cm": 3399}])]

        attribution = await get_gas_attribution_for_dives(self._db(rows), dive_ids=[7])

        assert attribution[7].duration == 4300
        assert attribution[7].entries == [GasAttribution(gas_number=0, seconds=2075, mean_depth_cm=3399)]

    @pytest.mark.asyncio
    async def test_a_dive_with_no_profile_comes_back_empty_rather_than_absent(self):
        """ "Nothing to attribute" is what the caller does with either, so a dive that has
        no profile row must not be a `KeyError` at the call site."""
        attribution = await get_gas_attribution_for_dives(self._db([]), dive_ids=[7])

        assert attribution[7] == ProfileGasAttribution()

    @pytest.mark.asyncio
    async def test_a_row_written_before_attribution_existed_reads_as_nothing_attributed(self):
        """NULL is "the backfill has not reached this row", `[]` is "this extractor looked
        and found nothing" - and both mean the same thing to the caller."""
        attribution = await get_gas_attribution_for_dives(self._db([self._row(None)]), dive_ids=[7])

        assert attribution[7].entries == []

    @pytest.mark.asyncio
    async def test_an_unreadable_stored_shape_degrades_to_no_figure_rather_than_raising(self, caplog):
        """The branch that keeps a stale payload off the dive detail page's error path. An
        entry missing `seconds` is what a differently-shaped older extraction looks like;
        it must cost the dive its per-tank figure, not its whole response.
        """
        rows = [self._row([{"gas_number": 1}])]

        with caplog.at_level(logging.WARNING):
            attribution = await get_gas_attribution_for_dives(self._db(rows), dive_ids=[7])

        assert attribution[7].entries == []
        assert "unreadable gas attribution" in caplog.text
