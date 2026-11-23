"""Revision `624702237252` run for real, against a database of its own
(`tests/helpers/migrations.py`).

What it must not lose runs on the hosted instance within minutes of merging: every member's
standing token, which a printed QR code may carry, and every tie between a person and an
account. Both carry over unchanged, the stored audit rows take the renamed values, and
`downgrade()` puts the old names back with the same rows.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text

from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "624702237252"
_BELOW = "c705e85dec9b"
TOKENS = {1: "a" * 32, 2: "b" * 32}


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("connect_links", below) as database:
        yield database


def _seed(engine: Engine) -> None:
    with engine.begin() as connection:
        for user_id in (1, 2, 3):
            connection.execute(
                text(
                    'INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at, '
                    "dive_form_hidden_fields) VALUES (:id, 'Seed', :username, :email, false, false, "
                    "gen_random_uuid(), now(), CAST('[]' AS json))"
                ),
                {"id": user_id, "username": f"seed{user_id}", "email": f"seed{user_id}@example.com"},
            )
        for user_id, token in TOKENS.items():
            connection.execute(
                text("INSERT INTO invite_link (user_id, token, created_at) VALUES (:user_id, :token, now())"),
                {"user_id": user_id, "token": token},
            )
        connection.execute(
            text(
                "INSERT INTO person (user_id, name, notes, linked_user_id, uuid, created_at) VALUES "
                "(1, 'Two', '', 2, gen_random_uuid(), now()), (1, 'Nobody', '', NULL, gen_random_uuid(), now()), "
                "(3, 'One', '', 1, gen_random_uuid(), now())"
            )
        )
        for event_type in ("invite_link_created", "invite_link_removed", "sign_in_succeeded"):
            connection.execute(
                text(
                    "INSERT INTO auth_audit_event (event_type, ip, user_agent, user_id, created_at) "
                    "VALUES (:event_type, '127.0.0.1', 'test', 1, now())"
                ),
                {"event_type": event_type},
            )


def _rows(engine: Engine, sql: str) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql)))


def _ties(engine: Engine, column: str) -> set[tuple[int, str, int | None]]:
    return {tuple(row) for row in _rows(engine, f"SELECT user_id, name, {column} FROM person")}  # type: ignore[misc]


def _events(engine: Engine) -> list[str]:
    return sorted(row.event_type for row in _rows(engine, "SELECT event_type FROM auth_audit_event"))


TIES = {(1, "Two", 2), (1, "Nobody", None), (3, "One", 1)}


class TestTheUpgrade:
    def test_every_token_and_every_tie_carry_over(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        _seed(engine)

        migrate(name, "upgrade", _REVISION)

        links = _rows(engine, "SELECT user_id, token FROM connect_link ORDER BY user_id")
        assert {row.user_id: row.token for row in links} == TOKENS
        assert _ties(engine, "connected_user_id") == TIES
        assert {row.connection_hidden_at for row in _rows(engine, "SELECT connection_hidden_at FROM person")} == {None}
        assert _rows(engine, "SELECT id FROM account_disconnect") == []
        assert _events(engine) == ["connect_link_created", "connect_link_removed", "sign_in_succeeded"]
        assert _rows(engine, "SELECT to_regclass('invite_link') AS gone")[0].gone is None

    def test_the_renamed_constraints_still_hold(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        _seed(engine)
        migrate(name, "upgrade", _REVISION)

        names = {
            row.name
            for row in _rows(
                engine,
                "SELECT conname AS name FROM pg_constraint WHERE conrelid = 'person'::regclass "
                "UNION SELECT indexname FROM pg_indexes WHERE tablename = 'person'",
            )
        }

        assert {
            "ck_person_not_connected_to_its_owner",
            "person_connected_user_id_fkey",
            "ix_person_connected_user_id",
            "ux_person_user_id_connected_user_id",
        } <= names
        assert not {name for name in names if "linked" in name}
        with pytest.raises(Exception, match="ux_person_user_id_connected_user_id"), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO person (user_id, name, notes, connected_user_id, uuid, created_at) "
                    "VALUES (1, 'Two again', '', 2, gen_random_uuid(), now())"
                )
            )


class TestTheDowngrade:
    def test_it_puts_the_old_names_back_with_the_same_rows(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        _seed(engine)
        migrate(name, "upgrade", _REVISION)

        migrate(name, "downgrade", _BELOW)

        links = _rows(engine, "SELECT user_id, token FROM invite_link ORDER BY user_id")
        assert {row.user_id: row.token for row in links} == TOKENS
        assert _ties(engine, "linked_user_id") == TIES
        assert _events(engine) == ["invite_link_created", "invite_link_removed", "sign_in_succeeded"]
