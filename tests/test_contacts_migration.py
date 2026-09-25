"""Revision `a9b7dc451f00` run for real, against a database of its own
(`tests/helpers/migrations.py`).

The suite's database is at head, where the two `training_center` columns are already gone.
What is worth pinning is the backfill's invariant - one contact per distinct trimmed,
lowercased string per diver, from courses and live cards, every row linked by its string and
a hidden card only where a live row made the contact - the hidden-set rewrite, and what
`downgrade()` puts back.

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

_REVISION = "a9b7dc451f00"
_BELOW = "ab9a5add4fee"


@pytest.fixture
def scratch() -> Iterator[tuple[str, Engine]]:
    with scratch_database("contacts") as database:
        yield database


# Two divers. The first carries every shape the backfill has to tell apart: one string in
# two spellings (the course's wins, being first), a blank and a null, a string a course
# and a live card share - the card spelling it lower-case, which the course's first
# spelling overrides - a string only live cards carry in two spellings, a hidden card
# sharing a live row's string, and a string only a hidden card carries. The second has the
# first's commonest string, which is a contact of their own.
COURSES = [
    (1, 1, "Blue Ocean"),
    (2, 1, "  blue ocean "),
    (3, 1, ""),
    (4, 1, None),
    (5, 1, "Koh Tao Divers"),
    (6, 2, "Blue Ocean"),
]
# (id, user, training_center, is_deleted)
CERTIFICATIONS = [
    (1, 1, "koh tao divers", False),
    (2, 1, "Reef School", False),
    (3, 1, "BLUE OCEAN", True),
    (4, 1, "Ghost Center", True),
    (5, 1, "   ", False),
    (6, 1, "REEF SCHOOL", False),
]
# (id, user, name, hidden_fields)
PRESETS = [
    (1, 1, "Basic", ["course_uuid", "avg_depth", "mixture.usage"]),
    (2, 1, "Both", ["trip_uuid", "course_uuid"]),
    (3, 1, "Recreational", ["altitude", "mixture.po2_limit"]),
    (4, 2, "Basic", ["trip_uuid", "course_uuid", "avg_depth"]),
]
USER_HIDDEN = {1: ["course_uuid", "notes"], 2: []}


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
        for course_id, user_id, training_center in COURSES:
            connection.execute(
                text(
                    "INSERT INTO course (id, user_id, name, status, training_center, notes, uuid, created_at) "
                    "VALUES (:id, :user_id, 'Course', 'completed', :tc, '', gen_random_uuid(), now())"
                ),
                {"id": course_id, "user_id": user_id, "tc": training_center},
            )
        for certification_id, user_id, training_center, is_deleted in CERTIFICATIONS:
            connection.execute(
                text(
                    "INSERT INTO certification (id, user_id, agency, name, training_center, notes, uuid, created_at, "
                    "is_deleted) VALUES (:id, :user_id, 'padi', 'Card', :tc, '', gen_random_uuid(), now(), :deleted)"
                ),
                {"id": certification_id, "user_id": user_id, "tc": training_center, "deleted": is_deleted},
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


def _contact_names_by_row(engine: Engine, table: str) -> dict[int, str | None]:
    return {
        row.id: row.name
        for row in _rows(
            engine,
            f"SELECT host.id, contact.name FROM {table} AS host LEFT JOIN contact ON contact.id = host.contact_id",
        )
    }


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
    def test_one_school_per_distinct_string_per_diver_from_live_rows(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _BELOW)
        _seed(engine)

        migrate(name, "upgrade", _REVISION)

        contacts = _rows(engine, "SELECT user_id, name, roles, notes, uuid FROM contact ORDER BY user_id, name")
        assert [(row.user_id, row.name) for row in contacts] == [
            (1, "Blue Ocean"),
            (1, "Koh Tao Divers"),
            (1, "Reef School"),
            (2, "Blue Ocean"),
        ]
        assert {json.dumps(row.roles) for row in contacts} == {'["school"]'}
        assert {row.notes for row in contacts} == {""}
        # Every public identifier in this schema is time-ordered.
        assert {row.uuid.version for row in contacts} == {7}

    def test_every_row_is_linked_by_its_string_and_a_hidden_card_only_where_a_live_row_made_one(
        self, scratch: tuple[str, Engine]
    ) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _BELOW)
        _seed(engine)

        migrate(name, "upgrade", _REVISION)

        assert _contact_names_by_row(engine, "course") == {
            1: "Blue Ocean",
            2: "Blue Ocean",
            3: None,
            4: None,
            5: "Koh Tao Divers",
            6: "Blue Ocean",
        }
        assert _contact_names_by_row(engine, "certification") == {
            1: "Koh Tao Divers",
            2: "Reef School",
            3: "Blue Ocean",
            4: None,
            5: None,
            6: "Reef School",
        }
        # Each diver's own: the second diver's course names their contact, not the first's.
        (row,) = _rows(
            engine,
            "SELECT contact.user_id FROM course JOIN contact ON contact.id = course.contact_id WHERE course.id = 6",
        )
        assert row.user_id == 2
        columns = {
            row.column_name
            for row in _rows(
                engine,
                "SELECT column_name FROM information_schema.columns WHERE table_name IN ('course', 'certification')",
            )
        }
        assert "training_center" not in columns

    def test_a_set_that_hides_the_course_hides_the_contact_beside_it(self, scratch: tuple[str, Engine]) -> None:
        """Right after `course_uuid`, which is `DiveFormField`'s canonical position, and
        nowhere the course was not hidden."""
        name, engine = scratch
        migrate(name, "upgrade", _BELOW)
        _seed(engine)

        migrate(name, "upgrade", _REVISION)

        assert _hidden_sets(engine) == {
            "preset 1": ["course_uuid", "contact_uuid", "avg_depth", "mixture.usage"],
            "preset 2": ["trip_uuid", "course_uuid", "contact_uuid"],
            "preset 3": ["altitude", "mixture.po2_limit"],
            "preset 4": ["trip_uuid", "course_uuid", "contact_uuid", "avg_depth"],
            "user 1": ["course_uuid", "contact_uuid", "notes"],
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

        courses = {row.id: row.training_center for row in _rows(engine, "SELECT id, training_center FROM course")}
        certifications = {
            row.id: row.training_center for row in _rows(engine, "SELECT id, training_center FROM certification")
        }
        assert courses == {1: "Blue Ocean", 2: "Blue Ocean", 3: None, 4: None, 5: "Koh Tao Divers", 6: "Blue Ocean"}
        assert certifications == {
            1: "Koh Tao Divers",
            2: "Reef School",
            3: "Blue Ocean",
            4: None,
            5: None,
            6: "Reef School",
        }
        assert _hidden_sets(engine) == hidden_before
        assert _rows(engine, "SELECT to_regclass('contact') AS present")[0].present is None

    def test_upgrading_again_reproduces_the_contacts(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _BELOW)
        _seed(engine)

        migrate(name, "upgrade", _REVISION)
        first = _rows(engine, "SELECT user_id, name FROM contact ORDER BY user_id, name")
        migrate(name, "downgrade", _BELOW)
        migrate(name, "upgrade", _REVISION)

        assert _rows(engine, "SELECT user_id, name FROM contact ORDER BY user_id, name") == first
        assert _hidden_sets(engine)["preset 1"] == ["course_uuid", "contact_uuid", "avg_depth", "mixture.usage"]
