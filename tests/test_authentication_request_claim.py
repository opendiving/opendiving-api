"""Integration tests for `claim_authentication_request` (`crud/crud_authentication_requests.py`),
the conditional `UPDATE ... WHERE used_at IS NULL` that makes a magic-link token single-use.

Against a live Postgres, because the whole point of the helper is behaviour the database
supplies and a mocked session cannot: the affected-row count of a predicated `UPDATE`, and
what that count is under two transactions racing for the same row. The endpoint-level tests
in `test_auth.py`/`test_email_change.py` pin what each caller does with the answer; these
pin that the answer is right.

Automatically skipped when no database is reachable - note that a run on the host needs
`POSTGRES_SERVER=localhost` before these execute at all (see `CONTRIBUTING.md`).
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from src.app.core.config import settings
from src.app.crud.crud_authentication_requests import claim_authentication_request
from src.app.models.authentication_request import AuthenticationRequest
from src.app.models.user import User
from tests.conftest import db_available, unique_email

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _live_request(db: Session, *, purpose: str = "sign_in", user: User | None = None) -> AuthenticationRequest:
    """An unused, unexpired request row. The token hash only has to be unique."""
    row = AuthenticationRequest(
        email=unique_email(),
        token_hash=f"hash-{datetime.now(UTC).timestamp()}-{id(db)}-{purpose}",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose=purpose,
        user_id=user.id if user is not None else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestClaimAuthenticationRequest:
    @pytest.mark.asyncio
    async def test_first_claim_wins_and_stamps_used_at(self, db: Session, async_db: AsyncSession) -> None:
        row = _live_request(db)

        assert await claim_authentication_request(async_db, request_id=row.id) is True

        stamped = await async_db.scalar(select(AuthenticationRequest.used_at).where(AuthenticationRequest.id == row.id))
        assert stamped is not None

    @pytest.mark.asyncio
    async def test_second_claim_loses_and_leaves_the_first_stamp_alone(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """A used token cannot be re-spent, and losing must not overwrite `used_at` -
        `verify_email_change` reads that timestamp to decide whether a replay is the
        harmless kind."""
        row = _live_request(db)
        await claim_authentication_request(async_db, request_id=row.id)
        first = await async_db.scalar(select(AuthenticationRequest.used_at).where(AuthenticationRequest.id == row.id))

        assert await claim_authentication_request(async_db, request_id=row.id) is False

        assert (
            await async_db.scalar(select(AuthenticationRequest.used_at).where(AuthenticationRequest.id == row.id))
            == first
        )

    @pytest.mark.asyncio
    async def test_a_missing_row_is_a_loss_not_an_error(self, async_db: AsyncSession) -> None:
        """FastCRUD's `update` raises `NoResultFound` when nothing matches. This reports
        it as `False` instead, which is the same thing every caller already handles."""
        assert await claim_authentication_request(async_db, request_id=-1) is False

    @pytest.mark.asyncio
    async def test_exactly_one_of_two_concurrent_claims_wins(self, db: Session) -> None:
        """The race the helper exists for, run for real on two connections.

        Both statements are issued before either commits, so the second blocks on the
        first's row lock rather than reading a stale snapshot - and when it unblocks it
        re-evaluates `used_at IS NULL` against the committed new row version, matches
        nothing, and reports zero. That is the whole argument for `rowcount` being a
        sufficient signal under READ COMMITTED, and it is worth executing rather than
        asserting in a comment.
        """
        row = _live_request(db)
        engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with session_factory() as one, session_factory() as two:
                results = await asyncio.gather(
                    claim_authentication_request(one, request_id=row.id),
                    claim_authentication_request(two, request_id=row.id),
                )
        finally:
            await engine.dispose()

        assert sorted(results) == [False, True]
