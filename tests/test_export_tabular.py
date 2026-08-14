"""Tests for the CSV writers (`services/export/tabular.py`).

The one that matters most is `test_the_dives_file_is_byte_for_byte_what_it_was`: a golden
file, checked in beside this module. `dives.csv` is the file a diver opens in a
spreadsheet, so a silently reordered column or a changed quoting rule breaks something
nobody has a test for otherwise - and reading the diff of a golden file is how you find
out you did it.

Everything else here is about the ways a CSV goes wrong in the field rather than in a
unit test: notes holding commas, quotes and newlines; a byte-order mark Excel needs and
`csv.reader` must still tolerate; and empty cells that have to stay empty rather than
becoming the string `None`.
"""

import csv
import io
from pathlib import Path

import pytest

from src.app.services.export.tabular import (
    BOM,
    DIVES_HEADER,
    _utc_offset,
    write_certifications_csv,
    write_dive_sites_csv,
    write_dives_csv,
    write_gear_items_csv,
    write_gear_service_csv,
    write_mixtures_csv,
    write_trips_csv,
)
from tests.helpers.export import build_bundle, full_bundle, make_dive, mixture

GOLDEN = Path(__file__).parent / "fixtures" / "export" / "dives.csv"


def _render(chunks) -> str:
    return "".join(chunks)


def _parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.removeprefix(BOM), newline="")))


class TestDivesCsv:
    def test_the_dives_file_is_byte_for_byte_what_it_was(self):
        """The golden file. Regenerate deliberately - see `fixtures/export/README.md`.

        Compared as **bytes**: `read_text` applies universal newlines and would quietly
        turn the file's CRLF endings into LF, so a writer that stopped emitting RFC 4180
        line endings would still pass.
        """
        assert _render(write_dives_csv(full_bundle())).encode("utf-8") == GOLDEN.read_bytes()

    def test_it_starts_with_a_byte_order_mark(self):
        """Excel reads a BOM-less UTF-8 CSV as the local ANSI codepage, which turns every
        accented site name into mojibake. Every programmatic reader tolerates the BOM."""
        assert _render(write_dives_csv(full_bundle())).startswith(BOM)

    def test_a_reader_stripping_the_bom_sees_the_declared_header(self):
        rows = _parse(_render(write_dives_csv(full_bundle())))
        assert tuple(rows[0]) == DIVES_HEADER

    def test_notes_with_commas_quotes_and_newlines_round_trip(self):
        """All three are ways to produce a file that opens with the columns shifted."""
        rows = _parse(_render(write_dives_csv(full_bundle())))
        assert rows[1][DIVES_HEADER.index("notes")] == 'Strong current, "the wall" was worth it.\nSaw a thresher.'

    def test_one_row_per_dive_oldest_first(self):
        rows = _parse(_render(write_dives_csv(full_bundle())))
        assert [row[0] for row in rows[1:]] == ["1", "2", "3"]

    def test_missing_readings_are_empty_cells_not_the_word_none(self):
        """`csv.writer` renders `None` as the empty string; a stray `str(None)` anywhere
        upstream would put the literal text `None` in a spreadsheet."""
        rows = _parse(_render(write_dives_csv(full_bundle())))
        bare = rows[3]
        assert "None" not in bare
        assert bare[DIVES_HEADER.index("max_depth_m")] == ""

    def test_sites_are_joined_in_visit_order(self):
        rows = _parse(_render(write_dives_csv(full_bundle())))
        assert rows[1][DIVES_HEADER.index("dive_sites")] == "Shark Reef; Yolanda"

    def test_cylinders_read_the_way_a_diver_says_them(self):
        rows = _parse(_render(write_dives_csv(full_bundle())))
        assert rows[1][DIVES_HEADER.index("cylinders")] == "EAN32 12L 200->70bar"
        assert rows[2][DIVES_HEADER.index("cylinders")] == "21/35 24L 232->90bar (bottom); EAN50 11.1L 200bar (deco)"

    def test_a_cylinder_with_no_pressures_says_only_what_it_knows(self):
        bundle = build_bundle(
            dives=[make_dive(1, full_bundle().dives[0].uuid)],
            mixtures_by_dive={1: [mixture(volume=12.0)]},
        )
        rows = _parse(_render(write_dives_csv(bundle)))
        assert rows[1][DIVES_HEADER.index("cylinders")] == "Air 12L"

    def test_the_local_time_and_its_offset_are_separate_columns(self):
        """So a spreadsheet can sort on local time without parsing an offset out of a
        string. 06:15 UTC at +02:00 is 08:15 local."""
        rows = _parse(_render(write_dives_csv(full_bundle())))
        assert rows[1][1:4] == ["2026-06-01", "08:15:00", "+02:00"]

    def test_an_empty_logbook_is_a_header_and_nothing_else(self):
        assert _parse(_render(write_dives_csv(build_bundle()))) == [list(DIVES_HEADER)]


class TestUtcOffset:
    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [(0, "+00:00"), (120, "+02:00"), (330, "+05:30"), (-480, "-08:00"), (-30, "-00:30")],
    )
    def test_it_spells_the_offset_the_way_the_datetime_does(self, minutes, expected):
        assert _utc_offset(minutes) == expected


class TestTheNormalizedFiles:
    def test_mixtures_carry_one_row_per_cylinder(self):
        rows = _parse(_render(write_mixtures_csv(full_bundle())))
        assert len(rows) == 4  # header + one cylinder on dive 1, two on dive 2
        assert rows[2][2:6] == ["21/35", "21.0", "35.0", "24.0"]

    def test_trips_count_the_dives_that_reference_them(self):
        rows = _parse(_render(write_trips_csv(full_bundle())))
        assert rows[1][0] == "Red Sea 2026"
        assert rows[1][4] == "2"

    def test_dive_sites_count_visits_not_dives(self):
        """Yolanda is the second site of one dive and the only site of another."""
        rows = _parse(_render(write_dive_sites_csv(full_bundle())))
        counts = {row[0]: row[2] for row in rows[1:]}
        assert counts == {"Shark Reef": "1", "Yolanda": "2"}

    def test_gear_items_carry_the_sets_they_belong_to(self):
        rows = _parse(_render(write_gear_items_csv(full_bundle())))
        assert {row[0]: row[6] for row in rows[1:]} == {"XTX50": "Tech", "Fusion": "Tech", "Slate": ""}

    def test_schedules_and_records_share_one_file_told_apart_by_row_type(self):
        rows = _parse(_render(write_gear_service_csv(full_bundle())))
        assert [row[1] for row in rows[1:]] == ["schedule", "record"]
        assert rows[1][0] == rows[2][0] == "XTX50"

    def test_certifications_name_the_agency_the_diver_gave(self):
        rows = _parse(_render(write_certifications_csv(full_bundle())))
        assert rows[1][0:2] == ["padi", "Open Water Diver"]

    def test_an_agency_of_other_shows_what_the_diver_typed(self):
        bundle = full_bundle()
        bundle.certifications[0].agency = "other"
        bundle.certifications[0].agency_other = "Ukrainian Diving Federation"
        rows = _parse(_render(write_certifications_csv(bundle)))
        assert rows[1][0] == "Ukrainian Diving Federation"

    def test_every_normalized_file_is_headed_even_when_empty(self):
        empty = build_bundle()
        for writer in (
            write_mixtures_csv,
            write_trips_csv,
            write_dive_sites_csv,
            write_gear_items_csv,
            write_gear_service_csv,
            write_certifications_csv,
        ):
            assert len(_parse(_render(writer(empty)))) == 1, writer.__name__
