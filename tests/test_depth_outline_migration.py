"""Revision `0f941e1c4130` run for real, against a database of its own
(`tests/helpers/migrations.py`): every stored profile gets its `depth_outline` from its own
`data`, in batches, and the downgrade drops the column.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import json
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

_REVISION = "0f941e1c4130"
_BELOW = "e8b670ac5780"

# One reading a second for 64 s, so each of the 64 slices holds exactly one - except the last,
# which also takes the reading on the far edge, and keeps the deeper of the two.
_DEPTH = {"t": [second * 1000 for second in range(65)], "v": [second * 10 for second in range(64)] + [0]}

SEED = """
INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at)
VALUES (1, 'Seed', 'seed', 'seed@example.com', false, false, gen_random_uuid(), now());
INSERT INTO dive (id, user_id, dive_number, start_time, utc_offset_minutes, duration, notes, uuid, created_at,
                  is_deleted)
VALUES (1, 1, 1, '2025-01-01 10:00:00+00', 0, 3000, '', gen_random_uuid(), now(), false);
INSERT INTO dive_recording (id, dive_id, user_id, ordinal, uuid, created_at)
SELECT n, 1, 1, n, gen_random_uuid(), now() FROM generate_series(1, 205) AS n;
INSERT INTO dive_profile (dive_id, recording_id, source_sha256, parser_key, extractor_version, duration,
                          depth_sample_count, data, uuid, created_at)
SELECT 1, n, repeat('a', 64), CASE WHEN n = 1 THEN 'suunto_json' ELSE 'divejson_import' END, 8, 64000,
       65, CAST(:depth AS jsonb), gen_random_uuid(), now()
  FROM generate_series(1, 202) AS n;
INSERT INTO dive_profile (dive_id, recording_id, source_sha256, parser_key, extractor_version, duration,
                          depth_sample_count, data, uuid, created_at)
VALUES (1, 203, repeat('b', 64), 'suunto_json', 8, 2000, 0, CAST(:temperature AS jsonb), gen_random_uuid(), now()),
       (1, 204, repeat('c', 64), 'suunto_json', 8, 0, 1, CAST(:single AS jsonb), gen_random_uuid(), now()),
       (1, 205, repeat('d', 64), 'suunto_json', 8, 0, 0, CAST('{}' AS jsonb), gen_random_uuid(), now())
"""


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("outline", below) as database:
        yield database


def _seed(engine: Engine) -> None:
    payloads = {
        "depth": json.dumps({"depth": _DEPTH, "temperature": {"t": [0, 1000], "v": [219, 218]}}),
        "temperature": json.dumps({"temperature": {"t": [0, 2000], "v": [219, 218]}}),
        "single": json.dumps({"depth": {"t": [0], "v": [1200]}}),
    }
    with engine.begin() as connection:
        for statement in SEED.split(";\n"):
            connection.execute(text(statement), payloads)


def _outlines(engine: Engine) -> dict[int, Any]:
    with engine.connect() as connection:
        return {
            row.recording_id: row.depth_outline
            for row in connection.execute(text("SELECT recording_id, depth_outline FROM dive_profile"))
        }


class TestTheUpgrade:
    def test_every_profile_with_a_depth_curve_gets_one_across_batches(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        _seed(engine)

        migrate(name, "upgrade", _REVISION)

        outlines = _outlines(engine)
        expected = {"span": 64_000, "values": [second * 10 for second in range(64)]}
        # An imported profile too: it is derived from `data`, which every row has.
        assert all(outlines[recording] == expected for recording in range(1, 203))
        # A temperature-only profile, a single reading and an empty payload have nothing to draw.
        assert (outlines[203], outlines[204], outlines[205]) == (None, None, None)


class TestTheDowngrade:
    def test_it_drops_the_column(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        _seed(engine)
        migrate(name, "upgrade", _REVISION)

        migrate(name, "downgrade", _BELOW)

        with engine.connect() as connection:
            columns = connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'dive_profile'")
            ).scalars()
            assert "depth_outline" not in set(columns)
