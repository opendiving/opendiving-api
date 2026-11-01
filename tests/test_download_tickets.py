"""Download tickets: the token, `get_downloading_user` over real sessions, and the mint route.

What a ticket has to be is narrow, and each case below pins one edge of it: it opens the one
path it names, as the account and session that minted it, while that session is live - and an
access token cannot pass for one, nor one for an access token.
"""

import uuid as uuid_pkg
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException, Request, Response
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api import router
from src.app.api.dependencies import current_session_uuid, get_current_user, get_downloading_user, refuse_api_keys
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.exceptions.http_exceptions import UnauthorizedException
from src.app.core.security import (
    ALGORITHM,
    SECRET_KEY,
    TokenType,
    create_download_token,
    token_session_id,
    verify_download_token,
    verify_token,
)
from src.app.core.setup import create_application
from src.app.core.utils.request_context import RequestContext
from src.app.crud.crud_user_sessions import revoke_session
from src.app.models.user import User
from src.app.services.auth_service import issue_tokens
from tests.conftest import db_available

CONTEXT = RequestContext(ip="203.0.113.7", user_agent="Mozilla/5.0 (X11; Linux x86_64) TestAgent/1.0")
ARCHIVE = "/export/archive"

needs_a_database = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _request(path: str = f"/api/v1{ARCHIVE}") -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})


class TestTheToken:
    def test_it_attests_who_asked_and_from_which_session(self) -> None:
        user_uuid, session_uuid = uuid_pkg.uuid4(), uuid_pkg.uuid4()

        data = verify_download_token(
            create_download_token(user_uuid=user_uuid, session_uuid=session_uuid, path=ARCHIVE), path=ARCHIVE
        )

        assert data is not None
        assert (data.user_uuid, data.session_uuid) == (user_uuid, session_uuid)

    def test_it_opens_no_other_path(self) -> None:
        ticket = create_download_token(user_uuid=uuid_pkg.uuid4(), session_uuid=uuid_pkg.uuid4(), path=ARCHIVE)

        assert verify_download_token(ticket, path="/export/csv") is None

    def test_two_tickets_minted_together_are_counted_apart(self) -> None:
        """The export limit counts a ticket by its `jti`, so two clicks are two exports."""
        user_uuid, session_uuid = uuid_pkg.uuid4(), uuid_pkg.uuid4()
        first = verify_download_token(
            create_download_token(user_uuid=user_uuid, session_uuid=session_uuid, path=ARCHIVE), path=ARCHIVE
        )
        second = verify_download_token(
            create_download_token(user_uuid=user_uuid, session_uuid=session_uuid, path=ARCHIVE), path=ARCHIVE
        )

        assert first is not None and second is not None
        assert first.jti != second.jti

    def test_an_expired_ticket_opens_nothing(self) -> None:
        payload = {
            "sub": str(uuid_pkg.uuid4()),
            "sid": str(uuid_pkg.uuid4()),
            "path": ARCHIVE,
            "jti": "x",
            "exp": datetime.now(UTC) - timedelta(seconds=1),
            "token_type": TokenType.DOWNLOAD,
        }

        token = jwt.encode(payload, SECRET_KEY.get_secret_value(), ALGORITHM)

        assert verify_download_token(token, path=ARCHIVE) is None

    def test_an_access_token_is_not_a_ticket(self) -> None:
        """Same key, same `sub` and `sid` - the type is all that tells them apart."""
        payload = {
            "sub": str(uuid_pkg.uuid4()),
            "sid": str(uuid_pkg.uuid4()),
            "path": ARCHIVE,
            "jti": "x",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "token_type": TokenType.ACCESS,
        }

        token = jwt.encode(payload, SECRET_KEY.get_secret_value(), ALGORITHM)

        assert verify_download_token(token, path=ARCHIVE) is None

    @needs_a_database
    @pytest.mark.asyncio
    async def test_a_ticket_is_not_an_access_token(self, async_db: AsyncSession) -> None:
        ticket = create_download_token(user_uuid=uuid_pkg.uuid4(), session_uuid=uuid_pkg.uuid4(), path=ARCHIVE)

        assert await verify_token(ticket, TokenType.ACCESS, async_db) is None


@needs_a_database
class TestGetDownloadingUser:
    """Against real Postgres, because what is pinned is the session's liveness - a `WHERE`
    clause a mocked session evaluates none of - and with the tokens `issue_tokens` mints."""

    @staticmethod
    async def _ticket(async_db: AsyncSession, diver: User, path: str = ARCHIVE) -> tuple[str, uuid_pkg.UUID]:
        tokens = await issue_tokens(Response(), diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)
        session_uuid = token_session_id(tokens.access_token)
        assert session_uuid is not None
        return create_download_token(user_uuid=diver.uuid, session_uuid=session_uuid, path=path), session_uuid

    @pytest.mark.asyncio
    async def test_a_ticket_authenticates_its_path_as_its_diver(self, async_db: AsyncSession, diver: User) -> None:
        ticket, _ = await self._ticket(async_db, diver)
        request = _request()

        user = await get_downloading_user(request, None, async_db, ticket)

        assert user["id"] == diver.id
        assert isinstance(request.state.download_ticket_id, str)

    @pytest.mark.asyncio
    async def test_a_ticket_for_one_path_is_refused_on_another(self, async_db: AsyncSession, diver: User) -> None:
        ticket, _ = await self._ticket(async_db, diver)

        with pytest.raises(UnauthorizedException):
            await get_downloading_user(_request("/api/v1/export/csv"), None, async_db, ticket)

    @pytest.mark.asyncio
    async def test_signing_the_session_out_ends_its_tickets(self, async_db: AsyncSession, diver: User) -> None:
        ticket, session_uuid = await self._ticket(async_db, diver)
        await revoke_session(async_db, session_uuid=session_uuid)
        await async_db.commit()

        with pytest.raises(UnauthorizedException):
            await get_downloading_user(_request(), None, async_db, ticket)

    @pytest.mark.asyncio
    async def test_no_ticket_and_no_header_is_a_401(self, async_db: AsyncSession) -> None:
        with pytest.raises(HTTPException) as refused:
            await get_downloading_user(_request(), None, async_db, None)

        assert refused.value.status_code == 401


SESSION = uuid_pkg.uuid4()
CALLER = {"id": 1, "uuid": uuid_pkg.uuid4(), "username": "ada", "is_superuser": False}


@pytest.fixture(scope="module")
def ticket_app() -> Any:
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def client(ticket_app: Any) -> Generator[TestClient]:
    ticket_app.dependency_overrides[get_current_user] = lambda: CALLER
    ticket_app.dependency_overrides[current_session_uuid] = lambda: SESSION
    ticket_app.dependency_overrides[refuse_api_keys] = lambda: None
    try:
        with TestClient(ticket_app) as test_client:
            yield test_client
    finally:
        ticket_app.dependency_overrides = {}


class TestMintingATicket:
    def test_the_ticket_is_for_the_caller_their_session_and_the_path(self, client: TestClient) -> None:
        response = client.post("/api/v1/user/download-ticket", json={"path": ARCHIVE})

        assert response.status_code == 201
        data = verify_download_token(response.json()["ticket"], path=ARCHIVE)
        assert data is not None
        assert (data.user_uuid, data.session_uuid) == (CALLER["uuid"], SESSION)

    def test_it_is_never_publicly_cacheable(self, client: TestClient) -> None:
        response = client.post("/api/v1/user/download-ticket", json={"path": ARCHIVE})

        assert response.headers["Cache-Control"] == "private, no-store"

    @pytest.mark.parametrize("path", ["export/archive", f"{ARCHIVE}?ticket=x", f"{ARCHIVE}#x", "/", "/" + "a" * 512])
    def test_a_path_it_could_never_match_is_refused(self, client: TestClient, path: str) -> None:
        assert client.post("/api/v1/user/download-ticket", json={"path": path}).status_code == 422


@pytest_asyncio.fixture
async def http(ticket_app: Any, async_db: AsyncSession) -> AsyncGenerator[httpx.AsyncClient]:
    """The real app with only the database overridden, so every credential is checked as it
    is in production."""

    async def the_tests_session() -> AsyncGenerator[AsyncSession]:
        yield async_db

    ticket_app.dependency_overrides[async_get_db] = the_tests_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ticket_app), base_url="http://test") as client:
        yield client
    ticket_app.dependency_overrides = {}


@needs_a_database
class TestADownloadByNavigation:
    @pytest.mark.asyncio
    async def test_a_minted_ticket_downloads_with_no_header(
        self, http: httpx.AsyncClient, async_db: AsyncSession, diver: User
    ) -> None:
        tokens = await issue_tokens(Response(), diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)
        minted = await http.post(
            "/api/v1/user/download-ticket",
            json={"path": "/export/divejson"},
            headers={"Authorization": f"Bearer {tokens.access_token}"},
        )
        assert minted.status_code == 201, minted.text

        response = await http.get("/api/v1/export/divejson", params={"ticket": minted.json()["ticket"]})

        assert response.status_code == 200, response.text
        assert response.headers["content-disposition"].startswith("attachment; filename=")
        assert response.headers["cache-control"] == "no-store"

    @pytest.mark.asyncio
    async def test_a_ticket_refused_is_not_publicly_cacheable(self, http: httpx.AsyncClient) -> None:
        response = await http.get("/api/v1/export/divejson", params={"ticket": "not-a-ticket"})

        assert response.status_code == 401
        assert response.headers["cache-control"] == "private, no-store"
