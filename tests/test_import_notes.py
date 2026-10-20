"""Which of a file's findings a diver sees, and where, without a database.

The comparison is pinned on hand-made dives against hand-made stored ones, so each rule is
one case: a line goes when the dive does not hold this file's own value at its member, a
stand-in a file writes for what it lacks is that file's own, and a member the map cannot
place keeps its line. The suite's batch tests run the same rules over real files.
"""

import uuid as uuid_pkg
from typing import Any

import divejson
import pytest

from src.app.schemas.dive_mixture import DiveMixtureRead
from src.app.schemas.logbook_import import IMPORT_NOTE_KINDS, ImportDive, ImportDiveNote, ImportDocument, ImportNoteCode
from src.app.services import recording_shape
from src.app.services.logbook_import import notes, planner
from src.app.services.logbook_import.notes import DiveSource, StoredDive, findings, grouped, left_out

DIVE = uuid_pkg.uuid4()
DIVE_ID = 7


def _dive(**members: Any) -> ImportDive:
    return ImportDive.model_validate({"uuid": str(DIVE), "started_at": "2026-04-17T09:30:00+02:00", **members})


def _source(
    dive: ImportDive, *, inferred: frozenset[str] = frozenset(), recordings: dict[int, tuple[int, int]] | None = None
) -> DiveSource:
    return DiveSource(dive=dive, index=0, converted=True, inferred=inferred, recordings=recordings or {})


def _stored(
    cylinders: tuple[dict[str, Any], ...] = (), readouts: dict[int, dict[str, Any]] | None = None, **values: Any
) -> StoredDive:
    defaults = {"utc_offset": 120, "seconds": (0, 0), "date_only": False}
    return StoredDive(
        values={**defaults, **values},
        readouts=readouts or {},
        cylinders=[DiveMixtureRead(id=index + 1, **row) for index, row in enumerate(cylinders)],
    )


def _note(member: str | None, kind: str = "absent", code: str = "a_code") -> divejson.Note:
    return divejson.Note("dive.fit/dive/0", "a sentence", kind, code=code, member=member)  # type: ignore[arg-type]


def _left_out(note: divejson.Note, source: DiveSource, stored: StoredDive, *others: DiveSource) -> bool:
    return left_out(note, source, others, DIVE_ID, stored)


class TestTheCatalogue:
    def test_every_hidden_code_is_one_the_package_names(self) -> None:
        """A code the package renames or drops fails the bump that brings it, rather than
        unhiding its finding without anybody noticing."""
        assert notes.HIDDEN <= set(divejson.NOTE_CODES)

    def test_no_code_of_the_package_spells_one_of_the_import_s(self) -> None:
        """A row's `code` carries both vocabularies in one string."""
        assert not set(divejson.NOTE_CODES) & {code.value for code in ImportNoteCode}

    def test_an_import_note_s_kind_is_one_of_the_package_s(self) -> None:
        assert set(IMPORT_NOTE_KINDS.values()) <= {"absent", "inferred", "resolved", "dropped"}

    def test_every_bounded_value_has_a_diver_s_name(self) -> None:
        bounds = [
            *planner.DIVE_BOUNDS,
            *planner._SITE_BOUNDS,
            *planner._GEAR_BOUNDS,
            *planner._SCHEDULE_BOUNDS,
            *planner._SIGHTING_BOUNDS,
            *planner._MIXTURE_BOUNDS,
            *recording_shape.READOUT_BOUNDS,
            *recording_shape.DECO_MODEL_BOUNDS,
        ]
        assert {bound.field for bound in bounds} <= set(recording_shape.VALUE_LABELS)


class TestWhatEachFindingIsAbout:
    def _found(self, *found: divejson.Note) -> list[tuple[str, uuid_pkg.UUID | None, str | None]]:
        site = uuid_pkg.uuid4()
        document = ImportDocument.model_validate(
            {"format": "divejson", "version": "1.0", "dives": [{"uuid": str(DIVE)}], "sites": [{"uuid": str(site)}]}
        )
        sources = {str(DIVE): "log.ssrf/dive/1", str(site): "log.ssrf/site/0"}
        return [
            (one.note.where, one.owner, one.collection)
            for one in findings(document, divejson.Conversion({}, found, sources))
        ]

    def test_a_finding_belongs_to_the_record_with_the_longest_path_it_starts_with(self) -> None:
        found = self._found(
            divejson.Note("log.ssrf/dive/1/sample/30.5", "x", "absent", code="a"),
            divejson.Note("log.ssrf/dive/10", "x", "absent", code="a"),
            divejson.Note("log.ssrf/site/0", "x", "absent", code="a"),
            divejson.Note("log.ssrf/$", "x", "absent", code="a"),
        )
        assert [(owner == DIVE, collection) for _, owner, collection in found] == [
            (True, "dives"),
            (False, None),
            (False, "sites"),
            (False, None),
        ]

    def test_a_hidden_finding_is_nowhere(self) -> None:
        assert self._found(divejson.Note("log.ssrf/dive/1", "x", "absent", code="identity_from_position")) == []

    def test_a_file_nothing_converted_has_no_findings(self) -> None:
        assert findings(ImportDocument.model_validate({"format": "divejson", "version": "1.0"}), None) == []


class TestTheComparison:
    def test_a_loss_is_never_left_out(self) -> None:
        source = _source(_dive())
        assert not _left_out(_note("dives/0/max_depth", "dropped"), source, _stored(max_depth=20.0))

    def test_a_finding_the_map_cannot_place_keeps_its_line(self) -> None:
        source = _source(_dive())
        stored = _stored(max_depth=20.0)
        for member in (None, "dives/0/a_member_of_a_later_release", "dives/1/max_depth", "diver/name"):
            assert not _left_out(_note(member), source, stored)
        assert not _left_out(_note("dives/0/started_at", code="a_part_of_a_start_nobody_named"), source, stored)

    def test_an_absence_another_file_filled_goes_and_one_nobody_filled_stays(self) -> None:
        source = _source(_dive())
        assert _left_out(_note("dives/0/max_depth"), source, _stored(max_depth=20.0))
        assert not _left_out(_note("dives/0/max_depth"), source, _stored(max_depth=None))

    def test_a_stand_in_the_file_writes_is_its_own(self) -> None:
        """A FIT with no time zone gives its start in UTC, stored as offset zero: on its own
        it is the dive's, and once another file gave the dive its zone it is not."""
        source = _source(_dive(started_at="2026-04-17T07:30:00Z"))
        note = _note("dives/0/started_at", code="start_time_in_utc")
        assert not _left_out(note, source, _stored(utc_offset=0))
        assert _left_out(note, source, _stored(utc_offset=120))

    def test_each_part_of_a_start_is_its_own_member(self) -> None:
        source = _source(_dive(started_at="2026-04-17T09:30:00"))
        seconds = _note("dives/0/started_at", code="start_time_seconds_missing")
        zone = _note("dives/0/started_at", code="start_time_zone_missing")
        assert not _left_out(seconds, source, _stored(utc_offset=None, seconds=(0, 0)))
        assert _left_out(seconds, source, _stored(utc_offset=None, seconds=(17, 0)))
        assert not _left_out(zone, source, _stored(utc_offset=None))
        assert _left_out(zone, source, _stored(utc_offset=120))

    def test_a_date_without_a_time_is_the_dive_s_until_another_file_gives_one(self) -> None:
        source = _source(_dive(started_at="2026-04-17"))
        note = _note("dives/0/started_at", code="start_time_of_day_missing")
        assert not _left_out(note, source, _stored(date_only=True))
        assert _left_out(note, source, _stored(date_only=False))

    def test_a_value_read_one_way_goes_when_the_dive_kept_another_file_s(self) -> None:
        source = _source(_dive(exit_position={"latitude": 28.5, "longitude": 34.5}, current="light"))
        exit_fix = _note("dives/0/exit_position", "resolved")
        assert not _left_out(exit_fix, source, _stored(exit_position=(28.5, 34.5)))
        assert _left_out(exit_fix, source, _stored(exit_position=(28.6, 34.5)))
        current = _note("dives/0/current", "resolved")
        assert not _left_out(current, source, _stored(current="light"))
        assert _left_out(current, source, _stored(current="strong"))

    def test_a_worked_out_figure_another_file_states_goes_however_they_agree(self) -> None:
        """The dive's figure is the stated one, which happens to equal the worked-out one."""
        fit = _source(_dive(submerged_time=3440), inferred=frozenset({"dives/0/submerged_time"}))
        stated = _source(_dive(submerged_time=3440))
        also_worked_out = _source(_dive(submerged_time=3440), inferred=frozenset({"dives/0/submerged_time"}))
        note = _note("dives/0/submerged_time", "inferred")
        stored = _stored(submerged_time=3440)
        assert not _left_out(note, fit, stored)
        assert _left_out(note, fit, stored, stated)
        assert not _left_out(note, fit, stored, also_worked_out)

    def test_an_absence_is_not_left_out_because_another_file_states_nothing_either(self) -> None:
        source = _source(_dive())
        assert not _left_out(_note("dives/0/avg_depth"), source, _stored(avg_depth=None), _source(_dive()))

    def test_the_planner_s_own_reading_is_the_file_s(self) -> None:
        """An average below the maximum is cleared as the planner clears it, so the stored
        nothing is this file's own."""
        source = _source(_dive(max_depth=10.0, avg_depth=12.0))
        assert not _left_out(_note("dives/0/avg_depth", "resolved"), source, _stored(avg_depth=None))

    def test_a_cylinder_is_the_dive_s_row_it_pairs_with(self) -> None:
        source = _source(_dive(cylinders=[{"oxygen": 32}, {"start_pressure": 200}]))
        gas = _note("dives/0/cylinders/1/oxygen")
        assert _left_out(gas, source, _stored(({"oxygen": 32}, {"oxygen": 50})))
        assert not _left_out(gas, source, _stored(({"oxygen": 32}, {"oxygen": None})))
        assert not _left_out(gas, source, _stored(({"oxygen": 32},))), "a cylinder no row took keeps its line"
        assert not _left_out(_note("dives/0/cylinders/5/oxygen"), source, _stored(({"oxygen": 32},)))

    def test_a_recording_s_figure_is_read_off_the_recording_it_became(self) -> None:
        source = _source(_dive(recordings=[{}]), recordings={0: (DIVE_ID, 1)})
        cns = _note("dives/0/recordings/0/cns_start")
        assert _left_out(cns, source, _stored(readouts={1: {"cns_start": 4.0}}))
        assert not _left_out(cns, source, _stored(readouts={1: {"cns_start": None}}))
        assert not _left_out(cns, source, _stored(readouts={0: {"cns_start": 4.0}})), "no recording at its ordinal"
        elsewhere = _source(_dive(recordings=[{}]), recordings={0: (DIVE_ID + 1, 1)})
        assert not _left_out(cns, elsewhere, _stored(readouts={1: {"cns_start": 4.0}})), "on another dive"

    def test_a_dive_with_no_start_holds_none_of_its_parts(self) -> None:
        source = _source(_dive(started_at=None))
        assert _left_out(_note("dives/0/started_at", code="start_time_zone_missing"), source, _stored(utc_offset=120))

    def test_another_file_whose_cylinder_or_recording_is_elsewhere_states_nothing_there(self) -> None:
        first = _source(
            _dive(cylinders=[{"volume": 12.0}], recordings=[{"cns_end": 12.0}]), recordings={0: (DIVE_ID, 0)}
        )
        other = _source(_dive(cylinders=[], recordings=[{"cns_end": 12.0}]), recordings={0: (DIVE_ID, 1)})
        size = _note("dives/0/cylinders/0/volume", "resolved", code="cylinder_size_read_as_litres")
        cns = _note("dives/0/recordings/0/cns_end", "resolved", code="figure_given_to_first_computer")
        stored = _stored(({"volume": 12.0},), readouts={0: {"cns_end": 12.0}, 1: {"cns_end": 12.0}})
        assert not _left_out(size, first, stored, other)
        assert not _left_out(cns, first, stored, other)

    def test_a_figure_given_to_the_first_computer_goes_with_another_file_s_stated_one(self) -> None:
        first = _source(_dive(recordings=[{"cns_end": 12.0}]), recordings={0: (DIVE_ID, 0)})
        other = _source(_dive(recordings=[{"cns_end": 12.0}]), recordings={0: (DIVE_ID, 0)})
        note = _note("dives/0/recordings/0/cns_end", "resolved", code="figure_given_to_first_computer")
        stored = _stored(readouts={0: {"cns_end": 12.0}})
        assert not _left_out(note, first, stored)
        assert _left_out(note, first, stored, other)


class TestTheGroups:
    def _note(self, code: str, message: str, collection: str | None = None) -> ImportDiveNote:
        return ImportDiveNote(code=code, kind="absent", collection=collection, message=message)

    def test_one_line_per_code_collection_and_sentence_in_first_seen_order(self) -> None:
        groups, truncated = grouped(
            [
                (self._note("a", "one"), "f/1"),
                (self._note("b", "two"), None),
                (self._note("a", "one"), "f/2"),
                (self._note("a", "one", "sites"), "f/3"),
                (self._note("a", "other"), "f/4"),
                *((self._note("a", "one"), f"f/{n}") for n in range(5, 8)),
            ]
        )
        assert truncated == 0
        assert [(group.code, group.collection, group.message, group.count) for group in groups] == [
            ("a", None, "one", 5),
            ("b", None, "two", 1),
            ("a", "sites", "one", 1),
            ("a", None, "other", 1),
        ]
        assert groups[0].wheres == ["f/1", "f/2", "f/5"]
        assert groups[1].wheres == []

    def test_past_the_cap_groups_are_counted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(notes, "MAX_NOTE_GROUPS", 2)
        groups, truncated = grouped([(self._note("a", str(n)), None) for n in range(5)])
        assert ([group.message for group in groups], truncated) == (["0", "1"], 3)
