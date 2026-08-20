import asyncio
import importlib
import pkgutil
from logging.config import fileConfig

from alembic import context
from alembic.runtime.environment import NameFilterParentNames, NameFilterType
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.core.db.database import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# `POSTGRES_URI` rather than interpolating the parts by hand: it percent-encodes the
# credentials (see `core.config.postgres_uri`), so a password containing `@` or `/` builds
# the DSN the operator meant rather than one naming a host nobody configured.
config.set_main_option("sqlalchemy.url", f"{settings.POSTGRES_ASYNC_PREFIX}{settings.POSTGRES_URI}")

# Only when driven by `alembic.ini` - the CLI. The app runs migrations on startup through a
# programmatic `Config` with no file (see `core.db.migrations`), and `fileConfig` would
# reset the root logger that `configure_logging` has already set from `LOG_LEVEL`.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def import_models(package_name: str) -> None:
    package = importlib.import_module(package_name)
    for _, module_name, _ in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(module_name)


import_models("app.models")

# `token_blacklist` is the one table declared outside `app.models` - it came with the
# upstream boilerplate and lives under `core/db/` next to the CRUD wrapper that uses it.
# Nothing else in this file's import graph reaches it, and a table missing from
# `target_metadata` is not merely skipped: autogenerate sees it in the database, doesn't
# see it in the models, and writes `op.drop_table("token_blacklist")` into the revision.
# `tests/test_migrations.py` re-checks this coverage in a subprocess, since inside the test
# process the app has already imported everything.
importlib.import_module("app.core.db.token_blacklist")

target_metadata = Base.metadata


def include_name(name: str | None, type_: NameFilterType, parent_names: NameFilterParentNames) -> bool:
    """Keep the admin panel's tables out of autogenerate's comparison.

    CRUDAdmin owns `admin_user`, `admin_session`, `admin_event_log` and `admin_audit_log`,
    declares them on its own `DeclarativeBase`, and creates them itself
    (`scripts.initialize_admin`). They are only visible here when `CRUD_ADMIN_DB_URL` points
    at the app's own Postgres, which is what the deploy bundle does - and then they look to
    autogenerate exactly like tables someone deleted the models for, so a revision generated
    on such a machine would carry four `drop_table` calls. No table of this app's own starts
    with `admin_`.
    """
    if type_ == "table" and name is not None and name.startswith("admin_"):
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, though an Engine is acceptable here as well.  By
    skipping the Engine creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, include_name=include_name)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine and associate a connection with the context."""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
