"""The cap on an import report's notes, at which a derived value's note gives way.

No database: `add_note` and the batch's merge of its files' notes are pure.
"""

from types import SimpleNamespace
from typing import Any, cast

from src.app.schemas.logbook_import import ImportNote, ImportNoteCode
from src.app.services.logbook_import.batch import _notes
from src.app.services.logbook_import.planner import MAX_NOTES, add_note


def _note(code: ImportNoteCode, message: str = "") -> ImportNote:
    return ImportNote(code=code, message=message)


def _full(code: ImportNoteCode = ImportNoteCode.VALUE_DERIVED) -> list[ImportNote]:
    return [_note(code, str(index)) for index in range(MAX_NOTES)]


class TestAddNote:
    def test_under_the_cap_it_appends_and_drops_nothing(self) -> None:
        notes: list[ImportNote] = []

        assert add_note(notes, _note(ImportNoteCode.VALUE_DERIVED)) == 0
        assert len(notes) == 1

    def test_at_the_cap_a_derived_note_is_dropped(self) -> None:
        notes = _full()

        assert add_note(notes, _note(ImportNoteCode.VALUE_DERIVED, "late")) == 1
        assert [note.message for note in notes] == [str(index) for index in range(MAX_NOTES)]

    def test_at_the_cap_any_other_note_takes_the_latest_derived_notes_place(self) -> None:
        notes = [*_full(ImportNoteCode.RECORD_SKIPPED)[:-2], _note(ImportNoteCode.VALUE_DERIVED, "derived")]
        notes.append(_note(ImportNoteCode.RECORD_SKIPPED, "kept"))

        assert add_note(notes, _note(ImportNoteCode.RECORD_SKIPPED, "late")) == 1
        assert len(notes) == MAX_NOTES
        assert [note.message for note in notes[-2:]] == ["kept", "late"]
        assert ImportNoteCode.VALUE_DERIVED not in {note.code for note in notes}

    def test_at_the_cap_with_nothing_derived_the_new_note_is_dropped(self) -> None:
        notes = _full(ImportNoteCode.RECORD_SKIPPED)

        assert add_note(notes, _note(ImportNoteCode.VALUE_DROPPED, "late")) == 1
        assert "late" not in {note.message for note in notes}


class TestTheBatchsNotes:
    def test_a_later_files_skipped_record_survives_every_earlier_files_derived_value(self) -> None:
        """One derived bottom temperature per dive-computer file, past the cap, and then a file
        whose dive was skipped."""
        derived = [_note(ImportNoteCode.VALUE_DERIVED) for _ in range(MAX_NOTES + 100)]
        plans = [SimpleNamespace(notes=[note], notes_dropped=0) for note in derived]
        plans.append(SimpleNamespace(notes=[_note(ImportNoteCode.RECORD_SKIPPED, "skipped")], notes_dropped=2))

        notes, truncated = _notes(cast(Any, plans))

        assert len(notes) == MAX_NOTES
        assert notes[-1].message == "skipped"
        assert truncated == 100 + 1 + 2
