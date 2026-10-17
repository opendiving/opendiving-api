"""`crud_token_blacklist.revoke`, the `INSERT ... ON CONFLICT (token) DO NOTHING` every
revocation goes through, against a live Postgres - what the statement answers when the same
token is revoked twice, one after the other and at once, is the database's behaviour and not
something a mocked session can supply.

Automatically skipped when no database is reachable - note that a run on the host needs
`POSTGRES_SERVER=localhost` before these execute at all (see `CONTRIBUTING.md`).
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from uuid6 import uuid7

from src.app.core.db.crud_token_blacklist import crud_token_blacklist
from src.app.core.db.token_blacklist import TokenBlacklist
from src.app.core.schemas import TokenBlacklistCreate
from tests.conftest import db_available

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _entry() -> TokenBlacklistCreate:
    now = datetime.now(UTC)
    return TokenBlacklistCreate(token=f"token-{uuid7().hex}", expires_at=now + timedelta(days=1), revoked_at=now)


class TestRevoke:
    @pytest.mark.asyncio
    async def test_the_first_revocation_inserts_and_the_second_does_not(self, async_db: AsyncSession) -> None:
        entry = _entry()

        assert await crud_token_blacklist.revoke(async_db, object=entry) is True
        assert await crud_token_blacklist.revoke(async_db, object=entry) is False

        rows = await async_db.scalar(
            select(func.count()).select_from(TokenBlacklist).where(TokenBlacklist.token == entry.token)
        )
        assert rows == 1

    @pytest.mark.asyncio
    async def test_exactly_one_of_two_concurrent_revocations_inserts(
        self, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The double-click, run on two connections: the second insert waits on the first's
        transaction, then inserts nothing - `False`, never an `IntegrityError`."""
        entry = _entry()
        async with async_sessions() as one, async_sessions() as two:
            results = await asyncio.gather(
                crud_token_blacklist.revoke(one, object=entry),
                crud_token_blacklist.revoke(two, object=entry),
            )

        assert sorted(results) == [False, True]
