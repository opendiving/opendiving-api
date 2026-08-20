"""Unit tests for the Alembic setup that the API applies on startup.

Nothing here needs a database. The one test that would - "does `upgrade head` actually
build the right schema?" - is CI's job (`.github/workflows/tests.yml` runs it against an
empty Postgres and then `alembic check`s the result), because it needs a database it is
allowed to create tables in and the suite's is the developer's own.
"""

import contextlib
import io
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from src.app.core import setup
from src.app.core.db.database import Base
from src.app.core.db.migrations import MIGRATIONS_PATH, alembic_config

# Registers every model on `Base.metadata`, including `token_blacklist`, which is declared
# under `core/db/` rather than in `app.models` and is the reason `migrations/env.py` needs
# an import of its own. `conftest.py` already does this, but the coverage test below is
# only meaningful if this module states the dependency itself.
from src.app import main  # noqa: F401  isort:skip


def _offline_upgrade_sql() -> str:
    """The DDL `alembic upgrade head` would emit, against no database at all.

    Alembic's offline mode (`--sql`) runs every revision from base through `head` with the
    operations rendered as text instead of executed, so this is the migrations' own account
    of the schema they build - unlike `Base.metadata`, which is the models' account of it.
    Comparing the two is the whole point of the coverage test.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.upgrade(alembic_config(), "head", sql=True)
    return buffer.getvalue()


class TestAlembicConfig:
    def test_script_location_is_the_shipped_migrations_directory(self):
        """Resolved from `__file__`, so it has to survive both layouts it runs in: `src/`
        in a checkout and `/code/` in the image.
        """
        assert (MIGRATIONS_PATH / "env.py").is_file()
        assert (MIGRATIONS_PATH / "versions").is_dir()
        assert alembic_config().get_main_option("script_location") == str(MIGRATIONS_PATH)

    def test_no_ini_file_is_read(self):
        """`alembic.ini` carries a `[loggers]` block, and `env.py` hands it to `fileConfig`
        whenever there is a file - which resets the root logger. In-process that would
        silently undo `configure_logging(LOG_LEVEL)` and the API would log at the ini's
        `WARN` regardless of what the operator set.
        """
        assert alembic_config().config_file_name is None


class TestRevisionHistory:
    def test_there_is_exactly_one_head(self):
        """Two heads mean two branches merged without an `alembic merge`, and
        `upgrade head` then fails at startup - on every instance at once, since migrations
        run on boot.
        """
        assert len(ScriptDirectory.from_config(alembic_config()).get_heads()) == 1

    def test_there_is_exactly_one_base(self):
        """One root revision: the baseline. A second base is a revision written without a
        `down_revision`, which silently detaches everything below it.
        """
        assert len(ScriptDirectory.from_config(alembic_config()).get_bases()) == 1


class TestMigrationsCoverEveryModel:
    def test_every_table_the_models_declare_is_created_by_a_revision(self):
        """The failure this catches is a model added without a revision - and the nastier
        one, a model in a module `migrations/env.py` never imports, which autogenerate
        cannot see and so writes nothing for. Both look completely healthy locally, because
        the test suite builds its schema from `Base.metadata` directly
        (`conftest._ensure_tables`) and never consults the revisions at all.
        """
        created = set(re.findall(r'CREATE TABLE (?:IF NOT EXISTS )?"?([a-z_]+)"?', _offline_upgrade_sql()))

        assert set(Base.metadata.tables) - created == set()

    def test_alembics_own_bookkeeping_table_is_among_them(self):
        """Guards the regex above rather than Alembic: `alembic_version` is the one table
        the migrations create that no model declares, so its absence here means the pattern
        stopped matching and the assertion above went vacuous.
        """
        assert "alembic_version" in _offline_upgrade_sql()


class TestCredentialsWithReservedCharacters:
    #: Every class `core.config.postgres_uri` percent-encodes, in one password: `@` and `:`
    #: and `/` break DSN parsing, and a space and `!` are what a generator emits.
    AWKWARD_PASSWORD = "p@ss w:rd/!"

    def test_a_percent_encoded_password_does_not_break_the_migration_run(self):
        """Regression: the DSN used to be written into the Alembic config as
        `sqlalchemy.url`, and Alembic keeps main options in a `ConfigParser` with
        `BasicInterpolation` - which rejects a bare `%` when the option is *set*. Since
        `POSTGRES_URI` percent-encodes the credentials, every password outside
        `[A-Za-z0-9_.~-]` raised `ValueError: invalid interpolation syntax` out of the
        lifespan before any connection was attempted, i.e. the API would not boot.

        A subprocess because `settings` is built once at import and this has to be the
        value `migrations/env.py` reads. No database: `--sql` renders the DDL instead of
        running it, and the failure this guards happened well before any connection.
        """
        script = (
            "import contextlib, io, sys;"
            "sys.path.insert(0, 'src');"
            "from alembic import command;"
            "from app.core.db.migrations import alembic_config;"
            "buf = io.StringIO();"
            "contextlib.redirect_stdout(buf).__enter__();"
            'command.upgrade(alembic_config(), "head", sql=True)'
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "POSTGRES_PASSWORD": self.AWKWARD_PASSWORD},
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr


class TestApplyMigrationsOnStartup:
    """`core.setup.apply_migrations` - the lifespan hook, and the `MIGRATE_ON_START` gate."""

    @staticmethod
    def _fake_engine() -> tuple[MagicMock, AsyncMock]:
        """An `engine` whose `begin()` yields a connection, mimicking `AsyncEngine`."""
        conn = AsyncMock()
        conn.dialect.name = "postgresql"

        begin = MagicMock()
        begin.__aenter__ = AsyncMock(return_value=conn)
        begin.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.begin.return_value = begin
        return engine, conn

    @pytest.mark.asyncio
    async def test_upgrades_under_an_advisory_lock(self):
        engine, conn = self._fake_engine()

        with (
            patch.object(setup, "engine", engine),
            patch.object(setup, "upgrade_to_head") as upgrade,
            patch.object(setup, "settings", MagicMock(MIGRATE_ON_START=True)),
        ):
            await setup.apply_migrations()

        upgrade.assert_called_once_with()

        # The lock is what keeps four gunicorn workers from running the same revision
        # simultaneously, and it has to be taken *before* the upgrade rather than
        # alongside it.
        conn.execute.assert_awaited_once()
        statement = str(conn.execute.await_args.args[0])
        assert "pg_advisory_xact_lock" in statement
        assert conn.execute.await_args.args[1] == {"key": setup._SCHEMA_BOOTSTRAP_LOCK_KEY}

    @pytest.mark.asyncio
    async def test_migrate_on_start_false_touches_nothing(self):
        """The Miniflux-style opt-out. Not "create the tables some other way" - the app
        boots against whatever schema it finds, which is the operator's problem by then.
        """
        engine, _ = self._fake_engine()

        with (
            patch.object(setup, "engine", engine),
            patch.object(setup, "upgrade_to_head") as upgrade,
            patch.object(setup, "settings", MagicMock(MIGRATE_ON_START=False)),
        ):
            await setup.apply_migrations()

        upgrade.assert_not_called()
        engine.begin.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_postgres_skips_the_lock_but_still_upgrades(self):
        """The advisory lock is Postgres-only, and the guard around it predates
        migrations. The upgrade itself must not be conditional on it.
        """
        engine, conn = self._fake_engine()
        conn.dialect.name = "sqlite"

        with (
            patch.object(setup, "engine", engine),
            patch.object(setup, "upgrade_to_head") as upgrade,
            patch.object(setup, "settings", MagicMock(MIGRATE_ON_START=True)),
        ):
            await setup.apply_migrations()

        conn.execute.assert_not_awaited()
        upgrade.assert_called_once_with()
