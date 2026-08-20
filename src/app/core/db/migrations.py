"""Running Alembic from inside the application process.

The API applies `alembic upgrade head` on startup (`core.setup.apply_migrations`), so an
upgrade for a self-hosted instance is `docker compose pull && docker compose up -d` and
nothing else. That is the pattern Immich and Umami ship, and it is what makes a schema
change something other than an instance-killer for people who never read this repo.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

# `src/migrations` sits next to `src/app`, and the image copies both to `/code`, so the
# same relative hop finds it in a checkout and in a container. Resolved from `__file__`
# rather than from the working directory because `alembic.ini`'s own `script_location` is
# CWD-relative, and the API's CWD is not something this code should depend on.
MIGRATIONS_PATH = Path(__file__).resolve().parents[3] / "migrations"


def alembic_config() -> Config:
    """An Alembic config equivalent to `src/alembic.ini`, minus its logging section.

    Deliberately built in code instead of read from the file: `alembic.ini` carries a
    `[loggers]` block that `env.py` feeds to `fileConfig`, which *resets* the root logger.
    Running it inside the API process would silently undo `configure_logging(LOG_LEVEL)` -
    the app would come up logging at the ini's `WARN` no matter what `LOG_LEVEL` says.
    `env.py` skips `fileConfig` when there is no file, which is the only behavioural
    difference between the two. The database URL is not one: `env.py` imports it from
    `core/db/database.py` and connects with it directly, so neither this config nor
    `alembic.ini` carries a `sqlalchemy.url` - see the comment on that import for why a
    credential must not go through Alembic's `Config` at all.

    The CLI (`alembic upgrade head`, `alembic check`, `alembic stamp head`) still goes
    through `alembic.ini` as normal, where reconfiguring logging is the right behaviour.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    # `env.py` imports `app.core.config`, so the directory holding the `app` package has to
    # be importable. `alembic.ini` gets this from `prepend_sys_path = .` plus a working
    # directory the operator is told to be in; nothing guarantees either here, and the
    # symptom of getting it wrong is `ModuleNotFoundError: No module named 'app'` thrown
    # out of the lifespan.
    config.set_main_option("prepend_sys_path", str(MIGRATIONS_PATH.parent))
    # How Alembic splits the path options above. Without it every call warns, and the
    # legacy fallback splits on spaces and commas as well as `os.pathsep`.
    config.set_main_option("path_separator", "os")
    return config


def upgrade_to_head() -> None:
    """Apply every revision the database is behind on.

    Synchronous, and called through `asyncio.to_thread`: `env.py` runs its own
    `asyncio.run()` (it drives an async engine), which cannot be nested inside the running
    event loop of the API process.
    """
    command.upgrade(alembic_config(), "head")
