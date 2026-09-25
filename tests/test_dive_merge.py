"""The decisions a merge makes, without a database: which dive survives, how two records
of one dive land on one axis, and what the surviving dive's figures come out as.

Everything here is pure, following the split `test_dive_recordings.py` keeps against
`test_dive_recordings_rows.py`: the gates and the arithmetic are testable on their own, and
what happens to the rows needs Postgres and lives next door.

**The numbers are the interrupted Perdix dive's**, and they are the whole reason this
feature exists: a computer that shut down mid-water wrote 180 seconds of one dive and then
2 940 seconds of it starting 223 seconds after the first record began - so the folded
profile is 314 samples covering 3 163 seconds with a 43-second hole in it, and nothing may
put a sample in that hole.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.app.core.schemas import NOTES_MAX_LENGTH
from src.app.schemas.dive_profile import ProfileEventType
from src.app.services.dive_merge import _Span, dive_figures, merged_notes
from src.app.services.dive_profiles import (
    NormalizedProfile,
    ProfileEvent,
    ProfilePressureSeries,
    ProfileSeries,
    attribute_and_cap,
    join_profiles,
    profile_from_data,
    shift_profile,
)
from src.app.services.dive_recordings import starts_before

# The first record: 19 samples at 10 s, ending at 180.
FIRST_PART_SAMPLES = 19
# The second: 295 at 10 s, its own axis running 0 to 2 940.
SECOND_PART_SAMPLES = 295
# Between the two records' starts, on the wall clock. Not between the two dives'.
RESTART_DELTA = 223
# 223 - 180: the stretch the computer was off, and the one thing a chart has to show.
GAP = 43


def _series(count: int, *, step: int = 10, first_value: int = 500) -> ProfileSeries:
    return ProfileSeries(t=[index * step for index in range(count)], v=[first_value + index for index in range(count)])


def _first_part() -> NormalizedProfile:
    return NormalizedProfile(depth=_series(FIRST_PART_SAMPLES))


def _second_part() -> NormalizedProfile:
    return NormalizedProfile(depth=_series(SECOND_PART_SAMPLES, first_value=900))


def _folded() -> NormalizedProfile:
    folded = attribute_and_cap(join_profiles(_first_part(), shift_profile(_second_part(), RESTART_DELTA)))
    assert folded is not None
    return folded


class TestFoldingTwoRecordsOntoOneAxis:
    def test_every_sample_of_both_records_survives(self) -> None:
        folded = _folded()

        assert folded.depth is not None
        assert len(folded.depth.t) == FIRST_PART_SAMPLES + SECOND_PART_SAMPLES == 314

    def test_the_second_records_first_sample_lands_at_the_restart_delta(self) -> None:
        """Its own axis begins at zero and the offset is the delta between the two
        *recordings'* starts, so `223 + 0` is where it goes."""
        folded = _folded()

        assert folded.depth is not None
        assert folded.depth.t[FIRST_PART_SAMPLES] == RESTART_DELTA + 0

    def test_the_gap_is_left_as_a_gap(self) -> None:
        """**No surface samples are synthesised.** The computer was off for 43 seconds and
        the profile says so by having nothing there - which is what DiveJSON §5.4 requires
        and what the chart's break-at-gaps rule already draws. Subsurface's
        `merge_one_sample` habit of filling the hole would invent a depth nobody measured.
        """
        folded = _folded()

        assert folded.depth is not None
        assert folded.depth.t[FIRST_PART_SAMPLES] - folded.depth.t[FIRST_PART_SAMPLES - 1] == GAP
        assert not [second for second in folded.depth.t if 180 < second < 223]

    def test_the_span_runs_to_the_last_sample_of_the_second_record(self) -> None:
        """223 plus the second record's own sampled 2 940. **Not** its logged end, which is
        2 921 and would give 3 144 - a dive claiming a span its own chart runs past."""
        folded = _folded()

        assert folded.duration == RESTART_DELTA + (SECOND_PART_SAMPLES - 1) * 10 == 3163

    def test_the_values_are_untouched_by_the_shift(self) -> None:
        """Only the axis moves. A shift that rounded or resampled would be inventing
        readings, which is the thing this whole path refuses to do."""
        folded = _folded()

        assert folded.depth is not None
        assert folded.depth.v == [*_first_part().depth.v, *_second_part().depth.v]  # type: ignore[union-attr]


class TestWhatJoiningTwoProfilesDoesToTheOtherChannels:
    def test_a_channel_only_one_record_carries_is_carried_whole(self) -> None:
        """A computer that owed a stop only after the restart wrote a ceiling channel on one
        of its two records and nothing on the other."""
        joined = join_profiles(
            NormalizedProfile(depth=_series(3)),
            NormalizedProfile(depth=ProfileSeries(t=[300], v=[400]), ceiling=ProfileSeries(t=[300], v=[300])),
        )

        assert joined is not None and joined.ceiling is not None
        assert joined.ceiling.t == [300]

    def test_pressure_is_joined_by_gas_number_and_not_by_position(self) -> None:
        """The two records are one computer's, so the tank it called 1 in the first is the
        tank it called 1 in the second - while a deco bottle first breathed after the
        restart appears in one list only and would land on the back gas if the lists were
        zipped.
        """
        joined = join_profiles(
            NormalizedProfile(pressure=[ProfilePressureSeries(gas_number=1, t=[0, 10], v=[2000, 1990])]),
            NormalizedProfile(
                pressure=[
                    ProfilePressureSeries(gas_number=2, t=[300], v=[2100]),
                    ProfilePressureSeries(gas_number=1, t=[300], v=[1500]),
                ]
            ),
        )

        assert joined is not None
        assert [(series.gas_number, series.t) for series in joined.pressure] == [(1, [0, 10, 300]), (2, [300])]

    def test_both_marker_streams_are_kept_and_sorted(self) -> None:
        """Unlike the fill rule one module over, which takes events whole from the first file
        of a recording: there the two streams are two devices' accounts of the same stretch,
        and here they are one device's accounts of two different stretches.
        """
        joined = join_profiles(
            NormalizedProfile(events=[ProfileEvent(t=60, type=ProfileEventType.GAS_SWITCH, gas_number=1)]),
            NormalizedProfile(events=[ProfileEvent(t=300, type=ProfileEventType.GAS_SWITCH, gas_number=2)]),
        )

        assert joined is not None
        assert [(event.t, event.gas_number) for event in joined.events] == [(60, 1), (300, 2)]

    def test_an_identical_marker_on_one_second_is_not_doubled(self) -> None:
        joined = join_profiles(
            NormalizedProfile(events=[ProfileEvent(t=60, type=ProfileEventType.BOOKMARK)]),
            NormalizedProfile(events=[ProfileEvent(t=60, type=ProfileEventType.BOOKMARK)]),
        )

        assert joined is not None
        assert len(joined.events) == 1

    def test_the_earlier_record_wins_a_second_they_both_claim(self) -> None:
        """A real pair cannot overlap - the second record starts after the first ended - but
        nothing in the data enforces that, and one value per second is the invariant every
        consumer relies on.
        """
        joined = join_profiles(
            NormalizedProfile(depth=ProfileSeries(t=[0, 10], v=[100, 200])),
            NormalizedProfile(depth=ProfileSeries(t=[10, 20], v=[999, 300])),
        )

        assert joined is not None and joined.depth is not None
        assert (joined.depth.t, joined.depth.v) == ([0, 10, 20], [100, 200, 300])

    @pytest.mark.parametrize("missing", ["earlier", "later"])
    def test_one_record_with_no_samples_leaves_the_other_whole(self, missing: str) -> None:
        """A half whose export carried a header and nothing else. The merge still folds the
        two records - the files and the dive move either way - and the profile is simply the
        half that has one.
        """
        part = NormalizedProfile(depth=_series(3))
        joined = join_profiles(None, part) if missing == "earlier" else join_profiles(part, None)

        assert joined is not None and joined.depth is not None
        assert joined.depth.t == [0, 10, 20]

    def test_neither_record_carrying_samples_joins_to_nothing(self) -> None:
        assert join_profiles(None, None) is None


class TestShiftingAProfile:
    def test_every_channel_and_every_marker_moves_together(self) -> None:
        """One axis, so a shift that moved the depth curve and not the temperature one would
        slide the two apart - the same reason `normalize` picks one origin for all of them.
        """
        shifted = shift_profile(
            NormalizedProfile(
                depth=ProfileSeries(t=[0, 10], v=[100, 200]),
                temperature=ProfileSeries(t=[5], v=[280]),
                pressure=[ProfilePressureSeries(gas_number=1, t=[0], v=[2000])],
                events=[ProfileEvent(t=7, type=ProfileEventType.BOOKMARK)],
            ),
            RESTART_DELTA,
        )

        assert shifted.depth is not None and shifted.temperature is not None
        assert shifted.depth.t == [223, 233]
        assert shifted.temperature.t == [228]
        assert shifted.pressure[0].t == [223]
        assert [event.t for event in shifted.events] == [230]

    def test_a_zero_shift_changes_nothing(self) -> None:
        """Which is the ordinary case: the surviving dive's own record is usually the earlier
        of the two, and it is the axis rather than the thing moved onto it."""
        original = NormalizedProfile(depth=ProfileSeries(t=[0, 10], v=[100, 200]))

        assert shift_profile(original, 0).depth == original.depth


class TestReadingAStoredProfileBack:
    def test_a_payload_round_trips_through_to_data(self) -> None:
        """The merge is the first thing in the app that has to *reason* about samples already
        stored, so the decoder has to be the exact inverse of the encoder."""
        original = NormalizedProfile(
            depth=ProfileSeries(t=[0, 10], v=[100, 200]),
            ceiling=ProfileSeries(t=[10], v=[300]),
            temperature=ProfileSeries(t=[0], v=[281]),
            pressure=[ProfilePressureSeries(gas_number=2, t=[0], v=[2000])],
            events=[ProfileEvent(t=5, type=ProfileEventType.GAS_SWITCH, gas_number=2, label="EAN50")],
        )

        assert profile_from_data(original.to_data()).to_data() == original.to_data()

    def test_a_channel_the_payload_omits_comes_back_absent(self) -> None:
        """`to_data` leaves a channel out rather than writing a null, so the decoder must
        never turn an absent key into an empty series - a dive with no ceiling would then
        claim a ceiling curve of no points."""
        decoded = profile_from_data({"depth": {"t": [0], "v": [100]}})

        assert decoded.ceiling is None and decoded.temperature is None
        assert decoded.pressure == [] and decoded.events == []


class TestWhichDiveSurvives:
    """The *Clocks* rule as an ordering. Same rule as the match gates' delta, because the two
    are asked of the same pair and an ordering derived any other way would contradict the
    distance the gates measured.
    """

    def test_two_dives_with_offsets_are_compared_as_instants(self) -> None:
        noon_utc = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

        assert starts_before(noon_utc, 180, noon_utc + timedelta(seconds=1), 180)
        assert not starts_before(noon_utc + timedelta(seconds=1), 180, noon_utc, 180)

    def test_an_offset_less_dive_is_compared_on_the_clock_face(self) -> None:
        """The corpus pair: a Perdix converted from Shearwater Cloud's UDDF carries no offset
        and stores `15:18:10` labelled UTC, while the Suunto on the other wrist stamps
        `15:17:38+03:00` - an instant of `12:17:38Z`. On the clock faces their divers read,
        the Suunto went in 32 seconds first.
        """
        perdix_wall_clock = datetime(2026, 9, 8, 15, 18, 10, tzinfo=UTC)
        suunto_instant = datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC)

        assert starts_before(suunto_instant, 180, perdix_wall_clock, None)
        assert not starts_before(perdix_wall_clock, None, suunto_instant, 180)

    def test_the_stored_columns_alone_would_order_the_pair_backwards(self) -> None:
        """The trap the rule exists for, on a pair chosen to expose it. An offset-less
        recording reading `15:17:00` on its own dial went in **before** a Suunto instant of
        `12:17:38Z`, which is `15:17:38` on its dial - while the two raw `timestamptz`
        columns say the opposite by nearly three hours. Comparing them naively folds the
        wrong dive into the wrong one, which is the thing a merge cannot take back.
        """
        offset_less = datetime(2026, 9, 8, 15, 17, 0, tzinfo=UTC)
        instant = datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC)

        assert offset_less > instant
        assert starts_before(offset_less, None, instant, 180)

    def test_equal_starts_are_neither_before_the_other(self) -> None:
        """Strict on purpose: the caller breaks the tie itself, so the same two uuids merge
        the same way round whichever order the request named them in."""
        noon = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

        assert not starts_before(noon, 0, noon, 0)
        assert not starts_before(noon, 180, noon - timedelta(minutes=180), None)


class TestTheSurvivingDivesFigures:
    def _span(self, *, minutes: int = 0, duration: int | None, max_depth_cm: int | None = None) -> _Span:
        """`duration` is the profile's span, in the axis's milliseconds."""
        return _Span(
            start_time=datetime(2026, 9, 8, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes),
            utc_offset_minutes=180,
            duration=duration,
            max_depth_cm=max_depth_cm,
        )

    def test_the_span_runs_from_the_earliest_recording_to_the_last_sample(self) -> None:
        """The folded case: one recording covering 3 163 seconds from its own start - and the
        dive's duration is whole seconds, the span's milliseconds divided."""
        duration, _ = dive_figures([self._span(duration=3_163_000)])

        assert duration == 3163

    def test_a_second_computers_recording_is_placed_by_its_own_start(self) -> None:
        """The appended case. The second computer went in two minutes later and surfaced
        after the first, so the dive runs to *its* last sample rather than to the primary
        recording's.
        """
        duration, _ = dive_figures([self._span(duration=1_800_000), self._span(minutes=2, duration=1_800_000)])

        assert duration == 120 + 1800

    def test_the_deepest_reading_wins_wherever_it_came_from(self) -> None:
        _, max_depth = dive_figures(
            [self._span(duration=100, max_depth_cm=1900), self._span(duration=100, max_depth_cm=1904)]
        )

        assert max_depth == 19.04

    def test_a_recording_with_no_samples_still_offers_its_depth(self) -> None:
        """A depth is a reading rather than an instant, so it needs no axis to count - while
        a record with nothing to place contributes no span."""
        duration, max_depth = dive_figures(
            [_Span(start_time=None, utc_offset_minutes=None, duration=None, max_depth_cm=2500)]
        )

        assert (duration, max_depth) == (None, 25.0)

    def test_nothing_to_read_leaves_both_figures_unanswered(self) -> None:
        """`None` is the caller's signal to leave the dive's own numbers alone, which is not
        the same as clearing them: a merge of two profile-less recordings must not blank a
        duration the diver typed."""
        assert dive_figures([]) == (None, None)

    def test_a_wall_clock_recording_is_placed_against_an_instant_one(self) -> None:
        """Mixed offsets degenerate the whole set to clock faces, which is what the pairwise
        rule already says for every pair involving the offset-less member. Comparing the
        columns naively would put these three hours apart and claim a four-hour dive.
        """
        duration, _ = dive_figures(
            [
                _Span(
                    start_time=datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC),
                    utc_offset_minutes=180,
                    duration=1_800_000,
                    max_depth_cm=None,
                ),
                _Span(
                    start_time=datetime(2026, 9, 8, 15, 18, 10, tzinfo=UTC),
                    utc_offset_minutes=None,
                    duration=1_800_000,
                    max_depth_cm=None,
                ),
            ]
        )

        assert duration == 32 + 1800


class TestTheNotesTheOtherDiveLeavesBehind:
    def test_the_other_dives_notes_arrive_under_a_line_naming_it(self) -> None:
        merged = merged_notes("Great viz.", "Lost the group at the wreck.", dive_number=215)

        assert merged == "Great viz.\n\nNotes from dive 215, merged into this one:\nLost the group at the wreck."

    def test_a_dive_with_no_notes_of_its_own_still_gets_the_heading(self) -> None:
        """The heading is what says where the prose came from, and that is worth as much on a
        dive whose own notes were empty."""
        assert merged_notes("", "Second half.", dive_number=215) == (
            "Notes from dive 215, merged into this one:\nSecond half."
        )

    def test_nothing_is_written_when_the_other_dive_had_nothing_to_say(self) -> None:
        """A heading over an empty section is noise in a field the diver owns."""
        assert merged_notes("Great viz.", "   \n ", dive_number=215) == "Great viz."

    def test_the_result_stays_within_the_notes_length_limit(self) -> None:
        """Two dives each inside the limit are not, and the merged dive has to survive its own
        read schema - which validates the length and would 500 the very dive this produced."""
        merged = merged_notes("a" * NOTES_MAX_LENGTH, "b" * NOTES_MAX_LENGTH, dive_number=215)

        assert len(merged) == NOTES_MAX_LENGTH
