"""Revision `671e5dc854e0` run for real, against a database of its own
(`tests/helpers/migrations.py`).

What is worth pinning is what a stored sighting becomes: every row the table already holds
keeps its count and carries no band; the outgoing build's insert, which names no band, still
lands; a row carrying both is refused; and the downgrade takes the band and leaves the count.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

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

_REVISION = "671e5dc854e0"
_BELOW = "e41fcd2a6308"

SEED = """
INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at)
VALUES (1, 'Seed', 'seed', 'seed@example.com', false, false, gen_random_uuid(), now());
INSERT INTO dive (id, user_id, dive_number, start_time, submerged_time, notes, uuid, created_at, is_deleted)
VALUES (1, 1, 1, '2025-01-01 10:00:00+00', 3000, '', gen_random_uuid(), now(), false);
INSERT INTO species (id, aphia_id, scientific_name, rank, status, uuid, created_at)
VALUES (1, 900000001, 'zzfixture-one', 'Species', 'accepted', gen_random_uuid(), now()),
       (2, 900000002, 'zzfixture-two', 'Species', 'accepted', gen_random_uuid(), now()),
       (3, 900000003, 'zzfixture-three', 'Species', 'accepted', gen_random_uuid(), now());
INSERT INTO dive_species (dive_id, species_id, position, count, notes)
VALUES (1, 1, 0, 4, 'Four.'), (1, 2, 1, NULL, '');
"""


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("abundance", below) as database:
        yield database


def _upgraded(scratch: tuple[str, Engine]) -> tuple[str, Engine]:
    name, engine = scratch
    with engine.begin() as connection:
        connection.execute(text(SEED))
    migrate(name, "upgrade", _REVISION)
    return name, engine


def _rows(engine: Engine, sql: str) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql)))


class TestTheUpgrade:
    def test_every_stored_sighting_keeps_its_count_and_carries_no_band(self, scratch: tuple[str, Engine]) -> None:
        _, engine = _upgraded(scratch)

        rows = _rows(engine, "SELECT species_id, count, abundance, notes FROM dive_species ORDER BY position")
        assert [tuple(row) for row in rows] == [(1, 4, None, "Four."), (2, None, None, "")]

    def test_the_outgoing_builds_insert_still_lands(self, scratch: tuple[str, Engine]) -> None:
        """The previous build names no band, and keeps inserting for as long as a deploy
        overlaps it."""
        _, engine = _upgraded(scratch)

        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO dive_species (dive_id, species_id, position, count) VALUES (1, 3, 2, 7)")
            )

        (row,) = _rows(engine, "SELECT count, abundance FROM dive_species WHERE species_id = 3")
        assert (row.count, row.abundance) == (7, None)

    def test_a_band_beside_a_count_is_refused(self, scratch: tuple[str, Engine]) -> None:
        _, engine = _upgraded(scratch)

        with (
            pytest.raises(IntegrityError, match="ck_dive_species_count_or_abundance"),
            engine.begin() as connection,
        ):
            connection.execute(text("UPDATE dive_species SET abundance = 'few' WHERE species_id = 1"))

    def test_a_band_alone_is_stored(self, scratch: tuple[str, Engine]) -> None:
        _, engine = _upgraded(scratch)

        with engine.begin() as connection:
            connection.execute(text("UPDATE dive_species SET abundance = 'many' WHERE species_id = 2"))

        (row,) = _rows(engine, "SELECT count, abundance FROM dive_species WHERE species_id = 2")
        assert (row.count, row.abundance) == (None, "many")


class TestTheDowngrade:
    def test_the_band_goes_and_the_count_stays(self, scratch: tuple[str, Engine]) -> None:
        name, engine = _upgraded(scratch)
        with engine.begin() as connection:
            connection.execute(text("UPDATE dive_species SET abundance = 'many' WHERE species_id = 2"))

        migrate(name, "downgrade", _BELOW)

        columns = {
            row.column_name
            for row in _rows(
                engine, "SELECT column_name FROM information_schema.columns WHERE table_name = 'dive_species'"
            )
        }
        assert "abundance" not in columns
        rows = _rows(engine, "SELECT species_id, count FROM dive_species ORDER BY position")
        assert [tuple(row) for row in rows] == [(1, 4), (2, None)]
