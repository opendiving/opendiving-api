"""Revision `6849ff025422` run for real, against a database of its own
(`tests/helpers/migrations.py`), and the boot-time step that finishes its work.

What is worth pinning: every dive file already stored occupies its upload's length, since none
of them is a frame; a rendition's length is left for the lifespan to read through the live
store, once; and the downgrade takes both columns away.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.app.core import setup
from src.app.core.config import postgres_uri, settings
from src.app.services import blob_store
from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

_REVISION = "6849ff025422"
_BELOW = "c47b308253a3"

RENDITION = b"RIFF\x00\x00\x00\x00WEBP a rendition stored before its length was"
RENDITION_KEY = f"user-avatars/ab/0190a000-0000-7000-8000-000000000000_{hashlib.sha256(RENDITION).hexdigest()}"

SEED = f"""
INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at)
VALUES (1, 'Seed', 'seed1', 'seed1@example.com', false, false, gen_random_uuid(), now());
INSERT INTO dive (id, user_id, dive_number, start_time, utc_offset_minutes, duration, notes, uuid, created_at,
                  is_deleted)
VALUES (1, 1, 1, '2025-01-01 10:00:00+00', 120, 3000, '', gen_random_uuid(), now(), false);
INSERT INTO dive_recording (id, dive_id, user_id, ordinal, uuid, created_at)
VALUES (1, 1, 1, 0, gen_random_uuid(), now());
INSERT INTO dive_file (id, user_id, recording_id, dive_id, sha256, content_type, byte_size, original_filename,
                       parser_key, storage_key, uuid, created_at)
VALUES (1, 1, 1, 1, '{"a" * 64}', 'application/json', 1825935, 'dive.json', 'suunto_json',
        'dive-files/aa/0190a000-0000-7000-8000-000000000001_{"a" * 64}', gen_random_uuid(), now()),
       (2, 1, 1, 1, '{"b" * 64}', 'application/octet-stream', 33134, 'dive.fit', 'fit',
        'dive-files/bb/0190a000-0000-7000-8000-000000000002_{"b" * 64}', gen_random_uuid(), now());
INSERT INTO user_picture (id, user_id, kind, rendition_storage_key, rendition_sha256, uuid, created_at)
VALUES (1, 1, 'avatar', '{RENDITION_KEY}', '{hashlib.sha256(RENDITION).hexdigest()}', gen_random_uuid(), now());
"""


@pytest.fixture
def scratch() -> Iterator[tuple[str, Engine]]:
    with scratch_database("storage_sizes") as database:
        yield database


def _upgraded(scratch: tuple[str, Engine]) -> tuple[str, Engine]:
    name, engine = scratch
    migrate(name, "upgrade", _BELOW)
    with engine.begin() as connection:
        connection.execute(text(SEED))
    migrate(name, "upgrade", _REVISION)
    return name, engine


def _rows(engine: Engine, sql: str) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql)))


class TestTheUpgrade:
    def test_every_stored_dive_file_occupies_its_upload_s_length(self, scratch: tuple[str, Engine]) -> None:
        _, engine = _upgraded(scratch)

        rows = _rows(engine, "SELECT byte_size, stored_byte_size FROM dive_file ORDER BY id")
        assert [tuple(row) for row in rows] == [(1825935, 1825935), (33134, 33134)]

    def test_a_rendition_is_left_for_the_lifespan_to_measure(self, scratch: tuple[str, Engine]) -> None:
        _, engine = _upgraded(scratch)

        assert _rows(engine, "SELECT rendition_byte_size FROM user_picture") == [(None,)]

    def test_the_outgoing_build_s_dive_file_insert_is_refused(self, scratch: tuple[str, Engine]) -> None:
        """The overlap the revision accepts: the previous build names no stored size, and its
        attach answers the 409 it gives any failed insert rather than storing a row the limit
        cannot count."""
        _, engine = _upgraded(scratch)

        with pytest.raises(IntegrityError, match="stored_byte_size"), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dive_file (user_id, recording_id, dive_id, sha256, content_type, byte_size, "
                    "original_filename, parser_key, storage_key, uuid, created_at) VALUES (1, 1, 1, :sha, "
                    "'application/json', 3, 'x.json', 'suunto_json', 'dive-files/cc/x', gen_random_uuid(), now())"
                ),
                {"sha": "c" * 64},
            )

    def test_the_downgrade_takes_both_columns_away(self, scratch: tuple[str, Engine]) -> None:
        name, engine = _upgraded(scratch)

        migrate(name, "downgrade", _BELOW)

        columns = _rows(
            engine,
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE column_name IN ('stored_byte_size', 'rendition_byte_size')",
        )
        assert columns == []


class TestTheLifespanMeasuresWhatTheRevisionCouldNot:
    @pytest.mark.asyncio
    async def test_a_rendition_is_measured_through_the_store_once(
        self, scratch: tuple[str, Engine], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Through the live store, whichever backend it is, which a frozen revision cannot
        read; and once, since the next boot finds nothing left unmeasured."""
        name, engine = _upgraded(scratch)
        monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
        await blob_store.put(RENDITION_KEY, RENDITION)
        app_engine = create_async_engine(
            settings.POSTGRES_ASYNC_PREFIX
            + postgres_uri(
                settings.POSTGRES_USER,
                settings.POSTGRES_PASSWORD,
                settings.POSTGRES_SERVER,
                settings.POSTGRES_PORT,
                name,
            )
        )
        reads: list[str] = []
        real_get = blob_store.get

        async def counting_get(key: str) -> bytes:
            reads.append(key)
            return await real_get(key)

        try:
            with patch.object(setup, "engine", app_engine), patch.object(setup.blob_store, "get", counting_get):
                await setup.measure_unsized_renditions()
                await setup.measure_unsized_renditions()
        finally:
            await app_engine.dispose()

        assert _rows(engine, "SELECT rendition_byte_size FROM user_picture") == [(len(RENDITION),)]
        assert reads == [RENDITION_KEY]

    @pytest.mark.asyncio
    async def test_a_rendition_whose_file_is_missing_stays_unmeasured_and_startup_goes_on(
        self, scratch: tuple[str, Engine], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        name, engine = _upgraded(scratch)
        monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
        app_engine = create_async_engine(
            settings.POSTGRES_ASYNC_PREFIX
            + postgres_uri(
                settings.POSTGRES_USER,
                settings.POSTGRES_PASSWORD,
                settings.POSTGRES_SERVER,
                settings.POSTGRES_PORT,
                name,
            )
        )
        try:
            with patch.object(setup, "engine", app_engine):
                await setup.measure_unsized_renditions()
        finally:
            await app_engine.dispose()

        assert _rows(engine, "SELECT rendition_byte_size FROM user_picture") == [(None,)]
        assert "1 picture rendition(s) are missing" in caplog.text
