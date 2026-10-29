"""`POST /auth/email/request`'s per-address limit, held in Postgres as well as Redis.

The Redis limiter fails open on an outage, and the number of requests per address is what
bounds guesses at that address's codes. These run with Redis down, against a live Postgres,
because what is pinned is the count and the advisory lock it is read under.

Automatically skipped when no database is reachable - a run on the host needs
`POSTGRES_SERVER=localhost` before these execute at all (see `CONTRIBUTING.md`).
"""

import asyncio
import contextlib
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import auth as auth_module
from src.app.api.v1.auth import request_email_link
from src.app.core.config import settings
from src.app.core.exceptions.http_exceptions import RateLimitException
from src.app.core.utils import cache
from src.app.models.authentication_request import AuthenticationRequest
from src.app.schemas.auth import EmailAuthRequest
from tests.conftest import db_available, unique_email
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.mocks import fake_request

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

# The default, pinned: a developer's `src/.env` may raise it.
LIMIT = 3


@pytest.fixture(autouse=True)
def _redis_down_and_no_mail() -> Generator[None]:
    with (
        patch.object(cache, "client", FakeRedis(failing=True)),
        patch.object(settings, "MAGIC_LINK_REQUEST_RATE_LIMIT_PER_EMAIL", LIMIT),
        patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
    ):
        yield


async def _ask(db: AsyncSession, email: str) -> RateLimitException | None:
    try:
        await request_email_link(fake_request(), EmailAuthRequest(email=email), db)
    except RateLimitException as refusal:
        # What closing the request's session does in the app: a refusal raised while the
        # address lock is held ends the transaction that holds it.
        await db.rollback()
        return refusal
    return None


async def _rows(db: AsyncSession, email: str) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(AuthenticationRequest).where(AuthenticationRequest.email == email)
        )
        or 0
    )


class TestTheRequestQuotaWithRedisDown:
    @pytest.mark.asyncio
    async def test_the_request_past_the_limit_is_refused_as_the_redis_limit_refuses_it(
        self, async_db: AsyncSession
    ) -> None:
        email = unique_email()
        for _ in range(LIMIT):
            assert await _ask(async_db, email) is None

        refused = await _ask(async_db, email)

        assert refused is not None
        assert await _rows(async_db, email) == LIMIT

        # What the Redis limiter says for the same address when it is up and spent.
        redis = FakeRedis()
        key = f"auth:email-request:email:{email}"
        redis.store[key] = str(LIMIT).encode()
        redis.expiries[key] = settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS
        with patch.object(cache, "client", redis):
            from_redis = await _ask(async_db, email)

        assert from_redis is not None
        assert (refused.status_code, refused.detail) == (from_redis.status_code, from_redis.detail)

    @pytest.mark.asyncio
    async def test_another_address_is_unaffected(self, async_db: AsyncSession) -> None:
        spent = unique_email()
        for _ in range(LIMIT + 1):
            await _ask(async_db, spent)

        assert await _ask(async_db, unique_email()) is None

    @pytest.mark.asyncio
    async def test_requests_older_than_the_window_do_not_count(self, db: Session, async_db: AsyncSession) -> None:
        email = unique_email()
        before_the_window = datetime.now(UTC) - timedelta(seconds=settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS + 60)
        db.add_all(
            AuthenticationRequest(
                email=email,
                token_hash=f"hash-{uuid7().hex}",
                expires_at=before_the_window + timedelta(minutes=15),
                purpose="sign_in",
                created_at=before_the_window,
            )
            for _ in range(LIMIT)
        )
        db.commit()

        assert await _ask(async_db, email) is None

    @pytest.mark.asyncio
    async def test_parallel_requests_admit_no_more_than_the_limit(
        self, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The race the lock exists for. Each request is held after counting until all of
        them have counted, or for a moment: with the lock only one is ever past it, so the
        hold times out and they go one at a time; without it, every request counts zero
        before any insert lands, every time."""
        email = unique_email()
        callers = LIMIT + 2
        counted = 0
        everyone_counted = asyncio.Event()
        real_count = auth_module.sign_in_requests_since

        async def count_then_hold(db: AsyncSession, *, email: str, window_seconds: int) -> int:
            nonlocal counted
            recent = await real_count(db, email=email, window_seconds=window_seconds)
            counted += 1
            if counted == callers:
                everyone_counted.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(everyone_counted.wait(), 0.25)
            return recent

        sessions = [async_sessions() for _ in range(callers)]
        try:
            with patch.object(auth_module, "sign_in_requests_since", count_then_hold):
                outcomes = await asyncio.gather(*(_ask(session, email) for session in sessions))
            stored = await _rows(sessions[0], email)
        finally:
            for session in sessions:
                await session.close()

        assert sum(outcome is None for outcome in outcomes) == LIMIT
        assert stored == LIMIT
