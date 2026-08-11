from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from faker import Faker
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import Session
from uuid6 import uuid7

from src.app.core.config import settings
from src.app.main import app

DATABASE_URI = settings.POSTGRES_URI
DATABASE_PREFIX = settings.POSTGRES_SYNC_PREFIX

sync_engine = create_engine(DATABASE_PREFIX + DATABASE_URI)
local_session = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)


fake = Faker()


def unique_username() -> str:
    """A username no previous test run can have taken.

    `fake.user_name()` draws from a small vocabulary, and the tests that need a database
    write real rows and never clean them up (`create_user`, and the
    `docker-compose.test.yml` overlay, both of which point at the developer's own
    database). After a few hundred runs the namespace is crowded enough that
    `username`/`email` - both `unique=True` - start colliding, and the suite fails
    intermittently in *fixture setup*, which reads like a broken test rather than a
    broken fixture. `fake.unique` would not help: it only de-duplicates within one
    process, not against rows already in the table.

    Derived from uuid7, so it is unique across runs and machines rather than merely
    unlikely to repeat. Shaped to satisfy the app's own rule for usernames
    (`^[a-z0-9]+$`, 2-20 characters - see `schemas/user.py`), which the ORM does not
    enforce but which a fixture has no business violating.
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
        profile_image_url=fake.image_url(),
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
