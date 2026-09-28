"""Revision `e02a39ada562` run for real, against a database of its own
(`tests/helpers/migrations.py`).

The backfill is the data path for every account the hosted instance already holds, so what is
pinned is where each one lands: the earliest under `bootstrap`, an accepted invitation under
`invitation` - matched on the lowercased address, which is the case a Google-born account
tests - and the rest under `open`, each on its UTC day. And that the statement can run again
without moving a number.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import importlib.util
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text

from src.app.core.db.migrations import MIGRATIONS_PATH
from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

_REVISION = "e02a39ada562"
_BELOW = "6849ff025422"

# (id, email, created_at) - the second is how Google hands over an address; the fourth was
# created on the evening of the 2nd in New York, which is the 3rd in UTC.
USERS = [
    (1, "operator@example.com", "2026-01-01 08:00:00+00"),
    (2, "Invited.Diver@Example.COM", "2026-01-02 09:00:00+00"),
    (3, "pending@example.com", "2026-01-02 10:00:00+00"),
    (4, "late@example.com", "2026-01-02 23:30:00-05"),
]
# (email, inviter, accepted) - only an accepted invitation says how an account got in.
INVITATIONS = [
    ("invited.diver@example.com", 1, True),
    ("pending@example.com", 1, False),
]


def _revision() -> Any:
    path = next((MIGRATIONS_PATH / "versions").glob(f"{_REVISION}_*.py"))
    spec = importlib.util.spec_from_file_location(f"revision_{_REVISION}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def upgraded() -> Iterator[tuple[str, Engine]]:
    with scratch_database("joinlinks") as (name, engine):
        migrate(name, "upgrade", _BELOW)
        with engine.begin() as connection:
            for user_id, email, created_at in USERS:
                connection.execute(
                    text(
                        'INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at) '
                        "VALUES (:id, 'Seed', :username, :email, false, false, gen_random_uuid(), :created_at)"
                    ),
                    {"id": user_id, "username": f"seed{user_id}", "email": email, "created_at": created_at},
                )
            for email, inviter, accepted in INVITATIONS:
                connection.execute(
                    text(
                        "INSERT INTO invitation (email, user_id, uuid, created_at, accepted_at) VALUES "
                        "(:email, :inviter, gen_random_uuid(), now(), CASE WHEN :accepted THEN now() END)"
                    ),
                    {"email": email, "inviter": inviter, "accepted": accepted},
                )
        migrate(name, "upgrade", _REVISION)
        yield name, engine


def _totals(engine: Engine) -> set[tuple[str, str, str, int]]:
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT day, metric, key, count FROM daily_total"))
        return {(day.isoformat(), metric, key, count) for day, metric, key, count in rows}


EXPECTED = {
    ("2026-01-01", "accounts_created", "bootstrap", 1),
    ("2026-01-02", "accounts_created", "invitation", 1),
    ("2026-01-02", "accounts_created", "open", 1),
    ("2026-01-03", "accounts_created", "open", 1),
}


class TestTheBackfill:
    def test_every_account_lands_under_its_door_on_its_utc_day(self, upgraded: tuple[str, Engine]) -> None:
        _, engine = upgraded

        assert _totals(engine) == EXPECTED

    def test_running_it_again_moves_nothing(self, upgraded: tuple[str, Engine]) -> None:
        _, engine = upgraded
        with engine.begin() as connection:
            connection.execute(text(_revision().BACKFILL_ACCOUNTS_CREATED))

        assert _totals(engine) == EXPECTED

    def test_it_never_lowers_a_count_already_there(self, upgraded: tuple[str, Engine]) -> None:
        """The same guard the hourly writers use: a day counted higher than the rows now
        support - an account since purged - keeps its count."""
        _, engine = upgraded
        with engine.begin() as connection:
            connection.execute(text("UPDATE daily_total SET count = 5 WHERE day = '2026-01-02' AND key = 'open'"))
            connection.execute(text(_revision().BACKFILL_ACCOUNTS_CREATED))

        assert ("2026-01-02", "accounts_created", "open", 5) in _totals(engine)

    def test_the_new_columns_read_as_nothing_on_existing_rows(self, upgraded: tuple[str, Engine]) -> None:
        _, engine = upgraded
        with engine.connect() as connection:
            flags = connection.execute(text("SELECT DISTINCT from_invite_request FROM invitation")).scalars().all()

        assert flags == [False]


class TestTheDowngrade:
    def test_the_table_and_both_columns_go(self, upgraded: tuple[str, Engine]) -> None:
        name, engine = upgraded
        migrate(name, "downgrade", _BELOW)

        with engine.connect() as connection:
            tables = connection.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_name = 'daily_total'")
            ).scalar()
            columns = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE "
                    "(table_name = 'invitation' AND column_name = 'from_invite_request') OR "
                    "(table_name = 'authentication_request' AND column_name = 'via')"
                )
            ).all()

        assert tables == 0
        assert columns == []
