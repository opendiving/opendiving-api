"""Revision `b5dad8793a54` run for real, against a database of its own
(`tests/helpers/migrations.py`).

The suite's database is at head, where the two `instructor_name` columns are already gone.
What is worth pinning is the backfill's invariant - one person per distinct trimmed,
lowercased string per diver, from courses and live cards, every course listing its person as
instructor and every card naming it, a hidden card only where a live row made the person -
the hidden-set rewrite, and what `downgrade()` puts back. This runs against real accounts on
the instance within minutes of merging, with no way back but a later revision.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text

from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

_REVISION = "b5dad8793a54"
_BELOW = "a8fb7217f84a"


@pytest.fixture
def scratch() -> Iterator[tuple[str, Engine]]:
    with scratch_database("people") as database:
        yield database


# Two divers. The first carries every shape the backfill has to tell apart: one string in
# two spellings (the course's wins, being first), a blank and a null, a string a course and
# a live card share - the card spelling it lower-case - a string only live cards carry in two
# spellings, a hidden card sharing a live row's string, and a string only a hidden card
# carries. The second has the first's commonest string, which is a person of their own.
COURSES = [
    (1, 1, "Jae Kim"),
    (2, 1, "  jae kim "),
    (3, 1, ""),
    (4, 1, None),
    (5, 1, "Sam Ortiz"),
    (6, 2, "Jae Kim"),
]
# (id, user, instructor_name, is_deleted)
CERTIFICATIONS = [
    (1, 1, "sam ortiz", False),
    (2, 1, "Lina", False),
    (3, 1, "JAE KIM", True),
    (4, 1, "Ghost", True),
    (5, 1, "   ", False),
    (6, 1, "LINA", False),
]
# (id, user, name, hidden_fields)
PRESETS = [
    (1, 1, "Basic", ["trip_uuid", "course_uuid", "contact_uuid", "avg_depth"]),
    (2, 1, "Contact last", ["course_uuid", "contact_uuid"]),
    (3, 1, "Recreational", ["water_type", "altitude"]),
    (4, 2, "Basic", ["contact_uuid", "mixture.usage"]),
]
USER_HIDDEN = {1: ["contact_uuid", "notes"], 2: []}


def _seed(engine: Engine) -> None:
    with engine.begin() as connection:
        for user_id in (1, 2):
            connection.execute(
                text(
                    'INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at, '
                    "dive_form_hidden_fields) VALUES (:id, 'Seed', :username, :email, false, false, "
                    "gen_random_uuid(), now(), CAST(:hidden AS json))"
                ),
                {
                    "id": user_id,
                    "username": f"seed{user_id}",
                    "email": f"seed{user_id}@example.com",
                    "hidden": json.dumps(USER_HIDDEN[user_id]),
                },
            )
        for course_id, user_id, instructor_name in COURSES:
            connection.execute(
                text(
                    "INSERT INTO course (id, user_id, name, status, instructor_name, instructor_number, notes, uuid, "
                    "created_at) VALUES (:id, :user_id, 'Course', 'completed', :name, 'TDI-1', '', gen_random_uuid(), "
                    "now())"
                ),
                {"id": course_id, "user_id": user_id, "name": instructor_name},
            )
        for certification_id, user_id, instructor_name, is_deleted in CERTIFICATIONS:
            connection.execute(
                text(
                    "INSERT INTO certification (id, user_id, agency, name, instructor_name, notes, uuid, created_at, "
                    "is_deleted) VALUES (:id, :user_id, 'padi', 'Card', :name, '', gen_random_uuid(), now(), :deleted)"
                ),
                {"id": certification_id, "user_id": user_id, "name": instructor_name, "deleted": is_deleted},
            )
        for preset_id, user_id, name, hidden in PRESETS:
            connection.execute(
                text(
                    "INSERT INTO dive_form_preset (id, user_id, name, hidden_fields, uuid, created_at) "
                    "VALUES (:id, :user_id, :name, CAST(:hidden AS json), gen_random_uuid(), now())"
                ),
                {"id": preset_id, "user_id": user_id, "name": name, "hidden": json.dumps(hidden)},
            )


def _rows(engine: Engine, sql: str) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql)))


def _hidden_sets(engine: Engine) -> dict[str, list[str]]:
    presets = {
        f"preset {row.id}": row.hidden_fields for row in _rows(engine, "SELECT id, hidden_fields FROM dive_form_preset")
    }
    users = {
        f"user {row.id}": row.dive_form_hidden_fields
        for row in _rows(engine, 'SELECT id, dive_form_hidden_fields FROM "user"')
    }
    return presets | users


class TestTheUpgrade:
    def test_one_person_per_distinct_string_per_diver_from_live_rows(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _BELOW)
        _seed(engine)

        migrate(name, "upgrade", _REVISION)

        people = _rows(engine, "SELECT user_id, name, notes, linked_user_id, uuid FROM person ORDER BY user_id, name")
        assert [(row.user_id, row.name) for row in people] == [
            (1, "Jae Kim"),
            (1, "Lina"),
            (1, "Sam Ortiz"),
            (2, "Jae Kim"),
        ]
        assert {(row.notes, row.linked_user_id) for row in people} == {("", None)}
        # Every public identifier in this schema is time-ordered.
        assert {row.uuid.version for row in people} == {7}

    def test_every_course_lists_its_instructor_and_every_card_names_it(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _BELOW)
        _seed(engine)

        migrate(name, "upgrade", _REVISION)

        listed = _rows(
            engine,
            "SELECT course.id, person.name, person.user_id, course_person.role, course_person.position "
            "FROM course_person JOIN course ON course.id = course_person.course_id "
            "JOIN person ON person.id = course_person.person_id ORDER BY course.id",
        )
        assert [(row.id, row.name, row.user_id, row.role, row.position) for row in listed] == [
            (1, "Jae Kim", 1, "instructor", 0),
            (2, "Jae Kim", 1, "instructor", 0),
            (5, "Sam Ortiz", 1, "instructor", 0),
            # Each diver's own: the second diver's course names their person, not the first's.
            (6, "Jae Kim", 2, "instructor", 0),
        ]
        cards = {
            row.id: row.name
            for row in _rows(
                engine,
                "SELECT certification.id, person.name FROM certification "
                "LEFT JOIN person ON person.id = certification.instructor_id",
            )
        }
        assert cards == {1: "Sam Ortiz", 2: "Lina", 3: "Jae Kim", 4: None, 5: None, 6: "Lina"}
        # The number printed on the card stays; the name goes.
        columns = {
            (row.table_name, row.column_name)
            for row in _rows(
                engine,
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name IN ('course', 'certification')",
            )
        }
        assert ("course", "instructor_number") in columns
        assert not {column for _, column in columns} & {"instructor_name"}
        # No dive names anyone: a backfilled instructor counts zero dives.
        assert _rows(engine, "SELECT count(*) AS n FROM dive_person")[0].n == 0

    def test_a_set_that_hides_the_contact_hides_the_people_beside_it(self, scratch: tuple[str, Engine]) -> None:
        """Right after `contact_uuid`, which is `DiveFormField`'s canonical position, and
        nowhere the contact was not hidden."""
        name, engine = scratch
        migrate(name, "upgrade", _BELOW)
        _seed(engine)

        migrate(name, "upgrade", _REVISION)

        assert _hidden_sets(engine) == {
            "preset 1": ["trip_uuid", "course_uuid", "contact_uuid", "people", "avg_depth"],
            "preset 2": ["course_uuid", "contact_uuid", "people"],
            "preset 3": ["water_type", "altitude"],
            "preset 4": ["contact_uuid", "people", "mixture.usage"],
            "user 1": ["contact_uuid", "people", "notes"],
            "user 2": [],
        }


class TestTheDowngrade:
    def test_it_puts_each_live_string_back_up_to_trimming(self, scratch: tuple[str, Engine]) -> None:
        """And to the first spelling where a diver's differed only by case; a string only a
        hidden card carried is the documented loss."""
        name, engine = scratch
        migrate(name, "upgrade", _BELOW)
        _seed(engine)
        hidden_before = _hidden_sets(engine)

        migrate(name, "upgrade", _REVISION)
        migrate(name, "downgrade", _BELOW)

        courses = {row.id: row.instructor_name for row in _rows(engine, "SELECT id, instructor_name FROM course")}
        certifications = {
            row.id: row.instructor_name for row in _rows(engine, "SELECT id, instructor_name FROM certification")
        }
        assert courses == {1: "Jae Kim", 2: "Jae Kim", 3: None, 4: None, 5: "Sam Ortiz", 6: "Jae Kim"}
        assert certifications == {1: "Sam Ortiz", 2: "Lina", 3: "Jae Kim", 4: None, 5: None, 6: "Lina"}
        assert _hidden_sets(engine) == hidden_before
        for table in ("person", "dive_person", "trip_person", "course_person"):
            assert _rows(engine, f"SELECT to_regclass('{table}') AS present")[0].present is None, table

    def test_upgrading_again_reproduces_the_people(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _BELOW)
        _seed(engine)

        migrate(name, "upgrade", _REVISION)
        first = _rows(engine, "SELECT user_id, name FROM person ORDER BY user_id, name")
        migrate(name, "downgrade", _BELOW)
        migrate(name, "upgrade", _REVISION)

        assert _rows(engine, "SELECT user_id, name FROM person ORDER BY user_id, name") == first
        assert _hidden_sets(engine)["preset 2"] == ["course_uuid", "contact_uuid", "people"]
