import os
import tempfile
from collections.abc import AsyncGenerator, Callable, Generator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

from starlette.config import Config

# **Before any `src.app` import below.** `settings` is built at import time, and the app's
# lifespan - which `TestClient` enters for real, see the `client` fixture - creates and
# probes `FILE_STORAGE_DIR` and hard-fails if it cannot write there. Its default is
# `/data/files`, a path that exists inside the container and nowhere else, so without this
# every client test dies in startup on a developer's laptop and on the CI runner alike.
# `.github/workflows/tests.yml` sets the same variable in its `env:` block for the steps
# that run outside pytest (`alembic upgrade head`).
#
# One directory for the whole session, left behind for the OS to reap: the suite writes
# nothing into it (every service test mocks the session or the store), and a fixture that
# tore it down would have to outlive the session-scoped `client`.
os.environ.setdefault("FILE_STORAGE_DIR", tempfile.mkdtemp(prefix="opendiving-test-files-"))

# **Before any `src.app` import too**, and for a sharper reason: this is the only moment at
# which the suite can be pointed somewhere other than the developer's dev database.
# `settings.POSTGRES_URI` is assembled when `core.config` is imported and `core/db/
# database.py` builds the app's engine from it, so a redirection after that reaches
# neither.
#
# The suite gets its own database - the configured name with `_test` appended - because the
# documented way of running it (`POSTGRES_SERVER=localhost`, per CONTRIBUTING.md) otherwise
# resolves to the very database `docker compose up` serves from, and the fixtures then
# write into it. That was not merely untidy: the suite's schema bootstrap builds tables
# without advancing `alembic_version`, so a suite run against the dev database left it with
# tables a pending revision was about to create, and the next `docker compose restart api`
# died in its startup `alembic upgrade head` on `DuplicateTableError`. See *"The suite has
# its own database, and builds it with the migrations"* in DECISIONS.md.
#
# Appended rather than named outright so the two can never coincide, whatever the operator
# called theirs: `f"{name}_test"` is a different database than `name` by construction, which
# is the whole property being bought here.
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", ".env")
# `Config` warns about an env file that isn't there, and in a worktree - where `src/.env` is
# gitignored and so never followed the `git worktree add` - there isn't one. `core.config`
# already emits that warning for the same path; a second copy of it says nothing new.
_env = Config(_ENV_FILE if os.path.isfile(_ENV_FILE) else None)
CONFIGURED_DATABASE = _env("POSTGRES_DB", default="postgres")
TEST_DATABASE = f"{CONFIGURED_DATABASE}_test"
os.environ["POSTGRES_DB"] = TEST_DATABASE

import pytest
import pytest_asyncio
from alembic.util.exc import CommandError
from faker import Faker
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import Session
from uuid6 import uuid7

from src.app.core.config import postgres_uri, settings
from src.app.core.db.migrations import upgrade_to_head
from src.app.main import app

if TYPE_CHECKING:
    from src.app.models.user import User

DATABASE_URI = settings.POSTGRES_URI
DATABASE_PREFIX = settings.POSTGRES_SYNC_PREFIX

sync_engine = create_engine(DATABASE_PREFIX + DATABASE_URI)
local_session = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)


#: Postgres's own code for `duplicate_database`, which is what a second pytest process
#: racing this one on a cold machine gets instead of the `CREATE DATABASE` it asked for.
_DUPLICATE_DATABASE = "42P04"


def _create_test_database() -> None:
    """Create the suite's database if it isn't there yet, at import time.

    Import time rather than in a fixture because `db_available()` is called during
    collection, by every module's `skipif` - a database created later than that would leave
    the first run of a fresh checkout skipping everything and the second run green, which is
    a worse trap than the one this whole arrangement exists to remove.

    The connection is made to the *configured* database, the one guaranteed to exist and to
    accept these credentials wherever the app itself runs. `CREATE DATABASE` cannot run
    inside a transaction, hence `AUTOCOMMIT`. Nothing is written to the database connected
    to - one `pg_database` lookup and one `CREATE DATABASE` naming a different one - which
    is what keeps the invariant honest rather than merely mostly-honest.

    Silent on `OperationalError` because that is the cold-checkout case the suite is built
    around: nothing listening, or credentials that don't fit (a worktree without `src/.env`
    falls back to `postgres`/`postgres`). The DB-backed modules then skip themselves exactly
    as before. A `CREATE DATABASE` refused for any other reason - no privilege, most likely
    - is *not* silenced: that one deserves to be read rather than to become a skip line.
    """
    maintenance_engine = create_engine(
        DATABASE_PREFIX
        + postgres_uri(
            settings.POSTGRES_USER,
            settings.POSTGRES_PASSWORD,
            settings.POSTGRES_SERVER,
            settings.POSTGRES_PORT,
            CONFIGURED_DATABASE,
        ),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with maintenance_engine.connect() as connection:
            already_there = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DATABASE}
            )
            if already_there is None:
                quoted = maintenance_engine.dialect.identifier_preparer.quote(TEST_DATABASE)
                connection.execute(text(f"CREATE DATABASE {quoted}"))
    except OperationalError:
        pass
    except ProgrammingError as exc:
        if getattr(exc.orig, "pgcode", None) != _DUPLICATE_DATABASE:
            raise
    finally:
        maintenance_engine.dispose()


_create_test_database()


fake = Faker()


def unique_username() -> str:
    """A username no previous test run has realistically taken.

    `fake.user_name()` draws from a small vocabulary, and the tests that need a database
    write real rows and never clean them up (`create_user`, and the
    `docker-compose.test.yml` overlay). Those rows land in the suite's own database now
    rather than the developer's dev one, but they still accumulate there across runs: after
    a few hundred the namespace is crowded enough that `username`/`email` - both
    `unique=True` - start colliding, and the suite fails intermittently in *fixture setup*,
    which reads like a broken test rather than a broken fixture. `fake.unique` would not
    help: it only de-duplicates within one process, not against rows already in the table.

    The suffix is uuid7's low 48 bits, which `uuid6` fills from `secrets` - so this is 48
    bits of CSPRNG entropy per name, drawn independently of any other process or machine.
    Still a probabilistic argument rather than a guarantee, but a different order of one:
    the birthday bound puts an even chance of collision somewhere past sixteen million
    names, against a few hundred for `fake.user_name()`. Note it is *only* the random tail
    - `hex[-12:]` slices below uuid7's timestamp, so nothing here is time-ordered, and two
    names generated in the same millisecond are as independent as any other two.

    Shaped to satisfy the app's own rule for usernames (`^[a-z0-9]+$`, 2-20 characters -
    see `schemas/user.py`), which the ORM does not enforce but which a fixture has no
    business violating. That 20-character cap is why the suffix is 12 hex characters rather
    than the whole uuid.
    """
    return f"t{uuid7().hex[-12:]}"


def unique_email() -> str:
    """Matching address for `unique_username`.

    `example.com` rather than something under `.test`: both are reserved by RFC 2606 and
    neither can reach a real inbox, but `email-validator` (which backs Pydantic's
    `EmailStr`) rejects `.test` outright as a special-use name - so an address there
    fails validation in any test that round-trips one through a schema. Comfortably
    inside the column's 50 characters either way.
    """
    return f"{unique_username()}@example.com"


@pytest.fixture(scope="session")
def client() -> Generator[TestClient, Any]:
    with TestClient(app) as _client:
        yield _client
    app.dependency_overrides = {}
    sync_engine.dispose()


@pytest.fixture
def db() -> Generator[Session, Any]:
    session = local_session()
    yield session
    session.close()


def db_available() -> bool:
    """Whether the Postgres-backed tests can run at all.

    Most of the suite mocks the session and a cold checkout has nothing listening, so every
    module that needs a real database guards on `not db_available()` - as a module-level
    `pytestmark` where the whole module is database-backed, and per class where it also
    holds classes that are not. Note the skip is silent: see CONTRIBUTING.md for why a run
    on the host needs `POSTGRES_SERVER=localhost` before these execute at all.
    """
    try:
        with sync_engine.connect():
            return True
    except OperationalError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables() -> None:
    """Bring the suite's own database up to `head` once per session.

    So the database-backed modules don't depend on the `api` service having already run its
    startup `alembic upgrade head` (`core.setup.apply_migrations`). A no-op when nothing is
    listening, so it costs an unreachable connection attempt on a mocked-only run, and a
    single `alembic_version` read on every run after the first.

    `alembic upgrade head` rather than the `Base.metadata.create_all` this used to be. That
    call was chosen while these tests ran against the developer's *dev* database, where a
    migration run would have died on the tables an unstamped database already had - and it
    was also the mechanism by which a suite run corrupted that database, since it creates
    tables without advancing `alembic_version` and left the `api` container's own
    `upgrade head` to die on the first `CREATE TABLE` at its next restart. The database is
    the suite's own now (see the top of this module), created empty, so the migrations can
    build it: they are what ships, they add columns to existing tables where `create_all`
    only ever added whole missing ones, and they leave `alembic_version` telling the truth.

    Two consequences worth knowing. A model change with no matching revision now fails these
    tests locally rather than passing and waiting for CI's `alembic check` - which is the
    better end of the trade, but it is a change. And a test database left at a revision that
    is not in this branch's history - hop onto a branch, run the suite, hop off - cannot be
    upgraded from; the `CommandError` below is what says so, since the raw Alembic message
    names a revision hash and nothing else.
    """
    if not db_available():
        return
    try:
        upgrade_to_head()
    except CommandError as exc:
        raise RuntimeError(
            f"Could not migrate the test database `{TEST_DATABASE}` to head: {exc}. It is the suite's "
            f"own and holds nothing worth keeping - drop it and let the next run rebuild it: "
            f"psql -c 'DROP DATABASE \"{TEST_DATABASE}\"'"
        ) from exc


@pytest_asyncio.fixture
async def async_db() -> AsyncGenerator[AsyncSession]:
    """An `AsyncSession` on its own engine, for testing async crud against real Postgres.

    Separate from `db`, which is the sync session the rest of the suite seeds rows with -
    a test typically wants both: `db` to arrange, `async_db` to exercise the code under
    test.

    Engine per test rather than per session, and disposed at teardown, because
    pytest-asyncio gives each test its own event loop and a pooled asyncpg connection is
    bound to the loop that opened it - a session-scoped engine hands the second test a
    connection from the first test's dead loop. Wasteful, and not optional. Every module
    that predates this fixture had worked that out separately; see *"The Postgres test
    fixtures are shared, and a local copy silently wins"* in `DECISIONS.md`.
    """
    engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def diver(db: Session) -> User:
    # Imported in the body, not at module scope: `tests.helpers.generators` imports
    # `fake`/`unique_username`/`unique_email` from this module, so a top-level import here
    # would be a cycle that fails on a half-initialized `conftest`.
    from tests.helpers.generators import create_user

    return create_user(db)


@pytest.fixture
def other_diver(db: Session) -> User:
    """A second logbook in the same tables. `user_id` is the only thing keeping a bulk
    `UPDATE` off another diver's dives, and a suite with one diver in it cannot notice
    when that condition stops working."""
    from tests.helpers.generators import create_user

    return create_user(db)


def override_dependency(dependency: Callable[..., Any], mocked_response: Any) -> None:
    app.dependency_overrides[dependency] = lambda: mocked_response


@pytest.fixture
def mock_db():
    """Mock database session for unit tests."""
    return Mock(spec=AsyncSession)


@pytest.fixture
def mock_redis():
    """Mock Redis connection for unit tests."""
    mock_redis = Mock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.delete = AsyncMock(return_value=True)
    return mock_redis


@pytest.fixture
def sample_user_data():
    """Generate sample user data for tests."""
    return {
        "name": fake.name(),
        "username": unique_username(),
        "email": unique_email(),
    }


@pytest.fixture
def sample_user_read():
    """Generate a sample UserRead object (the public, uuid-keyed shape)."""
    from src.app.schemas.user import UserRead

    return UserRead(
        uuid=uuid7(),
        name=fake.name(),
        username=unique_username(),
        email=unique_email(),
    )


@pytest.fixture
def current_user_dict():
    """Mock current user from auth dependency."""
    return {
        "id": 1,
        "uuid": uuid7(),
        "username": unique_username(),
        "email": unique_email(),
        "name": fake.name(),
        "is_superuser": False,
    }
