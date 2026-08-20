"""Integration tests for the `webauthn_credential` row itself, against a live Postgres.

Two things a mocked session cannot answer, and both are load-bearing:

- **`record_assertion`'s conditional UPDATE.** Its `rowcount` under two transactions racing
  for the same row is what decides which of two submissions of one assertion mints a
  session. Same argument, at more length, in `test_authentication_request_claim.py`.
- **`ON DELETE CASCADE` on `user_id`.** It ships from day one so
  `plans/account-deletion.md`'s purge needs no edit here - which is only true if the
  database actually enforces it, and a declaration in a model proves nothing about the
  table an Alembic revision built.

Automatically skipped when no database is reachable - note that a run on the host needs
`POSTGRES_SERVER=localhost` before these execute at all (see `CONTRIBUTING.md`).
"""

import asyncio
import os

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from src.app.core.config import settings
from src.app.crud.crud_webauthn_credentials import record_assertion
from src.app.models.user import User
from src.app.models.webauthn_credential import WebauthnCredential
from tests.conftest import db_available
from tests.helpers.generators import create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _credential(db: Session, user: User, *, sign_count: int = 0) -> WebauthnCredential:
    """A stored passkey. The credential id only has to be unique; nothing here verifies
    a ceremony (`test_passkeys.py` does that), so random bytes are the honest stand-in.
    """
    row = WebauthnCredential(
        user_id=user.id,
        credential_id=os.urandom(32),
        public_key=os.urandom(64),
        name="iPhone",
        sign_count=sign_count,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestRecordAssertion:
    @pytest.mark.asyncio
    async def test_it_stamps_the_counter_and_the_last_use(self, db: Session, async_db: AsyncSession) -> None:
        row = _credential(db, create_user(db), sign_count=3)

        assert (
            await record_assertion(
                async_db, credential_uuid=row.uuid, sign_count=9, backed_up=True, expected_sign_count=3
            )
            is True
        )

        stored = (
            await async_db.execute(
                select(
                    WebauthnCredential.sign_count, WebauthnCredential.last_used_at, WebauthnCredential.backed_up
                ).where(WebauthnCredential.uuid == row.uuid)
            )
        ).one()
        assert stored.sign_count == 9
        assert stored.last_used_at is not None
        assert stored.backed_up is True

    @pytest.mark.asyncio
    async def test_a_stale_expected_counter_loses(self, db: Session, async_db: AsyncSession) -> None:
        """Verification ran against a counter that has since moved, so this caller's
        assertion has already been spent by someone else."""
        row = _credential(db, create_user(db), sign_count=5)

        assert (
            await record_assertion(
                async_db, credential_uuid=row.uuid, sign_count=6, backed_up=False, expected_sign_count=4
            )
            is False
        )

        assert (
            await async_db.scalar(select(WebauthnCredential.sign_count).where(WebauthnCredential.uuid == row.uuid)) == 5
        )

    @pytest.mark.asyncio
    async def test_exactly_one_of_two_concurrent_recordings_wins(self, db: Session) -> None:
        """The race the helper exists for, run for real on two connections - the same
        shape as the magic-link claim test, for the same READ COMMITTED reason.
        """
        row = _credential(db, create_user(db), sign_count=2)
        engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with session_factory() as one, session_factory() as two:
                results = await asyncio.gather(
                    record_assertion(
                        one, credential_uuid=row.uuid, sign_count=3, backed_up=True, expected_sign_count=2
                    ),
                    record_assertion(
                        two, credential_uuid=row.uuid, sign_count=3, backed_up=True, expected_sign_count=2
                    ),
                )
        finally:
            await engine.dispose()

        assert sorted(results) == [False, True]


class TestCascade:
    @pytest.mark.asyncio
    async def test_deleting_the_user_takes_their_credentials_with_it(self, db: Session, async_db: AsyncSession) -> None:
        """A credential that outlived its account would be a live sign-in path pointing at
        a row that no longer exists.
        """
        user = create_user(db)
        _credential(db, user)

        await async_db.execute(delete(User).where(User.id == user.id))
        await async_db.commit()

        assert (
            await async_db.scalar(
                select(func.count()).select_from(WebauthnCredential).where(WebauthnCredential.user_id == user.id)
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_two_users_cannot_share_a_credential_id(self, db: Session, async_db: AsyncSession) -> None:
        """The sign-in ceremony names no account, so `credential_id` alone has to identify
        one - which is only true while the database refuses a second row with the same id.
        """
        from sqlalchemy.exc import IntegrityError

        first = _credential(db, create_user(db))
        duplicate = WebauthnCredential(
            user_id=create_user(db).id,
            credential_id=first.credential_id,
            public_key=os.urandom(64),
            name="Impostor",
        )
        db.add(duplicate)

        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
