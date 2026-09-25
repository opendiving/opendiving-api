"""Running one Alembic revision for real, against a database of its own.

The suite's database is at head, so a test of a revision's data moves migrates a fresh
database to the revision below, seeds it in that schema, and upgrades and downgrades it
through the Alembic CLI - a subprocess, because `settings` is built once at import and the
database it names is what `migrations/env.py` connects to.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text

from src.app.core.config import postgres_uri, settings
from src.app.core.db.migrations import MIGRATIONS_PATH


def engine_for(database: str, **options: Any) -> Engine:
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


@contextmanager
def scratch_database(label: str) -> Iterator[tuple[str, Engine]]:
    """A database created for one test and dropped after it, named after the suite's own."""
    name = f"{settings.POSTGRES_DB}_{label}_{uuid.uuid4().hex[:8]}"
    maintenance = engine_for(settings.POSTGRES_DB, isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = engine_for(name)
    try:
        yield name, engine
    finally:
        engine.dispose()
        with maintenance.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        maintenance.dispose()


def alembic(database: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=MIGRATIONS_PATH.parent,
        env={**os.environ, "POSTGRES_DB": database},
        capture_output=True,
        text=True,
    )


def migrate(database: str, *args: str) -> None:
    result = alembic(database, *args)
    assert result.returncode == 0, result.stderr
