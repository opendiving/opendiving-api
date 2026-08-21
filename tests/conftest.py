import os
import tempfile
from collections.abc import AsyncGenerator, Callable, Generator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

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

import pytest
import pytest_asyncio
from faker import Faker
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import Session
from uuid6 import uuid7

from src.app.core.config import settings
from src.app.core.db.database import Base
from src.app.main import app

if TYPE_CHECKING:
    from src.app.models.user import User

DATABASE_URI = settings.POSTGRES_URI
DATABASE_PREFIX = settings.POSTGRES_SYNC_PREFIX

sync_engine = create_engine(DATABASE_PREFIX + DATABASE_URI)
local_session = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)


fake = Faker()


def unique_username() -> str:
    """A username no previous test run has realistically taken.

    `fake.user_name()` draws from a small vocabulary, and the tests that need a database
    write real rows and never clean them up (`create_user`, and the
    `docker-compose.test.yml` overlay, both of which point at the developer's own
    database). After a few hundred runs the namespace is crowded enough that
    `username`/`email` - both `unique=True` - start colliding, and the suite fails
    intermittently in *fixture setup*, which reads like a broken test rather than a
    broken fixture. `fake.unique` would not help: it only de-duplicates within one
    process, not against rows already in the table.

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
    """Create any missing tables once per session, for the database-backed modules.

    So those modules don't depend on the `api` service having already run its startup
    `alembic upgrade head` (`core.setup.apply_migrations`). Idempotent, and a no-op when
    nothing is listening, so it costs an unreachable connection attempt on a mocked-only
    run.

    Still `create_all` rather than a migration run, deliberately. The database these tests
    write to is whatever `POSTGRES_*` resolves to - the developer's own dev database by
    default - and `alembic upgrade head` against one that predates migrations dies on the
    first `CREATE TABLE`, turning "you have not stamped your dev database" into a suite
    that cannot start. `create_all` is also what makes this fixture idempotent and free.
    The cost is that a model change with no matching revision passes here; CI closes that
    gap by running `alembic upgrade head` from empty and then `alembic check` before the
    suite (.github/workflows/tests.yml), which is the run that has a disposable database
    to do it in.

    This is now the only definition - the seven modules that had grown their own
    module-scoped copy of it and of `db_available` all use these. Keep it that way: a
    module-level fixture of the same name *shadows* this one, so a re-introduced copy
    silently stops this from running for that module rather than conflicting with it. See
    *"The Postgres test fixtures are shared, and a local copy silently wins"* in
    `DECISIONS.md`.
    """
    if db_available():
        Base.metadata.create_all(sync_engine)


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
