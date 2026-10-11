"""Revision `53ea55b922e5`'s backfill run for real, against a database of its own at the revision
below it (`tests/helpers/migrations.py`): a dive with no bottom temperature takes its primary
recording's coldest sample, as an import of it then would. Its SQL names `min_temperature_c10`,
which a later revision renames, so it runs on the schema it was written against.

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

_REVISION = "53ea55b922e5"
_BELOW = "cf3c73ed02f4"

# Dive 1 has no temperature and a primary profile at 28.2 C; dive 2 states its own; dive 3's primary
# has no temperature channel; dive 4's coldest reading is on its second recording alone.
SEED = """
INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at)
VALUES (1, 'Seed', 'seed', 'seed@example.com', false, false, gen_random_uuid(), now());
INSERT INTO dive (id, user_id, dive_number, start_time, utc_offset_minutes, duration, notes, uuid, created_at,
                  is_deleted, bottom_temperature)
VALUES (1, 1, 1, '2025-01-01 10:00:00+00', 0, 3000, '', gen_random_uuid(), now(), false, NULL),
       (2, 1, 2, '2025-01-02 10:00:00+00', 0, 3000, '', gen_random_uuid(), now(), false, 25.0),
       (3, 1, 3, '2025-01-03 10:00:00+00', 0, 3000, '', gen_random_uuid(), now(), false, NULL),
       (4, 1, 4, '2025-01-04 10:00:00+00', 0, 3000, '', gen_random_uuid(), now(), false, NULL);
INSERT INTO dive_recording (id, dive_id, user_id, ordinal, uuid, created_at)
VALUES (1, 1, 1, 0, gen_random_uuid(), now()),
       (2, 2, 1, 0, gen_random_uuid(), now()),
       (3, 3, 1, 0, gen_random_uuid(), now()),
       (4, 4, 1, 0, gen_random_uuid(), now()),
       (5, 4, 1, 1, gen_random_uuid(), now());
INSERT INTO dive_profile (dive_id, recording_id, source_sha256, parser_key, extractor_version, duration,
                          depth_sample_count, data, min_temperature_c10, uuid, created_at)
VALUES (1, 1, repeat('b', 64), 'suunto_json', 3, 1800, 2, '{}', 282, gen_random_uuid(), now()),
       (2, 2, repeat('b', 64), 'suunto_json', 3, 1800, 2, '{}', 282, gen_random_uuid(), now()),
       (3, 3, repeat('b', 64), 'suunto_json', 3, 1800, 2, '{}', NULL, gen_random_uuid(), now()),
       (4, 4, repeat('b', 64), 'suunto_json', 3, 1800, 2, '{}', NULL, gen_random_uuid(), now()),
       (4, 5, repeat('b', 64), 'suunto_json', 3, 1800, 2, '{}', 282, gen_random_uuid(), now())
"""


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("bottom", below) as database:
        yield database


def _bottom_temperatures(engine: Engine) -> dict[int, float | None]:
    with engine.connect() as connection:
        return {
            row.id: row.bottom_temperature
            for row in connection.execute(text("SELECT id, bottom_temperature FROM dive"))
        }


def test_only_a_dive_with_none_takes_its_primary_recordings_coldest_sample(scratch: tuple[str, Engine]) -> None:
    name, engine = scratch
    with engine.begin() as connection:
        for statement in SEED.split(";\n"):
            connection.execute(text(statement))

    migrate(name, "upgrade", _REVISION)

    assert _bottom_temperatures(engine) == {1: 282 / 10, 2: 25.0, 3: None, 4: None}
