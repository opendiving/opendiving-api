"""Unit tests for a stored profile's pure half: downsampling, the gas attribution, the fill,
the currency test and the wire mapping.

Inline fixtures and no database: everything worth testing here is a pure function reshaping
arrays. How a file's samples come to be a `NormalizedProfile` is `test_dive_reader.py`'s.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import divejson
import pytest

from src.app.schemas.dive_profile import (
    MILLISECONDS_PER_SECOND,
    GasAttribution,
    ProfileEventType,
    ProfileProvenance,
)
from src.app.services.dive_files import extract_file
from src.app.services.dive_profiles import (
    _PROVENANCE_BY_PARSER_KEY,
    IMPORT_PARSER_KEY,
    MAX_EVENTS,
    MAX_POINTS_PER_CHANNEL,
    MERGE_PARSER_KEY,
    READER_VERSION,
    UNREPRODUCIBLE_PROVENANCES,
    ExistingProfileRow,
    LoadedProfile,
    NormalizedProfile,
    ProfileEvent,
    ProfileGasAttribution,
    ProfilePressureSeries,
    ProfileSeries,
    attribute_and_cap,
    derive_gas_attribution,
    downsample,
    fill_channels,
    get_gas_attribution_for_dives,
    join_profiles,
    profile_from_data,
    provenance_of,
    shift_profile,
    should_extract,
    summary_extremes,
    to_read_schema,
    to_recording_read_schema,
)


def _series(seconds: list[float], values: list[int]) -> ProfileSeries:
    """A channel on the stored axis, from seconds - which is what every case below reasons in."""
    return ProfileSeries(t=[round(second * MILLISECONDS_PER_SECOND) for second in seconds], v=values)


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


def _switch(t: float, gas_number: int | None) -> ProfileEvent:
    return ProfileEvent(t=round(t * MILLISECONDS_PER_SECOND), type=ProfileEventType.GAS_SWITCH, gas_number=gas_number)


def _dive_on_two_gases() -> NormalizedProfile:
    """The corpus's commonest tech shape, in miniature: a back gas breathed deep, a switch
    to a deco bottle, and a shallow stop on it. Depth every 100 s, in centimeters.
    """
    return NormalizedProfile(
        depth=_series([0, 100, 200, 300, 400], [3000, 3000, 3000, 600, 600]),
        events=[_switch(0, 1), _switch(300, 2)],
    )


class TestDeriveGasAttribution:
    """Which cylinder was breathed for how long, and how deep - the fact Phase 4 turns
    into per-tank consumption. See `derive_gas_attribution` for why gas-switch events are
    the only source it will take.
    """

    def test_splits_a_two_gas_dive_at_the_switch(self):
        attribution = derive_gas_attribution(_dive_on_two_gases())

        # 0-300 s on gas 1 at a flat 30 m; 300-400 s on gas 2 at 6 m. The dive's own
        # average depth - 24 m - describes neither, which is the whole point.
        assert [(entry.gas_number, entry.seconds, entry.mean_depth_cm) for entry in attribution] == [
            (1, 300, 3000),
            (2, 100, 600),
        ]

    def test_a_gas_returned_to_is_one_entry_with_the_time_added_up(self):
        """A diver who goes back to their back gas for the ascent has two stretches on it
        and one cylinder, and it is the cylinder the pressures belong to."""
        profile = NormalizedProfile(
            depth=_series([0, 100, 200, 300], [3000, 600, 3000, 1500]),
            events=[_switch(0, 1), _switch(100, 2), _switch(200, 1)],
        )

        attribution = derive_gas_attribution(profile)

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(1, 200), (2, 100)]
        # Mean over both stretches on gas 1 - 30 m, then 30 m and 15 m after the switch
        # back - and not over the 6 m spent on the deco bottle in between.
        assert attribution[0].mean_depth_cm == 2500

    def test_the_same_gas_selected_twice_running_is_one_stretch(self):
        """Suunto writes this whenever a diver browses the gas list without changing
        anything - `Dive_2025-03-08-1440` records switches to gas 2 at 3 081 s and 3 087 s."""
        profile = NormalizedProfile(
            depth=_series([0, 100, 200], [3000, 3000, 3000]), events=[_switch(0, 1), _switch(100, 1)]
        )

        attribution = derive_gas_attribution(profile)

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(1, 200)]

    def test_a_switch_before_the_first_sample_is_the_gas_the_dive_started_on(self):
        """The ordinary case for a Suunto: the opening selection is recorded at t=0 while
        its first sample lands seconds later."""
        profile = NormalizedProfile(depth=_series([10, 110], [3000, 3000]), events=[_switch(0, 1)])

        attribution = derive_gas_attribution(profile)

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(1, 100)]

    def test_time_before_the_first_switch_is_left_unattributed(self):
        """Nothing says what was breathed then. The shortfall is what
        `compute_multi_tank_gas_use` reports as `attributed_seconds` rather than dividing a
        cylinder's gas by less time than it was breathed for."""
        profile = NormalizedProfile(depth=_series([0, 100, 200, 300], [3000] * 4), events=[_switch(200, 2)])

        attribution = derive_gas_attribution(profile)

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(2, 100)]

    def test_a_file_with_no_switches_attributes_nothing(self):
        """No single-gas fallback: a one-cylinder dive already yields a figure through
        `compute_gas_use`, so inferring one here would be a second answer to a question
        that already has one."""
        profile = NormalizedProfile(
            depth=_series([0, 100], [3000, 3000]),
            pressure=[ProfilePressureSeries(gas_number=1, t=[0, 100_000], v=[2052, 1800])],
        )

        assert derive_gas_attribution(profile) == []

    def test_a_switch_that_does_not_say_which_gas_attributes_nothing(self):
        """A file can record that a switch happened without saying to what, and the reader
        keeps a null `gas_number` rather than guessing. There is nothing here to join a
        cylinder to."""
        profile = NormalizedProfile(depth=_series([0, 100], [3000, 3000]), events=[_switch(50, None)])

        assert derive_gas_attribution(profile) == []

    def test_a_switch_after_the_last_sample_is_ignored(self):
        """A marker can be pressed after the final sample, and there is no dive left after
        the recording stops to attribute to it."""
        profile = NormalizedProfile(depth=_series([0, 100], [3000, 3000]), events=[_switch(0, 1), _switch(500, 2)])

        attribution = derive_gas_attribution(profile)

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(1, 100)]

    def test_two_switches_on_one_second_keep_the_later_gas(self):
        """What the diver ended up on is what they breathed."""
        profile = NormalizedProfile(depth=_series([0, 100], [3000, 3000]), events=[_switch(0, 1), _switch(0, 2)])

        attribution = derive_gas_attribution(profile)

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(2, 100)]

    def test_a_gas_with_no_depth_sample_of_its_own_is_dropped(self):
        """It has a time but no depth to normalize it against, and a borrowed one would be
        a number the file never recorded."""
        profile = NormalizedProfile(
            depth=_series([0, 100], [3000, 3000]), events=[_switch(0, 1), _switch(50, 2), _switch(60, 1)]
        )

        attribution = derive_gas_attribution(profile)

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
        profile = NormalizedProfile(
            depth=_series([0, 100, 200], [3000, 3000, 3000]), events=[_switch(0, 1), _switch(200, 2)]
        )

        attribution = derive_gas_attribution(profile)

        assert [(entry.gas_number, entry.seconds) for entry in attribution] == [(1, 200)]

    def test_a_profile_with_no_depth_channel_attributes_nothing(self):
        """A pressure-and-temperature-only export has no depth for a mean to be taken of,
        and every figure downstream is normalized against depth."""
        profile = NormalizedProfile(temperature=_series([0, 100], [260, 259]), events=[_switch(0, 1)])

        assert derive_gas_attribution(profile) == []

    def test_reads_the_switches_a_two_gas_xml_export_nests_in_its_mixtures(self):
        """End to end from the bytes, on the shape the corpus actually holds: the DM5 export
        keeps each cylinder's gas changes inside its own `<DiveMixture>`, and the reader
        labels the two cylinders 0 and 1 by position. Samples from `<Time>1</Time>` and gas
        changes from 0, so the opening selection is clipped forward to the first sample."""
        samples = "".join(
            f"<Dive.Sample><Depth>{'30.0' if index < 3 else '6.0'}</Depth><Time>{1 + index * 100}</Time></Dive.Sample>"
            for index in range(5)
        )
        mixtures = "".join(
            f"<DiveMixture><DiveGasChanges><DiveGasChange><GasChangeTime>{time}</GasChangeTime></DiveGasChange>"
            f"</DiveGasChanges><Oxygen>{oxygen}</Oxygen></DiveMixture>"
            for time, oxygen in ((0, 21), (300, 49))
        )
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"><StartTime>2025-06-03T12:15:30</StartTime>
<Duration>401</Duration><MaxDepth>30</MaxDepth><DiveMixtures>{mixtures}</DiveMixtures>
<DiveSamples>{samples}</DiveSamples></Dive>""".encode()

        profile = attribute_and_cap(extract_file(content, "suunto_xml").profile)

        assert profile is not None
        assert [(entry.gas_number, entry.seconds, entry.mean_depth_cm) for entry in profile.gas_attribution] == [
            (0, 299, 3000),
            (1, 101, 600),
        ]


class TestAttributeAndCap:
    def test_attributes_before_downsampling_so_the_mean_is_of_the_dive(self):
        """The load-bearing ordering in `attribute_and_cap`. Min/max bucketing keeps each
        bucket's extremes and discards what lies between them, so a mean taken afterwards
        would be a mean of the dive's peaks and troughs. This dive spends most of its time
        at 10 m with a brief excursion to 40 m every hundred samples, so the sawtooth is
        deliberately lopsided.
        """
        depths = [4000 if index % 100 == 0 else 1000 for index in range(2000)]
        profile = attribute_and_cap(
            NormalizedProfile(depth=_series([float(index) for index in range(2000)], depths), events=[_switch(0, 1)])
        )

        assert profile is not None
        # The honest mean: 1% of the samples at 40 m, the rest at 10 m.
        assert profile.gas_attribution[0].mean_depth_cm == round(sum(depths) / len(depths))
        # And the channel really was thinned, so the mean could not have been taken from it.
        assert profile.depth is not None and len(profile.depth.t) < len(depths)
        assert profile.gas_attribution[0].mean_depth_cm != round(sum(profile.depth.v) / len(profile.depth.v))

    def test_attributed_time_never_exceeds_the_span_it_is_a_fraction_of(self):
        """The invariant `attributed_seconds`/`duration` exists to state, and the one place
        the two halves can disagree: attribution is derived from the full-resolution channel
        while the stored span comes off the thinned one. A 77-minute 1 Hz dive - the cadence
        every FIT export uses, and past `MAX_POINTS_PER_CHANNEL` within twenty minutes -
        ending in a flat stretch at the surface is what used to make the denominator the
        shorter of the two, and a client print `100.2%`.
        """
        depths = [min(3000, second * 10) if second < 4_500 else 0 for second in range(4_620)]
        profile = attribute_and_cap(
            NormalizedProfile(
                depth=_series([float(second) for second in range(4_620)], depths),
                events=[_switch(0, 1), _switch(3000, 2)],
            )
        )

        assert profile is not None
        # `seconds` is whole seconds and the span milliseconds, so the fraction divides the span.
        span = profile.duration / MILLISECONDS_PER_SECOND
        assert sum(entry.seconds for entry in profile.gas_attribution) <= span
        # And exactly equal here, since the dive begins on a gas and never stops being on one.
        assert sum(entry.seconds for entry in profile.gas_attribution) == span

    def test_attribution_between_whole_seconds_still_fits_the_rounded_span(self):
        """Rounding each gas's total on its own gives 2356 + 2255 here, a second past the span."""
        times = [float(second) for second in range(0, 4_610, 10)] + [4_610.2]
        profile = attribute_and_cap(
            NormalizedProfile(depth=_series(times, [1500] * len(times)), events=[_switch(0, 1), _switch(2_355.6, 2)])
        )

        assert profile is not None
        assert [(entry.gas_number, entry.seconds) for entry in profile.gas_attribution] == [(1, 2356), (2, 2254)]
        assert sum(entry.seconds for entry in profile.gas_attribution) == round(
            profile.duration / MILLISECONDS_PER_SECOND
        )

    def test_nothing_in_is_nothing_out(self):
        assert attribute_and_cap(None) is None


def _row(
    source_sha256: str, extractor_version: int, parser_key: str = "suunto_xml", reader_version: str | None = "1.0.0"
) -> ExistingProfileRow:
    return ExistingProfileRow(
        source_sha256=source_sha256,
        extractor_version=extractor_version,
        parser_key=parser_key,
        reader_version=reader_version,
    )


class TestShouldExtract:
    @pytest.mark.parametrize(
        ("existing", "sha256", "version", "reader", "expected"),
        [
            (None, "abc", 1, "1.0.0", "extract"),
            (_row("abc", 1), "abc", 1, "1.0.0", "skip"),
            (_row("def", 1), "abc", 1, "1.0.0", "extract"),
            (_row("abc", 1), "abc", 2, "1.0.0", "extract"),
            # A row written by a *newer* extractor than this build is also "not current",
            # and re-extracting is the honest answer - it is what this build can vouch for.
            (_row("abc", 3), "abc", 2, "1.0.0", "extract"),
            # The reader is the third input, and every way it can differ is "not current": an
            # older package, a newer one, and none at all - every row stored before the column.
            (_row("abc", 1, reader_version="0.9.0"), "abc", 1, "1.0.0", "extract"),
            (_row("abc", 1, reader_version="1.1.0"), "abc", 1, "1.0.0", "extract"),
            (_row("abc", 1, reader_version=None), "abc", 1, "1.0.0", "extract"),
            # **Provenance beats all three.** A profile a document supplied or a merge
            # produced is never re-extracted, whatever its digest and versions say, because
            # nothing on this instance can produce those samples a second time - and for a
            # merged recording what the files *would* yield is one half of what the profile
            # describes.
            (_row("abc", 1, parser_key="divejson_import", reader_version=None), "different", 1, "1.0.0", "skip"),
            (_row("abc", 1, parser_key="merge", reader_version=None), "different", 99, "1.0.0", "skip"),
        ],
    )
    def test_table(self, existing, sha256, version, reader, expected):
        assert should_extract(existing, sha256=sha256, version=version, reader_version=reader) == expected

    def test_the_default_reader_is_the_installed_package(self):
        """The version a profile is stored under is the one it is later checked against."""
        assert READER_VERSION == divejson.__version__
        assert should_extract(_row("abc", 1, reader_version=READER_VERSION), sha256="abc", version=1) == "skip"


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

    Grouped rather than folded into `TestToData` above because the point is that every
    stage treats them exactly as it treats depth - and a stage that named the four original
    channels and forgot the new ones is what these would catch.
    """

    def _profile(self) -> NormalizedProfile:
        return NormalizedProfile(
            depth=_series([0, 10], [3000, 2400]),
            ndl=_series([0, 10], [5940, 0]),
            tts=_series([10], [268]),
            ppo2=_series([0], [96]),
            cns=_series([10], [800]),
            gradient_factor=_series([10], [398]),
            surface_gradient_factor=_series([10], [116]),
        )

    def test_to_data_and_back_is_the_same_profile(self):
        profile = self._profile()

        assert profile_from_data(profile.to_data()) == profile

    def test_a_zero_is_a_reading_and_survives_the_round_trip(self):
        """An NDL of zero is the moment a dive stopped being a no-decompression dive, which
        is the one reading a decompression dive most needs."""
        profile = self._profile()

        assert profile.ndl.v == [5940, 0]
        assert profile_from_data(profile.to_data()).ndl.v == [5940, 0]

    def test_channels_lists_them_in_the_formats_order(self):
        """`pressure` between `temperature` and the computed channels, which is where §6.4
        puts it - and what a chart stacks its curves in."""
        profile = NormalizedProfile(
            depth=_series([0], [3000]),
            temperature=_series([0], [219]),
            pressure=[ProfilePressureSeries(gas_number=1, t=[0], v=[2052])],
            ndl=_series([0], [5940]),
            gradient_factor=_series([0], [64]),
        )

        assert profile.channels == ["depth", "temperature", "pressure", "ndl", "gradient_factor"]

    def test_each_channel_is_capped_independently(self):
        long = _series([float(second) for second in range(9_000)], list(range(9_000)))
        profile = downsample(NormalizedProfile(depth=long, ndl=long, cns=long))

        assert len(profile.ndl.t) <= MAX_POINTS_PER_CHANNEL
        assert len(profile.cns.t) <= MAX_POINTS_PER_CHANNEL
        assert max(profile.ndl.v) == 8_999

    def test_a_second_file_fills_a_channel_the_first_did_not_carry(self):
        base = NormalizedProfile(depth=_series([0], [3000]))
        addition = NormalizedProfile(ppo2=_series([0], [96]))

        filled = fill_channels(base, addition)

        assert filled.depth.v == [3000]
        assert filled.ppo2.v == [96]

    def test_a_channel_the_first_file_carried_is_never_overwritten(self):
        base = NormalizedProfile(ndl=_series([0], [5940]))
        addition = NormalizedProfile(ndl=_series([0], [10]))

        assert fill_channels(base, addition).ndl.v == [5940]

    def test_shifting_moves_every_channel_onto_one_clock(self):
        profile = self._profile()

        moved = shift_profile(profile, 223_000)

        assert moved.ndl.t == [223_000, 233_000]
        assert moved.cns.t == [233_000]

    def test_shifting_back_past_zero_clamps_there_keeping_the_later_reading(self):
        """A file whose clock started before the recording's: what lands before zero is
        kept at zero, and where two land there the later reading stands."""
        profile = self._profile()

        moved = shift_profile(profile, -10_000)

        assert moved.ndl.t == [0]
        assert moved.ndl.v == [0]
        assert moved.depth.v == [2400]

    def test_joining_two_records_of_one_dive_joins_every_channel(self):
        earlier = NormalizedProfile(tts=_series([0], [120]))
        later = shift_profile(NormalizedProfile(tts=_series([0], [60])), 300)

        joined = join_profiles(earlier, later)

        assert joined.tts.t == [0, 300]
        assert joined.tts.v == [120, 60]

    def test_the_summary_extremes_are_per_quantity(self):
        """`min` for the no-decompression clock, because the *maximum* is the device's
        display cap on almost every recreational dive and says nothing; `max` for the rest.
        """
        extremes = summary_extremes(self._profile())

        assert extremes["min_ndl_s"] == 0
        assert extremes["max_tts_s"] == 268
        assert extremes["max_ppo2_bar100"] == 96
        assert extremes["max_cns_pct10"] == 800
        assert extremes["max_gradient_factor_pct"] == 398
        assert extremes["max_surface_gradient_factor_pct"] == 116

    def test_a_channel_this_profile_has_none_of_clears_its_column(self):
        """Every column is written, `None` included: a rewrite that left one out would leave
        the previous profile's extreme standing beside samples that no longer contain it."""
        extremes = summary_extremes(NormalizedProfile(depth=_series([0], [3000])))

        assert extremes["min_ndl_s"] is None
        assert extremes["max_surface_gradient_factor_pct"] is None
        assert extremes["max_depth_cm"] == 3000

    def test_a_gradient_factor_past_100_is_stored_as_written(self):
        """A GF99 runs into four figures on a real Suunto decompression ascent. Suunto
        publishes no definition of the field and nothing in the file accounts for the size,
        so a cap would be a guess wearing a plausible number."""
        profile = NormalizedProfile(gradient_factor=_series([0], [12_575]))

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

    `parser_key` is overloaded on purpose - a format id, or one of two sentinels - which is
    right for a column that also has to answer "can this be extracted again" and wrong for a
    member a client switches on.
    """

    def test_every_format_the_reader_reads_reads_as_file(self):
        """Every format, rather than the one this suite happens to write: the wire member
        must not grow a value when the reader gains one."""
        assert {provenance_of(fmt) for fmt in divejson.read_formats()} == {ProfileProvenance.FILE}

    def test_each_sentinel_reads_as_itself(self):
        assert provenance_of(IMPORT_PARSER_KEY) is ProfileProvenance.DIVEJSON_IMPORT
        assert provenance_of(MERGE_PARSER_KEY) is ProfileProvenance.MERGE

    def test_every_unreproducible_provenance_has_a_wire_value(self):
        """The guard on the fallback. Anything not named in the mapping reads as `FILE` -
        "read off this recording's stored files, and read from them again" - which is the one
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
