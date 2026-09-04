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
    CERTIFICATIONS_HEADER,
    COURSES_HEADER,
    CSV_WRITERS,
    DIVE_SITES_HEADER,
    DIVES_HEADER,
    GEAR_ITEMS_HEADER,
    GEAR_SERVICE_HEADER,
    MIXTURES_HEADER,
    SPECIES_HEADER,
    TRIPS_HEADER,
    _utc_offset,
    write_certifications_csv,
    write_courses_csv,
    write_dive_sites_csv,
    write_dives_csv,
    write_gear_items_csv,
    write_gear_service_csv,
    write_mixtures_csv,
    write_species_csv,
    write_trips_csv,
)
from tests.helpers.export import UUIDS, build_bundle, full_bundle, make_dive, mixture

GOLDEN = Path(__file__).parent / "fixtures" / "export" / "dives.csv"

# Every file in `CSV_WRITERS` except `dives.csv`, which is the flat one and has the golden
# file instead. Hand-written and therefore able to go short - a writer left out of it is
# simply not covered by the two tests it feeds. `write_species_csv` was missing until
# courses were added; both are here now.
#
# Each writer is paired with the header it claims, because the pairing is itself the thing
# under test: `_rows_to_csv` takes the header and the rows as two independent tuples and
# never compares them.
_NORMALIZED_FILES = (
    (write_mixtures_csv, MIXTURES_HEADER),
    (write_trips_csv, TRIPS_HEADER),
    (write_courses_csv, COURSES_HEADER),
    (write_dive_sites_csv, DIVE_SITES_HEADER),
    (write_species_csv, SPECIES_HEADER),
    (write_gear_items_csv, GEAR_ITEMS_HEADER),
    (write_gear_service_csv, GEAR_SERVICE_HEADER),
    (write_certifications_csv, CERTIFICATIONS_HEADER),
)


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

    def test_the_water_type_and_altitude_are_their_own_columns(self):
        """`altitude_m` carries the unit suffix `water_type` has no need of, which is the
        convention the rest of the header already follows (`max_depth_m`, `weight_kg`).
        The 0 is the air dive's real sea-level altitude, and an empty cell on the next row
        is the difference between that and "not recorded" - the distinction the whole
        column is nullable for."""
        rows = _parse(_render(write_dives_csv(full_bundle())))
        assert rows[1][DIVES_HEADER.index("water_type")] == "salt"
        assert rows[1][DIVES_HEADER.index("altitude_m")] == "0"
        assert rows[2][DIVES_HEADER.index("water_type")] == ""
        assert rows[2][DIVES_HEADER.index("altitude_m")] == ""

    def test_only_the_dive_on_a_course_names_one(self):
        """The `course` cell mirrors `trip` beside it: the dive logged on the course names
        it, and the two that were not are empty rather than inheriting anything."""
        rows = _parse(_render(write_dives_csv(full_bundle())))
        column = DIVES_HEADER.index("course")

        assert [row[column] for row in rows[1:]] == ["Advanced Nitrox + Decompression Procedures", "", ""]

    def test_sites_are_joined_in_visit_order(self):
        rows = _parse(_render(write_dives_csv(full_bundle())))
        assert rows[1][DIVES_HEADER.index("dive_sites")] == "Shark Reef; Yolanda"

    def test_cylinders_read_the_way_a_diver_says_them(self):
        """Role and usage are separate parenthesised tokens rather than one - they are
        orthogonal facts, and a bottle can be `(deco)` without being `(staged)`.
        """
        rows = _parse(_render(write_dives_csv(full_bundle())))
        assert rows[1][DIVES_HEADER.index("cylinders")] == "EAN32 12L 200->70bar"
        assert (
            rows[2][DIVES_HEADER.index("cylinders")]
            == "21/35 24L 232->90bar (bottom); EAN50 11.1L 200bar (deco) (staged)"
        )

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


class TestSpeciesCsv:
    def test_the_dives_row_folds_the_scientific_names_in_order(self):
        """Scientific names, not the common ones the dive page shows: they are unambiguous
        and every row has one, which a common name does not."""
        rows = _parse(_render(write_dives_csv(full_bundle())))

        assert rows[1][rows[0].index("species")] == "Amphiprion ocellaris; Muraenidae"
        # The dive that saw nothing gets an empty cell, not the word "None".
        assert rows[3][rows[0].index("species")] == ""

    def test_the_count_is_dives_not_sightings(self):
        """The same distinction `species_seen` makes. The clownfish is on two of the three
        dives and the moray on one, and neither is on the bare dive."""
        rows = _parse(_render(write_species_csv(full_bundle())))

        assert rows[0] == list(SPECIES_HEADER)
        assert [(row[0], row[4]) for row in rows[1:]] == [("Amphiprion ocellaris", "2"), ("Muraenidae", "1")]

    def test_a_species_with_no_common_name_leaves_the_cell_empty(self):
        rows = _parse(_render(write_species_csv(full_bundle())))
        by_name = {row[0]: row for row in rows[1:]}

        assert by_name["Amphiprion ocellaris"][1] == "ocellaris clownfish"
        assert by_name["Muraenidae"][1] == ""
        # The AphiaID is the column that means anything outside this database.
        assert by_name["Muraenidae"][3] == "125230"


class TestTheNormalizedFiles:
    def test_mixtures_carry_one_row_per_cylinder(self):
        rows = _parse(_render(write_mixtures_csv(full_bundle())))
        assert len(rows) == 4  # header + one cylinder on dive 1, two on dive 2
        assert rows[2][2:6] == ["21/35", "21.0", "35.0", "24.0"]

    def test_mixtures_carry_the_role_and_usage_as_their_values(self):
        """Both are enums on the read schema, so a bare `str()` would write
        `GasRole.DECO` into a spreadsheet cell. An unanswered cylinder is empty, not
        `None`.
        """
        rows = _parse(_render(write_mixtures_csv(full_bundle())))
        role, usage = MIXTURES_HEADER.index("role"), MIXTURES_HEADER.index("usage")

        assert [(row[role], row[usage]) for row in rows[1:]] == [("", ""), ("bottom", ""), ("deco", "staged")]

    def test_trips_count_the_dives_that_reference_them(self):
        rows = _parse(_render(write_trips_csv(full_bundle())))
        assert rows[1][0] == "Red Sea 2026"
        assert rows[1][TRIPS_HEADER.index("dives")] == "2"

    def test_a_trips_places_are_joined_into_one_location_cell(self):
        """A spreadsheet column is no place for a nested shape, so the list is rendered the
        way the app shows it and the structured version stays in `logbook.divejson`."""
        rows = _parse(_render(write_trips_csv(full_bundle())))
        assert rows[1][TRIPS_HEADER.index("location")] == "Sharm el-Sheikh, Ras Mohammed"

    def test_dive_sites_count_visits_not_dives(self):
        """Yolanda is the second site of one dive and the only site of another."""
        rows = _parse(_render(write_dive_sites_csv(full_bundle())))
        counts = {row[0]: row[DIVE_SITES_HEADER.index("dives")] for row in rows[1:]}
        assert counts == {"Shark Reef": "1", "Yolanda": "2"}

    def test_dive_site_coordinates_are_written_as_stored(self):
        """Decimal degrees in their own two columns, so a spreadsheet can sort on them and
        a mapping tool can read them - and an empty cell where a site has no position,
        rather than a `0` a reader would take for Null Island."""
        rows = _parse(_render(write_dive_sites_csv(full_bundle())))
        latitude, longitude = DIVE_SITES_HEADER.index("latitude"), DIVE_SITES_HEADER.index("longitude")
        assert {row[0]: (row[latitude], row[longitude]) for row in rows[1:]} == {
            "Shark Reef": ("27.7278", "34.2564"),
            "Yolanda": ("", ""),
        }

    def test_gear_items_carry_the_sets_they_belong_to(self):
        rows = _parse(_render(write_gear_items_csv(full_bundle())))
        sets = GEAR_ITEMS_HEADER.index("sets")
        assert {row[0]: row[sets] for row in rows[1:]} == {
            "XTX50": "Tech",
            "XTX200": "",
            "Fusion": "Tech",
            "Slate": "",
        }

    def test_schedules_and_records_share_one_file_told_apart_by_row_type(self):
        rows = _parse(_render(write_gear_service_csv(full_bundle())))
        row_type = GEAR_SERVICE_HEADER.index("row_type")
        assert [row[row_type] for row in rows[1:]] == ["schedule", "record"]
        assert rows[1][0] == rows[2][0] == "XTX50"

    def test_gear_service_rows_join_by_uuid_not_by_display_name(self):
        """A gear item's name is not unique across a diver's history."""
        rows = _parse(_render(write_gear_service_csv(full_bundle())))
        item_uuid = GEAR_SERVICE_HEADER.index("gear_item_uuid")
        assert {row[item_uuid] for row in rows[1:]} == {str(UUIDS["gear-regulator"])}
        assert all(row[GEAR_SERVICE_HEADER.index("row_uuid")] for row in rows[1:])

    def test_no_file_carries_a_deleted_column_any_more(self):
        """These four had one because `loader._owned` could resurrect their rows, and a
        CSV listing as live what `logbook.divejson` flagged as deleted would have contradicted
        itself inside one archive. Nothing is resurrected now, so a column that could only
        ever read `False` is a column that misleads. A breaking format change, deliberately
        - see the PR body."""
        for writer, header in (
            (write_trips_csv, TRIPS_HEADER),
            (write_dive_sites_csv, DIVE_SITES_HEADER),
            (write_gear_items_csv, GEAR_ITEMS_HEADER),
            (write_gear_service_csv, GEAR_SERVICE_HEADER),
        ):
            assert "deleted" not in header, writer.__name__
            # And the rows still line up with the header they claim - dropping a column
            # from one and not the other shifts every cell after it.
            rows = _parse(_render(writer(full_bundle())))
            assert all(len(row) == len(header) for row in rows), writer.__name__

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
        for writer, _ in _NORMALIZED_FILES:
            assert len(_parse(_render(writer(empty)))) == 1, writer.__name__

    def test_every_normalized_file_lines_its_rows_up_with_its_header(self):
        """Each writer declares its header as one tuple and yields its rows as another,
        matched by position and by nothing else. Drop a column from one and not the other
        and every cell after it shifts silently into the neighbouring column - the file
        still parses, still has a header, and is wrong from that column to the end.

        `test_no_file_carries_a_deleted_column_any_more` pins this for the four files that
        change touched; `courses.csv` was covered by neither, which is what let a column
        removal there go unnoticed. This asserts it for every normalized file, so the next
        one is covered without anyone remembering to add it.

        The row count is asserted first because the alignment check is vacuous on a file
        with no data rows: a fixture that stopped producing a course would turn this test
        green rather than red.
        """
        for writer, header in _NORMALIZED_FILES:
            rows = _parse(_render(writer(full_bundle())))
            assert len(rows) > 1, writer.__name__
            assert tuple(rows[0]) == header, writer.__name__
            assert all(len(row) == len(header) for row in rows[1:]), writer.__name__

    def test_every_file_carries_the_byte_order_mark_not_just_dives(self):
        """`dive-sites.csv`, `trips.csv`, `courses.csv` and `certifications.csv` hold the
        same free text as `dives.csv`, and a diver who unzips the archive and double-clicks
        one hits the same Excel mojibake. Pinned across all nine so a file added later
        cannot quietly be the exception."""
        bundle = full_bundle()
        assert len(CSV_WRITERS) == 9
        for filename, writer in CSV_WRITERS:
            assert _render(writer(bundle)).startswith(BOM), filename
