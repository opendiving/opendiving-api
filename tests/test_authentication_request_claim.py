"""Integration tests for the two hand-written statements in
`crud/crud_authentication_requests.py`: `claim_authentication_request`, the conditional
`UPDATE ... WHERE used_at IS NULL` that makes a magic-link token single-use, and
`register_failed_code_attempt`, the increment-and-maybe-null that bounds guesses at the
six-digit sign-in code.

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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.core.security import hash_sign_in_code
from src.app.crud.crud_authentication_requests import (
    claim_authentication_request,
    invalidate_live_requests,
    register_failed_code_attempt,
)
from src.app.models.authentication_request import AuthenticationRequest
from src.app.models.user import User
from tests.conftest import db_available, unique_email
from tests.helpers.generators import create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _live_request(
    db: Session,
    *,
    purpose: str = "sign_in",
    user: User | None = None,
    code: str | None = None,
    email: str | None = None,
) -> AuthenticationRequest:
    """An unused, unexpired request row. The token hash only has to be unique."""
    row = AuthenticationRequest(
        email=email or unique_email(),
        token_hash=f"hash-{uuid7().hex}",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        code_hash=hash_sign_in_code(code) if code is not None else None,
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
    async def test_exactly_one_of_two_concurrent_claims_wins(
        self, db: Session, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The race the helper exists for, run for real on two connections.

        Both statements are issued before either commits, so the second blocks on the
        first's row lock rather than reading a stale snapshot - and when it unblocks it
        re-evaluates `used_at IS NULL` against the committed new row version, matches
        nothing, and reports zero. That is the whole argument for `rowcount` being a
        sufficient signal under READ COMMITTED, and it is worth executing rather than
        asserting in a comment.
        """
        row = _live_request(db)
        async with async_sessions() as one, async_sessions() as two:
            results = await asyncio.gather(
                claim_authentication_request(one, request_id=row.id),
                claim_authentication_request(two, request_id=row.id),
            )

        assert sorted(results) == [False, True]


class TestRegisterFailedCodeAttempt:
    """`register_failed_code_attempt` - one statement that charges a wrong guess and, on
    the guess that reaches the cap, spends the code.

    Also against a live Postgres, and for the same reason: `code_attempts + 1` evaluated
    inside a `CASE` under concurrent statements is exactly the behaviour a mock cannot
    supply, and it is the behaviour the attempt cap's usefulness rests on.
    """

    @pytest.mark.asyncio
    async def test_a_guess_below_the_cap_is_counted_and_the_code_survives(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        row = _live_request(db, code="481052")

        await register_failed_code_attempt(async_db, request_id=row.id, max_attempts=5)

        after = (
            await async_db.execute(
                select(AuthenticationRequest.code_attempts, AuthenticationRequest.code_hash).where(
                    AuthenticationRequest.id == row.id
                )
            )
        ).one()
        assert after.code_attempts == 1
        assert after.code_hash == hash_sign_in_code("481052")

    @pytest.mark.asyncio
    async def test_the_guess_that_reaches_the_cap_spends_the_code(self, db: Session, async_db: AsyncSession) -> None:
        row = _live_request(db, code="481052")

        for _ in range(5):
            await register_failed_code_attempt(async_db, request_id=row.id, max_attempts=5)

        after = (
            await async_db.execute(
                select(AuthenticationRequest.code_attempts, AuthenticationRequest.code_hash).where(
                    AuthenticationRequest.id == row.id
                )
            )
        ).one()
        assert after.code_attempts == 5
        assert after.code_hash is None

    @pytest.mark.asyncio
    async def test_a_spent_code_leaves_its_link_claimable(self, db: Session, async_db: AsyncSession) -> None:
        """The regression test for the design this endpoint was redrawn around.

        An earlier shape had a burnt code burn its link, which handed anyone who could
        reach the verify endpoint a way to cancel a sign-in they could not complete -
        aimed at exactly the accounts whose only recovery path is that inbox. Five wrong
        guesses must leave the link in the same email fully live.
        """
        row = _live_request(db, code="481052")

        for _ in range(5):
            await register_failed_code_attempt(async_db, request_id=row.id, max_attempts=5)

        assert await claim_authentication_request(async_db, request_id=row.id) is True

    @pytest.mark.asyncio
    async def test_concurrent_guesses_are_each_charged(
        self, db: Session, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The race the statement exists for. Read-decide-write would let two guesses
        issued together both store `1`, and an attacker firing five in parallel would be
        charged for one - which is the difference between a five-guess bound and none.
        """
        row = _live_request(db, code="481052")
        async with async_sessions() as one, async_sessions() as two:
            await asyncio.gather(
                register_failed_code_attempt(one, request_id=row.id, max_attempts=5),
                register_failed_code_attempt(two, request_id=row.id, max_attempts=5),
            )
            charged = await one.scalar(
                select(AuthenticationRequest.code_attempts).where(AuthenticationRequest.id == row.id)
            )

        assert charged == 2


class TestInvalidateLiveRequests:
    """`invalidate_live_requests` - the one-live-link rule as one `UPDATE`, against a live
    Postgres because "matches nothing" has to be an ordinary answer rather than the
    `NoResultFound` FastCRUD's counted precondition raises."""

    @staticmethod
    async def _invalidated(async_db: AsyncSession, row: AuthenticationRequest) -> bool:
        stamped = await async_db.scalar(
            select(AuthenticationRequest.invalidated_at).where(AuthenticationRequest.id == row.id)
        )
        return stamped is not None

    @pytest.mark.asyncio
    async def test_nothing_to_invalidate_is_not_an_error(self, async_db: AsyncSession) -> None:
        await invalidate_live_requests(async_db, purpose="sign_in", email=unique_email())

    @pytest.mark.asyncio
    async def test_stamps_the_addresses_live_rows_of_that_purpose_only(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        email = unique_email()
        first, second = _live_request(db, email=email), _live_request(db, email=email)
        other_address = _live_request(db)
        other_purpose = _live_request(db, email=email, purpose="email_change")

        await invalidate_live_requests(async_db, purpose="sign_in", email=email)
        await async_db.commit()

        assert await self._invalidated(async_db, first)
        assert await self._invalidated(async_db, second)
        assert not await self._invalidated(async_db, other_address)
        assert not await self._invalidated(async_db, other_purpose)

    @pytest.mark.asyncio
    async def test_keys_on_the_account_when_given_one(self, db: Session, async_db: AsyncSession) -> None:
        user = create_user(db)
        mine = _live_request(db, purpose="email_change", user=user)
        someone_elses = _live_request(db, purpose="email_change", user=create_user(db))

        await invalidate_live_requests(async_db, purpose="email_change", user_id=user.id)
        await async_db.commit()

        assert await self._invalidated(async_db, mine)
        assert not await self._invalidated(async_db, someone_elses)
