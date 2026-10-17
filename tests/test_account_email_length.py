"""An address `EmailStr` admits is one the account columns take: the sign-in request writes it
to real Postgres, and an address over `EMAIL_MAX_LENGTH` is a 422 before anything is written.
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.core.config import settings
from src.app.core.schemas import EMAIL_MAX_LENGTH
from src.app.core.setup import create_application
from src.app.models.auth_audit_event import AuthAuditEvent
from src.app.models.authentication_request import AuthenticationRequest
from tests.conftest import db_available, unique_username

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

PATH = "/api/v1/auth/email/request"


def _address(length: int) -> str:
    """A unique, valid address of exactly `length` characters - long ones need a long domain,
    since a local part stops at 64."""
    local = unique_username()
    labels, pad = divmod(length - len(local) - len("@example.com"), 50)
    return f"{local}{'x' * pad}@{('a' * 49 + '.') * labels}example.com"


@pytest.fixture(scope="module")
def client() -> Generator[TestClient]:
    app = create_application(router=router, settings=settings, apply_migrations_on_start=False)
    with (
        TestClient(app) as test_client,
        patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
        patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
    ):
        yield test_client


@pytest.mark.parametrize("length", [60, EMAIL_MAX_LENGTH])
def test_a_long_address_is_stored(client: TestClient, db: Session, length: int) -> None:
    email = _address(length)
    assert len(email) == length

    response = client.post(PATH, json={"email": email})

    assert response.status_code == 200, response.text
    assert db.scalar(select(AuthenticationRequest.email).where(AuthenticationRequest.email == email)) == email
    assert db.scalar(select(AuthAuditEvent.email).where(AuthAuditEvent.email == email)) == email


def test_an_address_over_the_limit_is_a_422(client: TestClient, db: Session) -> None:
    email = _address(EMAIL_MAX_LENGTH + 1)

    response = client.post(PATH, json={"email": email})

    assert response.status_code == 422, response.text
    assert db.scalar(select(AuthenticationRequest.id).where(AuthenticationRequest.email == email)) is None
