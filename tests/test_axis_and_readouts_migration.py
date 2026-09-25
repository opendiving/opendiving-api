"""Revision `ce09bc7d4c64` run for real, against a database of its own.

The suite's database is at head, where the dive columns this revision moves are already
gone, so each test here migrates a fresh database to the revision below, seeds it in that
schema, and upgrades and downgrades it through the Alembic CLI - a subprocess, because
`settings` is built once at import and the database it names is what `migrations/env.py`
connects to. What is worth pinning is what Postgres does with the data moves, and that
`downgrade()` puts each one back.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from src.app.core.config import postgres_uri, settings
from src.app.core.db.migrations import MIGRATIONS_PATH
from tests.conftest import db_available

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

_REVISION = "ce09bc7d4c64"
_BELOW = "dd420c8df9de"
_MODULE = "ce09bc7d4c64_the_profile_axis_is_milliseconds_and_a_"


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE, MIGRATIONS_PATH / "versions" / f"{_MODULE}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine(database: str, **options: Any) -> Engine:
    return create_engine(
        settings.POSTGRES_SYNC_PREFIX
        + postgres_uri(
            settings.POSTGRES_USER,
            settings.POSTGRES_PASSWORD,
            settings.POSTGRES_SERVER,
            settings.POSTGRES_PORT,
            database,
        ),
        **options,
    )


@pytest.fixture
def scratch() -> Iterator[tuple[str, Engine]]:
    name = f"{settings.POSTGRES_DB}_axis_{uuid.uuid4().hex[:8]}"
    maintenance = _engine(settings.POSTGRES_DB, isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = _engine(name)
    try:
        yield name, engine
    finally:
        engine.dispose()
        with maintenance.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        maintenance.dispose()


def _alembic(database: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=MIGRATIONS_PATH.parent,
        env={**os.environ, "POSTGRES_DB": database},
        capture_output=True,
        text=True,
    )


def _migrate(database: str, *args: str) -> None:
    result = _alembic(database, *args)
    assert result.returncode == 0, result.stderr


PROFILE = {
    "depth": {"t": [0, 1200, 3473], "v": [100, 3000, 0]},
    "temperature": {"t": [0, 1, 2], "v": [219, 219, 218]},
    "pressure": [{"gas_number": 1, "t": [0, 1200], "v": [2052, 2041]}],
    "events": [{"t": 0, "type": "gas_switch", "gas_number": 1}, {"t": 3500, "type": "bookmark"}],
}

SEED = """
INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at)
VALUES (1, 'Seed', 'seed', 'seed@example.com', false, false, gen_random_uuid(), now());
INSERT INTO dive (id, user_id, dive_number, start_time, utc_offset_minutes, duration, notes, uuid, created_at,
                  is_deleted, water_type, cns_start, cns_end, otu_start, otu_end, surface_pressure_bar)
VALUES (1, 1, 1, '2025-01-01 10:00:00+00', 120, 3000, '', gen_random_uuid(), now(), false, 'salt',
        3, 9, 10, 22, 1.012),
       (2, 1, 2, '2025-01-02 10:00:00+00', NULL, 3000, '', gen_random_uuid(), now(), false, NULL,
        5, 12, NULL, NULL, 1.009),
       (3, 1, 3, '2025-01-03 10:00:00+00', 0, 3000, '', gen_random_uuid(), now(), false, 'en13319',
        NULL, NULL, NULL, NULL, NULL),
       (4, 1, 4, '2025-01-04 10:00:00+00', 0, 3000, '', gen_random_uuid(), now(), false, 'en13319',
        NULL, NULL, NULL, NULL, NULL),
       (5, 1, 5, '2025-01-05 10:00:00+00', -300, 3000, '', gen_random_uuid(), now(), false, 'en13319',
        NULL, NULL, NULL, 7, NULL),
       (6, 1, 6, '2025-01-06 10:00:00+00', 0, 3000, '', gen_random_uuid(), now(), true, NULL,
        NULL, 4, NULL, NULL, NULL);
INSERT INTO dive_recording (id, dive_id, user_id, ordinal, device_brand, device_model, start_time,
                            utc_offset_minutes, uuid, created_at)
VALUES (10, 2, 1, 0, 'Suunto', NULL, '2025-01-02 10:00:00+00', NULL, gen_random_uuid(), now()),
       (11, 2, 1, 1, 'Shearwater', NULL, '2025-01-02 10:03:43+00', NULL, gen_random_uuid(), now()),
       (12, 3, 1, 0, NULL, 'Perdix', '2025-01-03 10:00:00+00', 0, gen_random_uuid(), now());
INSERT INTO dive_file (user_id, dive_id, recording_id, sha256, content_type, byte_size, original_filename,
                       parser_key, storage_key, uuid, created_at)
VALUES (1, 2, 10, repeat('a', 64), 'application/json', 10, 'a.json', 'suunto_json', 'dive-files/x',
        gen_random_uuid(), now());
INSERT INTO dive_profile (dive_id, recording_id, source_sha256, parser_key, extractor_version, duration,
                          depth_sample_count, data, uuid, created_at)
VALUES (2, 10, repeat('a', 64), 'suunto_json', 4, 3473, 3, CAST(:profile AS jsonb), gen_random_uuid(), now()),
       (3, 12, repeat('c', 64), 'merge', 4, 10, 2, CAST(:small AS jsonb), gen_random_uuid(), now());
SELECT setval('dive_recording_id_seq', 100);
"""

_READOUTS = "cns_start, cns_end, otu_start, otu_end, surface_pressure_bar"


def _seed(engine: Engine) -> None:
    small = {"depth": {"t": [0, 10], "v": [0, 500]}}
    with engine.begin() as connection:
        for statement in SEED.split(";\n"):
            if statement.strip():
                connection.execute(text(statement), {"profile": json.dumps(PROFILE), "small": json.dumps(small)})


def _snapshot(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    with engine.connect() as connection:
        return {
            "dives": [
                tuple(row)
                for row in connection.execute(text(f"SELECT id, water_type, {_READOUTS} FROM dive ORDER BY id"))
            ],
            "recordings": [
                tuple(row)
                for row in connection.execute(text("SELECT id, dive_id, ordinal FROM dive_recording ORDER BY id"))
            ],
            "profiles": [
                (row.recording_id, row.duration, row.data)
                for row in connection.execute(
                    text("SELECT recording_id, duration, data FROM dive_profile ORDER BY recording_id")
                )
            ],
        }


def _recordings(engine: Engine, dive_id: int) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    f"SELECT id, ordinal, salinity, device_brand, start_time, utc_offset_minutes, {_READOUTS} "
                    "FROM dive_recording WHERE dive_id = :dive ORDER BY ordinal"
                ),
                {"dive": dive_id},
            )
        )


class TestTheUpgrade:
    def test_the_readouts_and_the_setting_move_onto_the_primary_recording(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        _migrate(name, "upgrade", _BELOW)
        _seed(engine)

        _migrate(name, "upgrade", _REVISION)

        # A dive with readouts and no recording gets one of readouts alone, at the dive's start.
        [minted] = _recordings(engine, 1)
        assert (minted.ordinal, minted.device_brand, minted.salinity) == (0, None, None)
        assert (minted.cns_start, minted.cns_end, minted.otu_start, minted.otu_end) == (3, 9, 10, 22)
        assert minted.surface_pressure_bar == 1.012
        assert minted.utc_offset_minutes == 120
        # Onto the primary alone; a second computer's readouts are its own files' to give.
        primary, second = _recordings(engine, 2)
        assert (primary.cns_start, primary.cns_end, primary.surface_pressure_bar) == (5, 12, 1.009)
        assert (second.cns_end, second.surface_pressure_bar) == (None, None)
        # `en13319` onto the recording that has it, the minted one included; dropped with no
        # recording and no readout to carry it. The deleted dive's readouts move too.
        assert [row.salinity for row in _recordings(engine, 3)] == ["en13319"]
        assert _recordings(engine, 4) == []
        assert [(row.salinity, row.otu_end) for row in _recordings(engine, 5)] == [("en13319", 7)]
        assert [row.cns_end for row in _recordings(engine, 6)] == [4]
        with engine.connect() as connection:
            water = {row.id: row.water_type for row in connection.execute(text("SELECT id, water_type FROM dive"))}
            columns = connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'dive'")
            ).scalars()
            assert "cns_end" not in set(columns)
        assert water == {1: "salt", 2: None, 3: None, 4: None, 5: None, 6: None}

    def test_every_axis_entry_is_multiplied_and_stamped(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        _migrate(name, "upgrade", _BELOW)
        _seed(engine)

        _migrate(name, "upgrade", _REVISION)

        with engine.connect() as connection:
            rows = {
                row.recording_id: row
                for row in connection.execute(
                    text("SELECT recording_id, duration, extractor_version, data FROM dive_profile")
                )
            }
        assert rows[10].duration == 3_473_000
        assert rows[10].data == {
            "depth": {"t": [0, 1_200_000, 3_473_000], "v": [100, 3000, 0]},
            "temperature": {"t": [0, 1000, 2000], "v": [219, 219, 218]},
            "pressure": [{"gas_number": 1, "t": [0, 1_200_000], "v": [2052, 2041]}],
            "events": [{"t": 0, "type": "gas_switch", "gas_number": 1}, {"t": 3_500_000, "type": "bookmark"}],
        }
        assert (rows[12].duration, rows[12].data["depth"]["t"]) == (10_000, [0, 10_000])
        assert {row.extractor_version for row in rows.values()} == {5}


class TestTheDowngrade:
    def test_it_puts_the_data_back(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        _migrate(name, "upgrade", _BELOW)
        _seed(engine)
        before = _snapshot(engine)

        _migrate(name, "upgrade", _REVISION)
        _migrate(name, "downgrade", _BELOW)

        after = _snapshot(engine)
        # The one loss the upgrade logged: `en13319` on a dive with nowhere to carry it.
        expected_dives = [(4, None, *row[2:]) if row[0] == 4 else row for row in before["dives"]]
        assert after["dives"] == expected_dives
        # The recordings the upgrade minted are gone, and the others kept their places.
        assert after["recordings"] == before["recordings"]
        assert after["profiles"] == before["profiles"]
        with engine.connect() as connection:
            versions = set(connection.execute(text("SELECT extractor_version FROM dive_profile")).scalars())
        assert versions == {4}

    def test_it_refuses_an_axis_that_is_not_whole_seconds(self, scratch: tuple[str, Engine]) -> None:
        """Data written after the change can carry an offset below a second, which the old
        axis cannot hold - so the downgrade refuses rather than rounds, and rolls back whole."""
        name, engine = scratch
        _migrate(name, "upgrade", _BELOW)
        _seed(engine)
        _migrate(name, "upgrade", _REVISION)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE dive_profile SET data = jsonb_set(data, '{depth,t,0}', '160') WHERE recording_id = 10")
            )

        result = _alembic(name, "downgrade", _BELOW)

        assert result.returncode != 0
        assert "not a whole second" in result.stderr
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _REVISION


class TestTheRewrite:
    """The payload walk itself, without a database."""

    def test_every_time_in_the_payload_is_rescaled_and_nothing_else(self) -> None:
        module = _revision()

        moved = module.rescaled(PROFILE | {"future": {"x": [1]}}, lambda t: t * 1000)

        assert moved["depth"] == {"t": [0, 1_200_000, 3_473_000], "v": [100, 3000, 0]}
        assert moved["pressure"][0]["v"] == [2052, 2041]
        assert [event["t"] for event in moved["events"]] == [0, 3_500_000]
        assert moved["future"] == {"x": [1]}
