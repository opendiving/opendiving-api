"""Revision `ab9a5add4fee` run for real, against a database of its own
(`tests/helpers/migrations.py`): every existing dive keeps its time of day, and the downgrade
refuses to turn a date-only dive into a midnight.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import db_available
from tests.helpers.migrations import alembic, migrate, scratch_database

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

_REVISION = "ab9a5add4fee"
_BELOW = "ce09bc7d4c64"

SEED = """
INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at)
VALUES (1, 'Seed', 'seed', 'seed@example.com', false, false, gen_random_uuid(), now());
INSERT INTO dive (id, user_id, dive_number, start_time, utc_offset_minutes, duration, notes, uuid, created_at,
                  is_deleted)
VALUES (1, 1, 1, '2025-01-01 10:00:00+00', 120, 3000, '', gen_random_uuid(), now(), false),
       (2, 1, 2, '2025-01-02 00:00:00+00', NULL, 3000, '', gen_random_uuid(), now(), false);
"""


@pytest.fixture
def scratch() -> Iterator[tuple[str, Engine]]:
    with scratch_database("date_only") as database:
        yield database


def _upgraded(scratch: tuple[str, Engine]) -> tuple[str, Engine]:
    name, engine = scratch
    migrate(name, "upgrade", _BELOW)
    with engine.begin() as connection:
        connection.execute(text(SEED))
    migrate(name, "upgrade", _REVISION)
    return name, engine


def test_every_existing_dive_keeps_its_time_of_day(scratch: tuple[str, Engine]) -> None:
    """An offset-unknown midnight included: the wall clock it holds is a time somebody recorded."""
    _, engine = _upgraded(scratch)

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, start_date_only FROM dive ORDER BY id")).all()
    assert [tuple(row) for row in rows] == [(1, False), (2, False)]


def test_a_date_only_dive_cannot_carry_an_offset(scratch: tuple[str, Engine]) -> None:
    _, engine = _upgraded(scratch)

    with pytest.raises(IntegrityError, match="ck_dive_start_date_only_has_no_offset"), engine.begin() as connection:
        connection.execute(text("UPDATE dive SET start_date_only = true WHERE id = 1"))


def test_the_downgrade_refuses_while_a_dive_is_date_only_and_runs_once_none_is(scratch: tuple[str, Engine]) -> None:
    name, engine = _upgraded(scratch)
    with engine.begin() as connection:
        connection.execute(text("UPDATE dive SET start_date_only = true WHERE id = 2"))

    refused = alembic(name, "downgrade", _BELOW)

    assert refused.returncode != 0
    assert "no time of day" in refused.stderr
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _REVISION

    with engine.begin() as connection:
        connection.execute(text("UPDATE dive SET start_date_only = false WHERE id = 2"))
    migrate(name, "downgrade", _BELOW)

    with engine.connect() as connection:
        columns = set(
            connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'dive'")
            ).scalars()
        )
    assert "start_date_only" not in columns
