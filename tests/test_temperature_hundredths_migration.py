"""Revision `f3a9c1d27b64` run for real, against a database of its own
(`tests/helpers/migrations.py`): every stored temperature is multiplied into hundredths whatever
wrote it, the summary columns are renamed, and the downgrade puts both back.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import importlib.util
import json
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import Engine, text

from src.app.core.db.migrations import MIGRATIONS_PATH
from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "f3a9c1d27b64"
_BELOW = "1538257d95db"
_MODULE = "f3a9c1d27b64_profile_temperatures_are_hundredths_of_a_"
_INT32_MAX = 2**31 - 1

FILE = {
    "depth": {"t": [0, 60_000], "v": [0, 1840]},
    "temperature": {"t": [0, 1000, 2000], "v": [267, 271, 268]},
    "pressure": [{"gas_number": 0, "t": [0], "v": [2052]}],
    "events": [{"t": 0, "type": "bookmark"}],
}
IMPORTED = {"temperature": {"t": [0, 1000], "v": [-15, 5]}}
NO_TEMPERATURE = {"depth": {"t": [0, 1000], "v": [0, 500]}}
PAST_INT32 = {"temperature": {"t": [0, 1000], "v": [-300_000_000, 300_000_000]}}

SEED = """
INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at)
VALUES (1, 'Seed', 'seed', 'seed@example.com', false, false, gen_random_uuid(), now());
INSERT INTO dive (id, user_id, dive_number, start_time, utc_offset_minutes, duration, notes, uuid, created_at,
                  is_deleted)
VALUES (1, 1, 1, '2025-01-01 10:00:00+00', 0, 3000, '', gen_random_uuid(), now(), false);
INSERT INTO dive_recording (id, dive_id, user_id, ordinal, uuid, created_at)
SELECT n, 1, 1, n - 1, gen_random_uuid(), now() FROM generate_series(1, 4) AS n;
INSERT INTO dive_profile (dive_id, recording_id, source_sha256, parser_key, extractor_version, duration,
                          depth_sample_count, data, min_temperature_c10, max_temperature_c10, uuid, created_at)
VALUES (1, 1, repeat('a', 64), 'suunto_json', 9, 60000, 2, CAST(:file AS jsonb), 267, 271, gen_random_uuid(), now()),
       (1, 2, repeat('b', 64), 'divejson_import', 9, 1000, 0, CAST(:imported AS jsonb), -15, 5, gen_random_uuid(),
        now()),
       (1, 3, repeat('c', 64), 'merge', 9, 1000, 2, CAST(:none AS jsonb), NULL, NULL, gen_random_uuid(), now()),
       (1, 4, repeat('d', 64), 'divejson_import', 9, 1000, 0, CAST(:past AS jsonb), -300000000, 300000000,
        gen_random_uuid(), now())
"""


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE, MIGRATIONS_PATH / "versions" / f"{_MODULE}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("hundredths", below) as database:
        name, engine = database
        payloads = {
            "file": json.dumps(FILE),
            "imported": json.dumps(IMPORTED),
            "none": json.dumps(NO_TEMPERATURE),
            "past": json.dumps(PAST_INT32),
        }
        with engine.begin() as connection:
            for statement in SEED.split(";\n"):
                connection.execute(text(statement), payloads)
        yield name, engine


def _profiles(engine: Engine, suffix: str) -> dict[int, Any]:
    with engine.connect() as connection:
        return {
            row.recording_id: row
            for row in connection.execute(
                text(
                    f"SELECT recording_id, data, min_temperature_{suffix} AS low, max_temperature_{suffix} AS high, "  # noqa: S608
                    "updated_at, extractor_version, reader_version FROM dive_profile"
                )
            )
        }


class TestTheUpgrade:
    def test_every_temperature_is_multiplied_whatever_wrote_it(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        rows = _profiles(engine, "c100")
        assert rows[1].data == {**FILE, "temperature": {"t": [0, 1000, 2000], "v": [2670, 2710, 2680]}}
        assert (rows[1].low, rows[1].high) == (2670, 2710)
        assert rows[2].data == {"temperature": {"t": [0, 1000], "v": [-150, 50]}}
        assert (rows[2].low, rows[2].high) == (-150, 50)
        # Nothing moves the versions: the pin bump, not this, is what puts a file-backed row behind.
        assert {(row.extractor_version, row.reader_version) for row in rows.values()} == {(9, None)}

    def test_a_row_with_no_temperature_is_untouched(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        rows = _profiles(engine, "c100")
        assert (rows[3].data, rows[3].low, rows[3].high, rows[3].updated_at) == (NO_TEMPERATURE, None, None, None)
        assert all(rows[recording].updated_at is not None for recording in (1, 2, 4))

    def test_a_value_past_a_32_bit_integer_is_clamped(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        row = _profiles(engine, "c100")[4]
        assert row.data["temperature"]["v"] == [-(2**31), _INT32_MAX]
        assert (row.low, row.high) == (-(2**31), _INT32_MAX)


class TestTheDowngrade:
    def test_it_puts_every_row_it_wrote_back(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        before = _profiles(engine, "c10")

        migrate(name, "upgrade", _REVISION)
        migrate(name, "downgrade", _BELOW)

        after = _profiles(engine, "c10")
        for recording in (1, 2, 3):
            assert (after[recording].data, after[recording].low, after[recording].high) == (
                before[recording].data,
                before[recording].low,
                before[recording].high,
            )

    def test_a_refined_row_goes_back_to_its_tenths(self, scratch: tuple[str, Engine]) -> None:
        """What a backfill wrote after the upgrade - the readings the file states - divides back
        to the reading tenths would have held."""
        name, engine = scratch
        migrate(name, "upgrade", _REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE dive_profile SET data = jsonb_set(data, '{temperature,v}', '[2669, 2711, 2675]'), "
                    "min_temperature_c100 = 2669, max_temperature_c100 = 2711 WHERE recording_id = 1"
                )
            )

        migrate(name, "downgrade", _BELOW)

        row = _profiles(engine, "c10")[1]
        assert (row.data["temperature"]["v"], row.low, row.high) == ([267, 271, 268], 267, 271)


class TestTheArithmetic:
    """The two directions, without a database."""

    @pytest.mark.parametrize(
        ("hundredths", "tenths"), [(2675, 268), (2674, 267), (-155, -16), (-154, -15), (0, 0), (-5, -1)]
    )
    def test_the_downgrade_rounds_half_away_from_zero(self, hundredths: int, tenths: int) -> None:
        assert _revision().to_tenths(hundredths) == tenths

    def test_only_the_temperature_readings_move(self) -> None:
        stored = {**FILE, "ceiling": {"t": [0], "v": [300]}}

        moved = _revision().rescaled(stored, lambda value: value * 10)

        assert moved == {**stored, "temperature": {"t": [0, 1000, 2000], "v": [2670, 2710, 2680]}}
