"""Running one Alembic revision for real, against a database of its own.

The suite's database is at head, so a test of a revision's data moves takes a fresh
database at the revision below, seeds it in that schema, and upgrades and downgrades it
through the Alembic CLI - a subprocess, because `settings` is built once at import and the
database it names is what `migrations/env.py` connects to.

Migrating an empty database through every revision before it is most of what such a test
costs, so a module migrates one `template_database` and each test copies it, which
Postgres does in milliseconds. Such a module marks its tests with one `xdist_group`, so
under `--dist loadgroup` the template is built once rather than on every worker its tests
land on.
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
def _database(label: str, template: str | None = None) -> Iterator[str]:
    """A database named after the suite's own, dropped on the way out."""
    name = f"{settings.POSTGRES_DB}_{label}_{uuid.uuid4().hex[:8]}"
    copy = "" if template is None else f' TEMPLATE "{template}"'
    maintenance = engine_for(settings.POSTGRES_DB, isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"{copy}'))
    try:
        yield name
    finally:
        with maintenance.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        maintenance.dispose()


@contextmanager
def template_database(revision: str) -> Iterator[str]:
    """A database migrated to `revision`, for `scratch_database` to copy.

    It yields a name and no engine: Postgres refuses to copy a database anyone is connected to.
    """
    with _database(f"at_{revision}") as name:
        migrate(name, "upgrade", revision)
        yield name


@contextmanager
def scratch_database(label: str, template: str) -> Iterator[tuple[str, Engine]]:
    """A copy of `template` for one test, dropped after it."""
    with _database(label, template) as name:
        engine = engine_for(name)
        try:
            yield name, engine
        finally:
            engine.dispose()


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
