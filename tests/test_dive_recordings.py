"""The three gates that decide whether two records are one recording, one dive, or neither.

Pure and DB-free, on `services/dive_recordings.py`'s own split: the gates are the decisions
worth testing and they are testable with two `RecordingFacts` and no database at all. The
row lifecycle they sit on top of - ordinals, promotion, the fill rule's writes - is
exercised against a live Postgres in `test_dive_recordings_rows.py`.

**Every case here carries the real numbers**, off the two computers this feature was built
from: a Suunto Ocean exported twice on 2026-09-08 (its app's JSON and its FIT), and a
Shearwater Perdix 3 whose dive was cut in half when the diver surfaced. Synthetic pairs
appear only where the corpus has no example - the device-counter branch, and a clean
two-device match - and are marked as such.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.app.schemas.parsed_dive import ParsedDevice
from src.app.services.dive_files import relabel_gas_numbers
from src.app.services.dive_recordings import (
    DeviceIdentity,
    RecordingFacts,
    delta_seconds,
    device_of,
    devices_differ,
    is_same_dive_loose,
    is_same_dive_strict,
    is_same_recording,
    same_device,
    wall_clock,
)

# 2026-09-08, Dahab. The Suunto stamps its start with the real offset; the Perdix's UDDF
# carries a `Z` its generator does not mean, so the converter reads it as a wall clock with
# no offset at all - which is what makes the *Clocks* rule the load-bearing line in the
# module rather than a nicety.
SUUNTO_INSTANT = datetime(2026, 9, 8, 12, 17, 38, 670000, tzinfo=UTC)  # 15:17:38.67+03:00
PERDIX_ONE_WALL = datetime(2026, 9, 8, 15, 18, 10, tzinfo=UTC)  # 15:18:10, labelled UTC
PERDIX_TWO_WALL = datetime(2026, 9, 8, 15, 21, 53, tzinfo=UTC)  # 15:21:53, labelled UTC

SUUNTO_DEVICE = DeviceIdentity(brand="Suunto", serial="253810000400")
SUUNTO_FIT_DEVICE = DeviceIdentity(brand="suunto", model="Suunto Ocean", dive_number=3)
PERDIX_DEVICE = DeviceIdentity(brand="Shearwater Research, Inc", model="Perdix 3", serial="D9772626")


def _suunto_json() -> RecordingFacts:
    """The app's JSON export: the device's own logged figures, and a sampled span 422 s
    longer than them - the file goes on sampling at the surface."""
    return RecordingFacts(
        device=SUUNTO_DEVICE,
        start_time=SUUNTO_INSTANT,
        utc_offset_minutes=180,
        duration=3051,
        max_depth=19.04,
        sampled_span=3_473_000,
    )


def _suunto_fit() -> RecordingFacts:
    """The same computer's FIT of the same dive, 0.67 s earlier by its own clock."""
    return RecordingFacts(
        device=SUUNTO_FIT_DEVICE,
        start_time=SUUNTO_INSTANT - timedelta(milliseconds=670),
        utc_offset_minutes=180,
        duration=3051,
        max_depth=19.04,
        sampled_span=3_473_000,
    )


def _perdix(part: int) -> RecordingFacts:
    """One half of the interrupted dive, as logbook import stores it.

    The figures are the samples' own, which is what an imported recording has: a DiveJSON
    Recording carries no scalars, so there is nowhere else to read them from.
    """
    if part == 1:
        return RecordingFacts(
            device=PERDIX_DEVICE, start_time=PERDIX_ONE_WALL, duration=180, max_depth=19.0, sampled_span=180_000
        )
    return RecordingFacts(
        device=PERDIX_DEVICE, start_time=PERDIX_TWO_WALL, duration=2940, max_depth=19.0, sampled_span=2_940_000
    )


class TestTheDeviceTest:
    """One rule under both halves: **a member absent on either side never makes two devices
    differ.** Only a member both sides carry, whose values disagree, does."""

    def test_one_computers_two_exports_are_one_device(self) -> None:
        """The corpus's Ocean: one serial in play, brands equal once folded, one model
        unknown. A case-sensitive comparison would call `suunto` and `Suunto` two makers and
        mint a second recording for one machine - the defect the whole feature exists to
        prevent."""
        assert same_device(SUUNTO_DEVICE, SUUNTO_FIT_DEVICE)
        assert not devices_differ(SUUNTO_DEVICE, SUUNTO_FIT_DEVICE)

    def test_equal_serials_settle_it_even_when_the_models_disagree(self) -> None:
        """A real pair: a Shearwater UDDF's `<model>Perdix 3</model>` beside the same
        computer's `.ssrf` `Shearwater Perdix 3`, both serial `D9772626`. Without the
        at-most-one-serial qualifier on the model branch they would satisfy *same* and
        *different* at once."""
        ssrf = DeviceIdentity(model="Shearwater Perdix 3", serial="D9772626")

        assert same_device(PERDIX_DEVICE, ssrf)
        assert not devices_differ(PERDIX_DEVICE, ssrf)

    def test_differing_serials_are_two_computers(self) -> None:
        assert devices_differ(SUUNTO_DEVICE, DeviceIdentity(brand="Suunto", serial="999999999999"))
        assert not same_device(SUUNTO_DEVICE, DeviceIdentity(brand="Suunto", serial="999999999999"))

    def test_two_devices_with_nothing_comparable_are_neither(self) -> None:
        """`devices_differ` is deliberately not `not same_device`. A pair may be neither, and
        reading "not the same" as "different" would let the strict gate fire on a pair it
        knows nothing about."""
        bare = DeviceIdentity()

        assert not devices_differ(bare, SUUNTO_DEVICE)
        # `same_device` is the permissive half of the same rule and does admit this pair,
        # which is what keeps an `.ssrf` reading with no brand matching its own FIT.
        assert same_device(bare, SUUNTO_DEVICE)

    def test_a_parsed_device_becomes_an_identity_and_an_absent_one_becomes_an_empty_one(self) -> None:
        """So every caller compares the same shape rather than branching on `None`."""
        assert device_of(ParsedDevice(brand="Suunto", serial="253810000400")) == SUUNTO_DEVICE
        assert device_of(None).is_empty


class TestTheClocksRule:
    """Δ is measured between instants when both sides carry an offset, and between wall
    clocks when either does not.

    Not a fallback. The Perdix carries no offset because its generator writes a local wall
    clock with a `Z` suffix, so a naive comparison of the two stored columns puts the pair
    three hours and change apart and no gate ever fires.
    """

    def test_the_naive_comparison_is_the_one_this_rule_replaces(self) -> None:
        assert (PERDIX_TWO_WALL - SUUNTO_INSTANT).total_seconds() == pytest.approx(11055, abs=1)

    def test_a_missing_offset_puts_both_on_the_clock_face(self) -> None:
        """255 s: 15:17:38 to 15:21:53, which is what the two divers' watches read."""
        delta = delta_seconds(SUUNTO_INSTANT, 180, PERDIX_TWO_WALL, None)

        assert delta == pytest.approx(255, abs=1)

    def test_two_offsets_are_compared_as_instants(self) -> None:
        """Where both sides know their offset there is nothing to reconstruct, and the
        instant is the sharper comparison - two computers set to different zones on one
        dive still pair."""
        one_hour_west = SUUNTO_INSTANT.astimezone(UTC)

        assert delta_seconds(SUUNTO_INSTANT, 180, one_hour_west, 60) == 0.0

    def test_an_offset_less_start_is_its_own_wall_clock(self) -> None:
        assert wall_clock(PERDIX_TWO_WALL, None) == PERDIX_TWO_WALL
        assert wall_clock(SUUNTO_INSTANT, 180) == SUUNTO_INSTANT + timedelta(hours=3)


class TestSameRecording:
    """A second file of a record that already exists."""

    def test_the_json_and_the_fit_of_one_dive_are_one_recording(self) -> None:
        """0.67 s apart, spans equal at 3473, one serial between them."""
        assert is_same_recording(_suunto_fit(), _suunto_json())
        assert is_same_recording(_suunto_json(), _suunto_fit())

    def test_the_two_perdix_parts_are_not(self) -> None:
        """223 s apart and 180 s against 2940 - the same computer's two records of one
        interrupted dive, which is the merge action's case and never a second file."""
        assert delta_seconds(PERDIX_ONE_WALL, None, PERDIX_TWO_WALL, None) == 223.0
        assert not is_same_recording(_perdix(1), _perdix(2))

    def test_differing_device_counters_refuse_a_pair_the_clock_would_admit(self) -> None:
        """Synthetic - no pair in the corpus carries counters on both sides. Two seconds
        apart, one sampled span, and only the counter to tell them apart: one computer's
        two dives, back to back."""
        first = RecordingFacts(
            device=DeviceIdentity(brand="Suunto", serial="253810000400", dive_number=1),
            start_time=SUUNTO_INSTANT,
            utc_offset_minutes=180,
            duration=600,
            max_depth=19.0,
            sampled_span=600_000,
        )
        second = RecordingFacts(
            device=DeviceIdentity(brand="Suunto", serial="253810000400", dive_number=2),
            start_time=SUUNTO_INSTANT + timedelta(seconds=2),
            utc_offset_minutes=180,
            duration=600,
            max_depth=19.0,
            sampled_span=600_000,
        )

        assert not is_same_recording(second, first)
        # And the strict gate *does* admit it: the same device reporting a different counter
        # is two records the diver may want on one dive, which is the only route by which a
        # single computer contributes two recordings.
        assert is_same_dive_strict(second, first)

    def test_a_stored_recording_that_names_no_device_matches_on_start_and_span(self) -> None:
        """The row logbook import created before this table existed, or from a document whose
        source said nothing about the computer. Refusing it would leave a diver unable to
        attach the very file that would say what recorded it."""
        deviceless = RecordingFacts(
            device=DeviceIdentity(), start_time=SUUNTO_INSTANT, utc_offset_minutes=180, sampled_span=3_473_000
        )

        assert is_same_recording(_suunto_fit(), deviceless)

    def test_spans_two_seconds_apart_are_the_edge(self) -> None:
        assert is_same_recording(replace(_suunto_fit(), sampled_span=3_475_000), _suunto_json())
        assert not is_same_recording(replace(_suunto_fit(), sampled_span=3_475_001), _suunto_json())


class TestAStoredRecordingWithNoStart:
    """What a date-only dive's import leaves where its document stated no start for a
    recording: the device and the span still decide, and the start clause is skipped rather
    than failed - otherwise that dive and the file that later records it would be two
    recordings of one computer."""

    def test_it_matches_its_own_device_on_device_and_span(self) -> None:
        assert is_same_recording(_suunto_fit(), replace(_suunto_json(), start_time=None, utc_offset_minutes=None))

    def test_it_matches_a_file_when_it_names_no_device_and_holds_no_samples(self) -> None:
        """A hand-logged date-only dive whose recording is a readout alone."""
        assert is_same_recording(_suunto_fit(), RecordingFacts(device=DeviceIdentity(), start_time=None))

    def test_another_computer_still_does_not_match_it(self) -> None:
        assert not is_same_recording(_perdix(1), replace(_suunto_json(), start_time=None, utc_offset_minutes=None))

    def test_the_span_still_refuses_it(self) -> None:
        stored = replace(_suunto_json(), start_time=None, utc_offset_minutes=None, sampled_span=1_000_000)

        assert not is_same_recording(_suunto_fit(), stored)

    def test_no_dive_gate_admits_a_start_nobody_stated(self) -> None:
        """The strict and loose gates are windows on the clock, and have nothing to hold."""
        stored = replace(_suunto_json(), device=PERDIX_DEVICE, start_time=None, utc_offset_minutes=None)

        assert not is_same_dive_strict(_suunto_fit(), stored)
        assert not is_same_dive_loose(_suunto_fit(), stored)


class TestSameDiveStrict:
    """A different computer's record of one dive - the gate logbook import attaches on."""

    def test_the_perdix_and_the_suunto_are_one_dive(self) -> None:
        """255 s apart against a window of 1525, 19.0 m against 19.04, 2940 s against 3051 -
        and the two figures come from different paths, which is the point: the Perdix's are
        its samples' and the Suunto's are its device's logged ones."""
        perdix, suunto = _perdix(2), _suunto_json()

        assert delta_seconds(PERDIX_TWO_WALL, None, SUUNTO_INSTANT, 180) == pytest.approx(255, abs=1)
        assert max(60.0, max(perdix.duration or 0, suunto.duration or 0) / 2) == 1525.5
        assert is_same_dive_strict(perdix, suunto)

    def test_two_devices_half_a_minute_apart_pass(self) -> None:
        """Synthetic, and the ordinary case this gate exists for: two computers on one
        diver, entering the water 32 s apart."""
        other = RecordingFacts(
            device=DeviceIdentity(brand="Garmin", model="Descent Mk2i", serial="3542000001"),
            start_time=SUUNTO_INSTANT + timedelta(seconds=32),
            utc_offset_minutes=180,
            duration=3040,
            max_depth=19.1,
            sampled_span=3_040_000,
        )

        assert is_same_dive_strict(other, _suunto_json())

    def test_one_computers_two_records_are_never_two_recordings(self) -> None:
        """Perdix #1 against #2: the same device with no counters on either side. That pair
        is the *merge* action's, and admitting it here would fold an interrupted dive into
        one dive with two recordings that both claim to be the whole of it."""
        assert not is_same_dive_strict(_perdix(2), _perdix(1))

    def test_the_perdix_first_part_is_its_own_dive_beside_the_suunto(self) -> None:
        """180 s against 3051 fails the five-minute duration clause outright, so part 1
        arrives as a dive of its own for the merge action to fold."""
        assert not is_same_dive_strict(_perdix(1), _suunto_json())

    def test_a_figure_one_side_carries_alone_refuses_the_match(self) -> None:
        """Subsurface's own absent rule, and it is *not* `same_device`'s: a recording
        claiming 45 m against one claiming nothing is not evidence of agreement."""
        nothing_recorded = RecordingFacts(
            device=DeviceIdentity(brand="Garmin", serial="3542000001"),
            start_time=SUUNTO_INSTANT + timedelta(seconds=32),
            utc_offset_minutes=180,
        )

        assert not is_same_dive_strict(nothing_recorded, _suunto_json())

    def test_a_zero_length_record_is_not_a_match(self) -> None:
        """`likely_same` refuses a pair when either duration is zero, and so does this: a
        device that logged nothing is not evidence of anything."""
        empty = RecordingFacts(
            device=DeviceIdentity(brand="Garmin", serial="3542000001"),
            start_time=SUUNTO_INSTANT,
            utc_offset_minutes=180,
            duration=0,
            max_depth=19.0,
        )

        assert not is_same_dive_strict(empty, replace(_suunto_json(), duration=0))

    def test_both_sides_carrying_neither_figure_still_pass_on_the_clock(self) -> None:
        """A figure *neither* side carries does not stop a match - two device-only records
        of one dive are still one dive."""
        left = RecordingFacts(device=SUUNTO_DEVICE, start_time=SUUNTO_INSTANT, utc_offset_minutes=180)
        right = RecordingFacts(
            device=DeviceIdentity(brand="Garmin", serial="3542000001"),
            start_time=SUUNTO_INSTANT + timedelta(seconds=32),
            utc_offset_minutes=180,
        )

        assert is_same_dive_strict(right, left)


class TestSameDiveLoose:
    """The start window alone - the candidates a form offers, where the diver decides."""

    def test_it_admits_what_the_strict_gate_refuses(self) -> None:
        """Perdix #1 fails strict on duration and depth; a form still has to offer it,
        because "my second computer surfaced early" is exactly the case a diver is looking
        at when they reach for *attach there*."""
        assert not is_same_dive_strict(_perdix(1), _suunto_json())
        assert is_same_dive_loose(_perdix(1), _suunto_json())

    def test_it_does_not_admit_a_dive_the_next_morning(self) -> None:
        next_day = RecordingFacts(
            device=SUUNTO_DEVICE,
            start_time=SUUNTO_INSTANT + timedelta(days=1),
            utc_offset_minutes=180,
            duration=3051,
            max_depth=19.04,
        )

        assert not is_same_dive_loose(next_day, _suunto_json())


class TestRelabellingASecondComputersCylinders:
    """`gas_number` is dive-scoped, so a second computer's labels are mapped onto the dive's
    own list: by mix first, then by order, unmatched appended with the next free label."""

    @staticmethod
    def _stored(*mixes: tuple[int, float | None, float | None]):
        from src.app.schemas.dive_mixture import DiveMixtureRead

        return [
            DiveMixtureRead(id=index, gas_number=number, oxygen=oxygen, helium=helium)
            for index, (number, oxygen, helium) in enumerate(mixes, start=1)
        ]

    @staticmethod
    def _parsed(*mixes: tuple[int, float | None, float | None]):
        from src.app.schemas.parsed_dive import DiveMixtureSchema

        return [
            DiveMixtureSchema(
                volume=None,
                start_pressure=None,
                end_pressure=None,
                oxygen=oxygen,
                helium=helium,
                po2_limit=None,
                gas_number=number,
                role=None,
                usage=None,
            )
            for number, oxygen, helium in mixes
        ]

    def test_the_mix_wins_over_the_position(self) -> None:
        """The second computer calls the deco bottle 1 and the back gas 2; the dive has them
        the other way round. Attributing the second computer's pressures by position would
        put its deco readings on the back gas."""
        mapping, appended = relabel_gas_numbers(
            self._parsed((1, 50.0, 0.0), (2, 21.0, 35.0)), self._stored((1, 21.0, 35.0), (2, 50.0, 0.0))
        )

        assert mapping == {1: 2, 2: 1}
        assert appended == []

    def test_position_is_the_fallback_where_no_mix_was_recorded(self) -> None:
        """A pair of air cylinders records no distinguishing fraction at all, which is what a
        2026 Suunto Ocean's reconstructed cylinders look like."""
        mapping, appended = relabel_gas_numbers(
            self._parsed((0, None, None), (1, None, None)), self._stored((3, None, None), (4, None, None))
        )

        assert mapping == {0: 3, 1: 4}
        assert appended == []

    def test_a_cylinder_the_dive_does_not_have_is_appended_with_the_next_free_label(self) -> None:
        """A real tank the second computer saw. Dropping it would lose a cylinder from the
        dive; reusing a label would attribute two tanks' pressures to one."""
        mapping, appended = relabel_gas_numbers(
            self._parsed((1, 21.0, 0.0), (2, 99.0, 0.0)), self._stored((1, 21.0, 0.0))
        )

        assert mapping == {1: 1, 2: 2}
        assert [row.gas_number for row in appended] == [2]

    def test_the_primary_recordings_own_labels_map_to_themselves(self) -> None:
        """The identity case, and the reason an empty map leaves a profile alone."""
        mapping, appended = relabel_gas_numbers(self._parsed((1, 32.0, 0.0)), self._stored((1, 32.0, 0.0)))

        assert mapping == {1: 1}
        assert appended == []
