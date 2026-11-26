"""Revision `0773a20d01b1` run for real, against a database of its own
(`tests/helpers/migrations.py`): every account's `unit_preferences` comes out holding all five
families, each the account's own override where it had one and its `units` otherwise.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from uuid6 import uuid7

from tests.conftest import db_available, unique_email, unique_username
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "0773a20d01b1"
_BELOW = "28d1ff486573"
_FAMILIES = ("depth", "pressure", "volume", "weight", "temperature")


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("unit_families", below) as database:
        yield database


def _user(db: Session, units: str, preferences: dict[str, str] | None = None) -> int:
    """A user row in this revision's schema, written in SQL: the model no longer maps `units`."""
    user_id = db.execute(
        text(
            'INSERT INTO "user" (name, username, email, is_superuser, is_deleted, uuid, created_at, units'
            + (", unit_preferences" if preferences is not None else "")
            + ") VALUES ('Seed', :username, :email, false, false, :uuid, now(), :units"
            + (", :preferences" if preferences is not None else "")
            + ") RETURNING id"
        ),
        {
            "username": unique_username(),
            "email": unique_email(),
            "uuid": uuid7(),
            "units": units,
            "preferences": json.dumps(preferences),
        },
    ).scalar_one()
    db.commit()
    return int(user_id)


def _preferences(engine: Engine, user_id: int) -> Any:
    with Session(engine) as db:
        return db.execute(text('SELECT unit_preferences FROM "user" WHERE id = :id'), {"id": user_id}).scalar_one()


class TestTheUpgrade:
    def test_every_record_holds_every_family(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        complete = dict.fromkeys(_FAMILIES, "imperial") | {"pressure": "metric", "weight": "metric"}
        with Session(engine) as db:
            imperial = _user(db, "imperial")
            partial = _user(db, "imperial", {"pressure": "metric", "temperature": "metric"})
            untouched = _user(db, "metric", complete)

        migrate(name, "upgrade", _REVISION)

        assert _preferences(engine, imperial) == dict.fromkeys(_FAMILIES, "imperial")
        assert _preferences(engine, partial) == dict.fromkeys(_FAMILIES, "imperial") | {
            "pressure": "metric",
            "temperature": "metric",
        }
        assert _preferences(engine, untouched) == complete

    def test_a_new_account_holds_every_family_at_metric(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _REVISION)

        with Session(engine) as db:
            created = _user(db, "imperial")

        assert _preferences(engine, created) == dict.fromkeys(_FAMILIES, "metric")


class TestTheDowngrade:
    def test_each_account_reads_as_it_did_under_the_preset(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        with Session(engine) as db:
            partial = _user(db, "imperial", {"pressure": "metric", "depth": "imperial"})
            plain = _user(db, "metric")
        migrate(name, "upgrade", _REVISION)

        migrate(name, "downgrade", _BELOW)

        assert _preferences(engine, partial) == {"pressure": "metric"}
        assert _preferences(engine, plain) == {}
