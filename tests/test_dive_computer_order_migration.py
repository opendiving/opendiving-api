"""Revision `b3f1c7a9d2e4` run for real, against a database of its own
(`tests/helpers/migrations.py`).

One additive column for the accounts the hosted instance already holds: each reads as the empty
order, and nothing else on the row moves.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "b3f1c7a9d2e4"
_BELOW = "e316b5f7b995"


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def upgraded(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("computerorder", below) as (name, engine):
        with engine.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at, '
                    "dive_form_hidden_fields) VALUES (1, 'Seed', 'seed1', 'seed@example.com', false, false, "
                    "gen_random_uuid(), now(), '[\"altitude\"]')"
                )
            )
        migrate(name, "upgrade", _REVISION)
        yield name, engine


def test_an_existing_account_has_placed_no_computer_and_keeps_the_rest(upgraded: tuple[str, Engine]) -> None:
    _, engine = upgraded
    with engine.connect() as connection:
        row = connection.execute(text('SELECT dive_computer_order, dive_form_hidden_fields FROM "user"')).one()

    assert (row.dive_computer_order, row.dive_form_hidden_fields) == ([], ["altitude"])


def test_the_downgrade_drops_the_column(upgraded: tuple[str, Engine]) -> None:
    name, engine = upgraded
    migrate(name, "downgrade", _BELOW)

    with engine.connect() as connection:
        columns = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'user' AND column_name = 'dive_computer_order'"
            )
        ).all()

    assert columns == []
