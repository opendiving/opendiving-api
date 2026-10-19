"""The join of a converted file's findings to its dives, which needs no database.

A finding's path counts the source's records and `Conversion.sources` names where each
carried record came from, so these pin the package's rule on hand-made conversions: the
longest source path the finding's own starts with, segment by segment.
"""

import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Any, cast

import divejson

from src.app.services.logbook_import.reader import dive_conversion_notes

DIVE = uuid_pkg.uuid4()
PERSON = uuid_pkg.uuid4()


def _found(*wheres: str) -> dict[uuid_pkg.UUID, list[tuple[str, str | None]]]:
    notes = tuple(divejson.Note(where, where, "absent") for where in wheres)
    sources = {str(DIVE): "log.ssrf/dive/1", str(PERSON): "log.ssrf/dive/1/buddy/0"}
    loaded = SimpleNamespace(
        conversion=divejson.Conversion({}, notes, sources), document=SimpleNamespace(dives=[SimpleNamespace(uuid=DIVE)])
    )
    return {
        uuid: [(note.message, note.where) for note in found]
        for uuid, found in dive_conversion_notes(cast(Any, loaded)).items()
    }


class TestTheJoin:
    def test_a_finding_at_the_dive_or_below_it_is_the_dive_s_placed_within_it(self) -> None:
        assert _found("log.ssrf/dive/1", "log.ssrf/dive/1/sample/30.5") == {
            DIVE: [("log.ssrf/dive/1", None), ("log.ssrf/dive/1/sample/30.5", "sample/30.5")]
        }

    def test_a_finding_about_a_record_below_the_dive_is_that_record_s(self) -> None:
        assert _found("log.ssrf/dive/1/buddy/0", "log.ssrf/dive/1/buddy/0/name") == {}

    def test_a_path_matches_by_whole_segments(self) -> None:
        assert _found("log.ssrf/dive/10", "log.ssrf/dive/1x") == {}

    def test_a_finding_about_the_file_or_a_dropped_dive_is_no_dive_s(self) -> None:
        assert _found("log.ssrf/$", "log.ssrf/dive/0", "log.ssrf/diver") == {}

    def test_a_document_nothing_converted_has_no_findings(self) -> None:
        loaded = SimpleNamespace(conversion=None, document=SimpleNamespace(dives=[SimpleNamespace(uuid=DIVE)]))
        assert dive_conversion_notes(cast(Any, loaded)) == {}
