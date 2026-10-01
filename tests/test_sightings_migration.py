"""Revision `c47b308253a3` run for real, against a database of its own
(`tests/helpers/migrations.py`).

What is worth pinning is what a stored sighting and a stored hidden set become: every row the
table already holds reads as seen, not counted, with no note; the outgoing build's insert,
which names neither column, still lands; and `species_uuids` becomes `sightings` in the slot it
held in every set - and comes back on the downgrade.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "c47b308253a3"
_BELOW = "b5dad8793a54"

# (id, user, name, hidden_fields)
PRESETS = [
    (1, 1, "Basic", ["trip_uuid", "weight", "species_uuids", "mixture.usage"]),
    (2, 1, "Recreational", ["altitude", "mixture.po2_limit"]),
    (3, 2, "Only", ["species_uuids"]),
]
USER_HIDDEN = {1: ["species_uuids", "notes"], 2: []}

SEED = """
INSERT INTO dive (id, user_id, dive_number, start_time, utc_offset_minutes, duration, notes, uuid, created_at,
                  is_deleted)
VALUES (1, 1, 1, '2025-01-01 10:00:00+00', 120, 3000, '', gen_random_uuid(), now(), false);
INSERT INTO species (id, aphia_id, scientific_name, rank, status, uuid, created_at)
VALUES (1, 900000001, 'zzfixture-one', 'Species', 'accepted', gen_random_uuid(), now()),
       (2, 900000002, 'zzfixture-two', 'Species', 'accepted', gen_random_uuid(), now()),
       (3, 900000003, 'zzfixture-three', 'Species', 'accepted', gen_random_uuid(), now());
INSERT INTO dive_species (dive_id, species_id, position) VALUES (1, 1, 0), (1, 2, 1);
"""


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("sightings", below) as database:
        yield database


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
        for preset_id, user_id, name, hidden in PRESETS:
            connection.execute(
                text(
                    "INSERT INTO dive_form_preset (id, user_id, name, hidden_fields, uuid, created_at) "
                    "VALUES (:id, :user_id, :name, CAST(:hidden AS json), gen_random_uuid(), now())"
                ),
                {"id": preset_id, "user_id": user_id, "name": name, "hidden": json.dumps(hidden)},
            )
        connection.execute(text(SEED))


def _upgraded(scratch: tuple[str, Engine]) -> tuple[str, Engine]:
    name, engine = scratch
    _seed(engine)
    migrate(name, "upgrade", _REVISION)
    return name, engine


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
    def test_every_stored_sighting_reads_as_seen_not_counted(self, scratch: tuple[str, Engine]) -> None:
        _, engine = _upgraded(scratch)

        rows = _rows(engine, "SELECT species_id, count, notes FROM dive_species ORDER BY position")
        assert [tuple(row) for row in rows] == [(1, None, ""), (2, None, "")]

    def test_the_outgoing_builds_insert_still_lands(self, scratch: tuple[str, Engine]) -> None:
        """The previous build names neither column, and keeps inserting for as long as a
        deploy overlaps it."""
        _, engine = _upgraded(scratch)

        with engine.begin() as connection:
            connection.execute(text("INSERT INTO dive_species (dive_id, species_id, position) VALUES (1, 3, 2)"))

        (row,) = _rows(engine, "SELECT count, notes FROM dive_species WHERE species_id = 3")
        assert (row.count, row.notes) == (None, "")

    def test_a_count_below_one_is_refused(self, scratch: tuple[str, Engine]) -> None:
        _, engine = _upgraded(scratch)

        with pytest.raises(IntegrityError, match="ck_dive_species_count_positive"), engine.begin() as connection:
            connection.execute(text("UPDATE dive_species SET count = 0 WHERE species_id = 1"))

    def test_every_set_hiding_the_old_key_hides_the_new_one_in_its_slot(self, scratch: tuple[str, Engine]) -> None:
        _, engine = _upgraded(scratch)

        assert _hidden_sets(engine) == {
            "preset 1": ["trip_uuid", "weight", "sightings", "mixture.usage"],
            "preset 2": ["altitude", "mixture.po2_limit"],
            "preset 3": ["sightings"],
            "user 1": ["sightings", "notes"],
            "user 2": [],
        }


class TestTheDowngrade:
    def test_the_old_key_comes_back_and_the_columns_go(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        _seed(engine)
        hidden_before = _hidden_sets(engine)
        migrate(name, "upgrade", _REVISION)
        with engine.begin() as connection:
            connection.execute(text("UPDATE dive_species SET count = 4, notes = 'Lost on the downgrade.'"))

        migrate(name, "downgrade", _BELOW)

        assert _hidden_sets(engine) == hidden_before
        columns = {
            row.column_name
            for row in _rows(
                engine, "SELECT column_name FROM information_schema.columns WHERE table_name = 'dive_species'"
            )
        }
        assert not {"count", "notes"} & columns
        assert len(_rows(engine, "SELECT id FROM dive_species")) == 2
