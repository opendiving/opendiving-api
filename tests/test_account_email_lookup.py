"""`crud.crud_users.account_for_email`, the lookup sign-in resolves a verified address with,
against a live Postgres.

A stored address may carry capitals - `POST /auth/complete` stores the onboarding token's
address as it came, and Google's claim is not lowercased - while the magic link lowercases
what it is sent. These pin that the lookup finds such an account, so its owner is signed in
rather than sent to onboarding for a second one, and that two rows differing only in case
resolve the same way every time.

Automatically skipped when no database is reachable - note that a run on the host needs
`POSTGRES_SERVER=localhost` before these execute at all (see `CONTRIBUTING.md`).
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.core.utils.request_context import RequestContext
from src.app.crud.crud_users import account_for_email
from src.app.models.user import User
from src.app.services.auth_service import AuthenticatedUser, resolve_identity
from tests.conftest import db_available, unique_username

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _account(db: Session, email: str, *, is_deleted: bool = False) -> User:
    user = User(name="Diver", username=unique_username(), email=email, uuid=uuid7())
    user.is_deleted = is_deleted
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mixed_case_address() -> str:
    return f"Jane.{uuid7().hex[-12:]}@Example.com"


class TestAccountForEmail:
    @pytest.mark.asyncio
    async def test_finds_a_capitalised_account_by_its_lowercased_address(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        stored = _mixed_case_address()
        user = _account(db, stored)

        found = await account_for_email(async_db, email=stored.lower())

        assert found is not None
        assert found["id"] == user.id
        assert found["email"] == stored

    @pytest.mark.asyncio
    async def test_returns_every_column_crud_users_get_would(self, db: Session, async_db: AsyncSession) -> None:
        """Its callers index the row as `crud_users.get`'s dict - `is_deleted`,
        `deleted_at`, `uuid` - so the shape is the contract."""
        stored = _mixed_case_address()
        _account(db, stored)

        found = await account_for_email(async_db, email=stored)

        assert found is not None
        assert {"id", "uuid", "email", "is_deleted", "deleted_at", "username"} <= found.keys()

    @pytest.mark.asyncio
    async def test_an_exact_case_match_wins_over_an_older_case_twin(self, db: Session, async_db: AsyncSession) -> None:
        stored = _mixed_case_address()
        _account(db, stored)
        lowercase = _account(db, stored.lower())

        found = await account_for_email(async_db, email=stored.lower())

        assert found is not None
        assert found["id"] == lowercase.id

    @pytest.mark.asyncio
    async def test_twins_neither_matching_exactly_resolve_to_the_oldest(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        stored = _mixed_case_address()
        older = _account(db, stored)
        _account(db, stored.upper())

        found = await account_for_email(async_db, email=stored.lower())

        assert found is not None
        assert found["id"] == older.id

    @pytest.mark.asyncio
    async def test_a_deleted_account_is_still_found(self, db: Session, async_db: AsyncSession) -> None:
        """`resolve_identity` answers `DeletionPending` for it, which needs the row."""
        stored = _mixed_case_address()
        _account(db, stored, is_deleted=True)

        found = await account_for_email(async_db, email=stored.lower())

        assert found is not None
        assert found["is_deleted"] is True

    @pytest.mark.asyncio
    async def test_an_unknown_address_is_none(self, async_db: AsyncSession) -> None:
        assert await account_for_email(async_db, email=_mixed_case_address()) is None


class TestSigningInByEmailToACapitalisedAccount:
    @pytest.mark.asyncio
    async def test_signs_the_account_in_rather_than_onboarding_a_second(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        stored = _mixed_case_address()
        user = _account(db, stored)

        outcome = await resolve_identity(
            async_db, provider="email", email=stored.lower(), context=RequestContext(ip="1.2.3.4", user_agent="test")
        )

        assert isinstance(outcome, AuthenticatedUser)
        assert outcome.user["id"] == user.id
