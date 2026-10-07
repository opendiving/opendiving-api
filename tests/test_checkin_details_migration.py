"""Revision `580d29f6c848` run for real, against a database of its own
(`tests/helpers/migrations.py`).

The copy is the data path for every account the hosted instance already holds: the check-in
columns move onto the new tables before they are dropped, blanks are not copied, a contact
without a name and a policy without a provider stay behind, and the policy keeps the reminder
pair it was last emailed about. The downgrade puts the first row of each kind back.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from sqlalchemy import Engine, text

from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "580d29f6c848"
_BELOW = "bb0a5f425d41"

FILLED: dict[str, Any] = {
    "date_of_birth": date(1988, 4, 12),
    "phone": " +20 100 123 4567 ",
    "emergency_contact_name": "Grace Hopper",
    "emergency_contact_phone": "+1 202 555 0143",
    "emergency_contact_relationship": "",
    "insurance_provider": "DAN Europe",
    "insurance_policy_number": "DE-4471902",
    "insurance_expires_on": date(2027, 6, 30),
    "insurance_notified_stage": "expiring_soon",
    "insurance_notified_for": date(2027, 6, 30),
}

# (id, columns): every detail filled; anchorless objects beside a phone; nothing at all.
USERS: list[tuple[int, dict[str, Any]]] = [
    (1, FILLED),
    (
        2,
        {
            "phone": "+44 20 0000 0000",
            "emergency_contact_name": "  ",
            "emergency_contact_phone": "+1 202 555 0100",
            "insurance_provider": "",
            "insurance_policy_number": "X-1",
        },
    ),
    (3, {}),
]


def _seed(engine: Engine) -> None:
    with engine.begin() as connection:
        for user_id, columns in USERS:
            names = ", ".join(columns)
            values = ", ".join(f":{name}" for name in columns)
            connection.execute(
                text(
                    'INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at'
                    + (f", {names}" if columns else "")
                    + ") VALUES (:id, 'Seed', :username, :email, false, false, gen_random_uuid(), now()"
                    + (f", {values}" if columns else "")
                    + ")"
                ),
                {"id": user_id, "username": f"seed{user_id}", "email": f"seed{user_id}@example.com", **columns},
            )


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def upgraded(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("checkin", below) as (name, engine):
        _seed(engine)
        migrate(name, "upgrade", _REVISION)
        yield name, engine


def _rows(engine: Engine, statement: str) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(statement))]


class TestTheCopy:
    def test_each_account_with_a_detail_gets_one_parent_row_trimmed(self, upgraded: tuple[str, Engine]) -> None:
        _, engine = upgraded

        assert _rows(engine, "SELECT user_id, email, phone, date_of_birth FROM checkin_details ORDER BY user_id") == [
            (1, None, "+20 100 123 4567", date(1988, 4, 12)),
            (2, None, "+44 20 0000 0000", None),
        ]

    def test_a_named_contact_is_copied_and_a_blank_member_is_not(self, upgraded: tuple[str, Engine]) -> None:
        _, engine = upgraded

        assert _rows(engine, "SELECT user_id, position, name, phone, relationship FROM checkin_emergency_contact") == [
            (1, 0, "Grace Hopper", "+1 202 555 0143", None)
        ]

    def test_a_policy_with_a_provider_is_copied_with_its_reminder_pair(self, upgraded: tuple[str, Engine]) -> None:
        _, engine = upgraded

        assert _rows(
            engine,
            "SELECT user_id, position, provider, number, expires_on, notified_stage, notified_for "
            "FROM checkin_insurance_policy",
        ) == [(1, 0, "DAN Europe", "DE-4471902", date(2027, 6, 30), "expiring_soon", date(2027, 6, 30))]

    def test_the_columns_are_gone_from_user(self, upgraded: tuple[str, Engine]) -> None:
        _, engine = upgraded

        left = _rows(
            engine,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'user' AND column_name IN "
            f"({', '.join(repr(name) for name in FILLED)})",
        )

        assert left == []


class TestTheDowngrade:
    def test_the_first_row_of_each_kind_goes_back_on_user(self, upgraded: tuple[str, Engine]) -> None:
        name, engine = upgraded
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO checkin_insurance_policy (user_id, position, provider) VALUES (1, 1, 'DiveAssure')")
            )
        migrate(name, "downgrade", _BELOW)

        restored = _rows(
            engine,
            f'SELECT id, {", ".join(FILLED)} FROM "user" ORDER BY id',
        )
        tables = _rows(engine, "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'checkin_%'")

        assert restored[0] == (
            1,
            date(1988, 4, 12),
            "+20 100 123 4567",
            "Grace Hopper",
            "+1 202 555 0143",
            None,
            "DAN Europe",
            "DE-4471902",
            date(2027, 6, 30),
            "expiring_soon",
            date(2027, 6, 30),
        )
        assert restored[1][1:] == (None, "+44 20 0000 0000", *([None] * 8))
        assert restored[2][1:] == (None,) * len(FILLED)
        assert sorted(table for (table,) in tables) == ["checkin_link"]
