"""The way back into an account inside its deletion grace period.

`plans/account-deletion.md` §5. Deleting is reversible for `ACCOUNT_DELETION_GRACE_DAYS`,
and this is the half that makes that mean something: every entry point resolves a
soft-deleted account to a `deletion_pending` outcome instead of a dead end, and
`POST /auth/restore` is the one thing that brings it back.

Two properties get most of the attention here, because both are the kind that pass a type
checker and fail in production. **Nothing on the way to the restore screen writes
anything** - a sign-in that silently cancelled a deletion would defeat the point of asking.
And **the restore clears both soft-delete columns**, not just the flag: leaving the clock
set restores the account into a state nothing reconciles.

The passkey entry point is tested in `tests/test_passkeys.py`, beside the ceremony it needs;
`DELETE /user` and the purge are in `tests/test_account_deletion.py`.
"""

import uuid as uuid_pkg
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.v1.auth import (
    auth_with_google,
    check_email_link,
    complete_profile,
    restore_account,
    verify_email_code,
    verify_email_link,
)
from src.app.core.config import settings
from src.app.core.db.database import async_engine
from src.app.core.exceptions.http_exceptions import RateLimitException, UnauthorizedException
from src.app.core.schemas import GoogleUserInfo, OnboardingTokenData
from src.app.core.security import (
    create_onboarding_token,
    create_restore_token,
    hash_sign_in_code,
    verify_onboarding_token,
    verify_restore_token,
)
from src.app.core.worker.functions import purge_deleted_accounts
from src.app.models.user import User
from src.app.schemas.auth import (
    EmailCodeVerifyRequest,
    EmailVerifyRequest,
    GoogleAuthRequest,
    ProfileCompletionRequest,
    RestoreRequest,
)
from src.app.services.auth_service import AuthenticatedUser, DeletionPending, OnboardingRequired, resolve_identity
from tests.conftest import db_available
from tests.helpers.generators import create_user
from tests.helpers.mocks import stub_claim

USER_UUID = uuid_pkg.uuid4()
REQUEST_UUID = uuid_pkg.uuid4()

DELETED_AT = datetime.now(UTC) - timedelta(days=2)
PURGE_AFTER = DELETED_AT + timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS)


def _request(ip: str = "1.2.3.4") -> Mock:
    request = Mock()
    request.client = Mock(host=ip)
    return request


def _pending_row(email: str = "gone@example.com", *, deleted_at: datetime | None = DELETED_AT) -> dict[str, Any]:
    """A `user` row as `crud_users.get` hands it back once `DELETE /user` has flagged it."""
    return {
        "id": 1,
        "uuid": USER_UUID,
        "username": "leaving",
        "email": email,
        "is_deleted": True,
        "deleted_at": deleted_at,
    }


def _live_auth_request(email: str = "gone@example.com") -> dict[str, Any]:
    return {
        "id": 1,
        "email": email,
        "code_hash": hash_sign_in_code("481052"),
        "used_at": None,
        "invalidated_at": None,
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
    }


class TestResolveIdentityAgainstADeletedAccount:
    """Both of `resolve_identity`'s lookups, because relaxing only one of them is worse
    than relaxing neither."""

    @pytest.mark.asyncio
    async def test_the_email_lookup_answers_deletion_pending(self, mock_db):
        with (
            patch("src.app.services.auth_service.crud_authentication_providers") as providers,
            patch("src.app.services.auth_service.crud_users") as users,
        ):
            users.get = AsyncMock(return_value=_pending_row())

            outcome = await resolve_identity(mock_db, provider="email", email="gone@example.com")

            assert isinstance(outcome, DeletionPending)
            assert outcome.purge_after == PURGE_AFTER
            # Not a place to write to: the provider link waits for the restore.
            providers.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_provider_link_answers_deletion_pending_for_an_address_that_moved(self, mock_db):
        """The second-account bug, and the reason relaxing the email lookup alone is not
        half a feature but a hole.

        Someone who signed up with Google and later changed their account email is reachable
        *only* by provider link - `verify_email_change` writes the new address onto the row.
        With the filter left on the first lookup they would miss it, miss the email lookup
        too (the Google identity's address is no longer the row's), fall through to
        `OnboardingRequired`, and `/auth/complete` would hand them a **second account** while
        the first sat waiting to be purged.
        """
        with (
            patch("src.app.services.auth_service.crud_authentication_providers") as providers,
            patch("src.app.services.auth_service.crud_users") as users,
        ):
            providers.get = AsyncMock(return_value={"user_id": 1})
            users.get = AsyncMock(return_value=_pending_row(email="new-address@example.com"))

            outcome = await resolve_identity(
                mock_db, provider="google", email="the-old-one@example.com", provider_user_id="g-1"
            )

            assert isinstance(outcome, DeletionPending)
            assert not isinstance(outcome, OnboardingRequired)
            users.get.assert_called_once_with(db=mock_db, id=1)

    @pytest.mark.asyncio
    async def test_a_purged_account_is_onboarding_again(self, mock_db):
        """Bug 1's other end: once the row is gone the address is free, and signing up with
        it is an ordinary new account rather than "an account with this email already
        exists" against a tombstone."""
        with (
            patch("src.app.services.auth_service.crud_authentication_providers"),
            patch("src.app.services.auth_service.crud_users") as users,
        ):
            users.get = AsyncMock(return_value=None)

            outcome = await resolve_identity(mock_db, provider="email", email="gone@example.com")

            assert isinstance(outcome, OnboardingRequired)

    @pytest.mark.asyncio
    async def test_a_row_flagged_without_its_clock_still_offers_a_restore(self, mock_db):
        """The never-purge state `purge_deleted_accounts` warns about. There is no date to
        show, and the way back must not depend on there being one."""
        with (
            patch("src.app.services.auth_service.crud_authentication_providers"),
            patch("src.app.services.auth_service.crud_users") as users,
        ):
            users.get = AsyncMock(return_value=_pending_row(deleted_at=None))

            outcome = await resolve_identity(mock_db, provider="email", email="gone@example.com")

            assert isinstance(outcome, DeletionPending)
            assert outcome.purge_after is None


class TestTheOutcomeOnEveryEntryPoint:
    """`_start_onboarding_or_sign_in` is one `isinstance` chain with an `else` that reads
    `outcome.email`/`.provider`/`.name`, so a third variant that reaches it unhandled dies
    on the first attribute. These are the three paths that resolve through
    `resolve_identity`; the fourth (passkey) resolves from its own site and is tested in
    `tests/test_passkeys.py`.
    """

    @staticmethod
    def _assert_offered_the_account_back(outcome, response: Mock) -> None:
        assert outcome.status == "deletion_pending"
        assert outcome.restore_token
        assert outcome.purge_after == PURGE_AFTER
        assert outcome.email == "gone@example.com"
        # No session at all - not an access token, not a cookie. The account stays deleted
        # until the user acts on the screen this outcome routes to.
        assert outcome.access_token is None
        assert outcome.onboarding_token is None
        response.set_cookie.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_magic_link(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.services.auth_service.crud_authentication_providers"),
            patch("src.app.services.auth_service.crud_users") as users,
        ):
            requests.get = AsyncMock(return_value=_live_auth_request())
            users.get = AsyncMock(return_value=_pending_row())
            stub_claim(mock_db)

            response = Mock()
            outcome = await verify_email_link(_request(), EmailVerifyRequest(token="good"), response, mock_db)

            self._assert_offered_the_account_back(outcome, response)

    @pytest.mark.asyncio
    async def test_the_six_digit_code(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.services.auth_service.crud_authentication_providers"),
            patch("src.app.services.auth_service.crud_users") as users,
        ):
            requests.get = AsyncMock(return_value=_live_auth_request())
            users.get = AsyncMock(return_value=_pending_row())
            stub_claim(mock_db)

            response = Mock()
            outcome = await verify_email_code(
                _request(),
                EmailCodeVerifyRequest(request_id=REQUEST_UUID, code="481052"),
                response,
                mock_db,
            )

            self._assert_offered_the_account_back(outcome, response)

    @pytest.mark.asyncio
    async def test_google(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as verify,
            patch("src.app.services.auth_service.crud_authentication_providers") as providers,
            patch("src.app.services.auth_service.crud_users") as users,
        ):
            verify.return_value = GoogleUserInfo(google_id="g-1", email="gone@example.com", name="Gone")
            providers.get = AsyncMock(return_value=None)
            users.get = AsyncMock(return_value=_pending_row())

            response = Mock()
            outcome = await auth_with_google(_request(), GoogleAuthRequest(credential="good"), response, mock_db)

            self._assert_offered_the_account_back(outcome, response)

    @pytest.mark.asyncio
    async def test_the_precheck_relabels_the_button_without_calling_the_link_dead(self, mock_db):
        """`valid=True` plus a flag, deliberately - the link works, and `valid=False` is
        what the landing page shows "ask for a new one" for."""
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.api.v1.auth.crud_users") as users,
        ):
            requests.get = AsyncMock(return_value=_live_auth_request())
            users.get = AsyncMock(return_value=_pending_row())

            result = await check_email_link(_request(), Mock(headers={}), "good", mock_db)

            assert result.valid is True
            assert result.deletion_pending is True
            assert result.purge_after == PURGE_AFTER
            assert result.email == "gone@example.com"


class _RestoreSession(AsyncMock):
    """A session for `restore_account`: answers the locking SELECT with `locked` and records
    every statement it is handed.
    """

    def __init__(self, *, locked: Any) -> None:
        super().__init__()
        self._locked = locked
        self.statements: list[Any] = []

    async def execute(self, statement, parameters=None):
        self.statements.append(statement)
        result = MagicMock()
        result.one_or_none.return_value = self._locked
        return result

    @property
    def sql(self) -> str:
        return " ".join(str(statement).replace("\n", " ") for statement in self.statements)


class TestRestoreAccount:
    """`POST /auth/restore` - the explicit click that undoes a deletion."""

    @staticmethod
    def _call(session: Any, token: str, response: Any = None):
        return restore_account(
            request=_request(), body=RestoreRequest(restore_token=token), response=response or Mock(), db=session
        )

    @pytest.mark.asyncio
    async def test_it_clears_both_columns_and_signs_the_account_back_in(self):
        session = _RestoreSession(locked=Mock(id=1, is_deleted=True))
        token = await create_restore_token(USER_UUID)

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.verify_restore_token", new_callable=AsyncMock) as verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock) as blacklist,
        ):
            verify.return_value = USER_UUID
            response = Mock()
            outcome = await self._call(session, token, response)

        assert outcome.status == "authenticated"
        assert outcome.access_token
        response.set_cookie.assert_called_once()
        # Both columns, not just the flag. Clearing `is_deleted` alone would leave the
        # deletion clock set on a live account - a row nothing reconciles.
        assert "is_deleted" in session.sql
        assert "deleted_at" in session.sql
        blacklist.assert_awaited_once()
        assert blacklist.await_args.args[0] == token or blacklist.await_args.kwargs.get("token") == token

    @pytest.mark.asyncio
    async def test_it_takes_the_row_for_update_so_the_purge_cannot_pass_it(self):
        """The whole settlement of the race with `purge_deleted_accounts`: whichever
        transaction gets the lock first, the other sees the result rather than a stale
        snapshot."""
        session = _RestoreSession(locked=Mock(id=1, is_deleted=True))

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.verify_restore_token", new_callable=AsyncMock) as verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock),
        ):
            verify.return_value = USER_UUID
            await self._call(session, "tok")

        assert "FOR UPDATE" in session.sql

    @pytest.mark.asyncio
    async def test_an_account_already_purged_is_told_so_rather_than_401ing_generically(self):
        """The caller holds a token this server signed for this account, so there is nothing
        here they are not entitled to know - and "invalid link" would send them hunting for
        a fresh one that cannot exist."""
        session = _RestoreSession(locked=None)

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.verify_restore_token", new_callable=AsyncMock) as verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock) as blacklist,
        ):
            verify.return_value = USER_UUID

            with pytest.raises(UnauthorizedException) as raised:
                await self._call(session, "tok")

        assert "permanently deleted" in str(raised.value.detail)
        blacklist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_account_someone_else_already_restored_just_signs_in(self):
        """A second restore token, from a second entry point, arriving after the first has
        been spent. There is nothing left to undo, and the answer is the same session it
        would have got anyway."""
        session = _RestoreSession(locked=Mock(id=1, is_deleted=False))

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.verify_restore_token", new_callable=AsyncMock) as verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock),
        ):
            verify.return_value = USER_UUID
            outcome = await self._call(session, "tok")

        assert outcome.status == "authenticated"
        assert 'UPDATE "user"' not in session.sql

    @pytest.mark.asyncio
    async def test_a_double_submit_that_loses_the_race_is_a_401_not_a_500(self):
        """The one write here a row lock cannot serialize away.

        `verify_restore_token` reads the blacklist *before* the lock is taken, so both
        halves of a double-click are past that check before either commits. The loser then
        wakes up holding the lock and inserts a `token_blacklist.token` that is unique and
        already there - an uncaught `IntegrityError` would be a 500 on a restore that
        actually succeeded.
        """
        session = _RestoreSession(locked=Mock(id=1, is_deleted=False))

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.verify_restore_token", new_callable=AsyncMock) as verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock) as blacklist,
        ):
            verify.return_value = USER_UUID
            blacklist.side_effect = IntegrityError("duplicate key", None, Exception())

            with pytest.raises(UnauthorizedException) as raised:
                await self._call(session, "tok")

        # The same sentence the pre-lock check answers with - the two are one question
        # asked at two moments.
        assert "already been used" in str(raised.value.detail)
        session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_token_that_does_not_verify_is_a_401(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.verify_restore_token", new_callable=AsyncMock) as verify,
        ):
            verify.return_value = None

            with pytest.raises(UnauthorizedException):
                await self._call(mock_db, "nonsense")

    @pytest.mark.asyncio
    async def test_it_is_rate_limited(self, mock_db):
        with patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock) as limit:
            limit.side_effect = RateLimitException("Too many requests. Please try again later.")

            with pytest.raises(RateLimitException):
                await self._call(mock_db, "tok")


class TestRestoreTokens:
    """`TokenType.RESTORE` exists so that a restore token and an onboarding token are not
    interchangeable at each other's endpoints. Both directions are harmless against
    *today's* payloads, which is a fact about today rather than a design."""

    @pytest.mark.asyncio
    async def test_a_restore_token_is_not_an_onboarding_token(self, mock_db):
        with patch("src.app.core.security.crud_token_blacklist") as blacklist:
            blacklist.exists = AsyncMock(return_value=False)
            token = await create_restore_token(USER_UUID)

            assert await verify_onboarding_token(token, mock_db) is None

    @pytest.mark.asyncio
    async def test_an_onboarding_token_is_not_a_restore_token(self, mock_db):
        with patch("src.app.core.security.crud_token_blacklist") as blacklist:
            blacklist.exists = AsyncMock(return_value=False)
            token = await create_onboarding_token(OnboardingTokenData(email="a@example.com", provider="email"))

            assert await verify_restore_token(token, mock_db) is None

    @pytest.mark.asyncio
    async def test_a_spent_restore_token_is_refused(self, mock_db):
        """Single-use by blacklisting, exactly like the onboarding token - which is why it
        carries a `jti` and two minted in the same second are still distinct."""
        with patch("src.app.core.security.crud_token_blacklist") as blacklist:
            blacklist.exists = AsyncMock(return_value=True)
            token = await create_restore_token(USER_UUID)

            assert await verify_restore_token(token, mock_db) is None

    @pytest.mark.asyncio
    async def test_it_names_the_account_by_uuid(self, mock_db):
        """Not by email: the passkey path proves an identity with no email in it at all, and
        `verify_email_change` can rewrite the row's address while the token is in flight."""
        with patch("src.app.core.security.crud_token_blacklist") as blacklist:
            blacklist.exists = AsyncMock(return_value=False)
            token = await create_restore_token(USER_UUID)

            assert await verify_restore_token(token, mock_db) == USER_UUID

    @pytest.mark.asyncio
    async def test_a_restore_token_cannot_complete_a_profile(self, mock_db):
        """The endpoint-level half of the same property: `/auth/complete` reads its token
        through `verify_onboarding_token`, so a restore token is simply an invalid session
        there rather than a way to mint an account."""
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.core.security.crud_token_blacklist") as blacklist,
        ):
            blacklist.exists = AsyncMock(return_value=False)
            token = await create_restore_token(USER_UUID)

            with pytest.raises(UnauthorizedException):
                await complete_profile(
                    _request(),
                    ProfileCompletionRequest(onboarding_token=token, name="A Diver", username="adiver"),
                    Mock(),
                    mock_db,
                )


@pytest.mark.skipif(not db_available(), reason="database not reachable")
class TestRestoreAgainstPostgres:
    """The restore against a real database, because what it has to get right is two columns
    and a row lock - and a mocked session evaluates neither.

    The purge is run afterwards in the same test, unscoped as the cron runs it: a restore
    that only cleared the flag would leave `deleted_at` set, and the job's own predicate
    would then still find the account due.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        """`purge_deleted_accounts` opens its own session from the module-level
        `local_session`, and a pooled asyncpg connection belongs to the loop that opened
        it."""
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @pytest.mark.asyncio
    async def test_it_clears_both_columns_and_the_purge_then_leaves_the_account_alone(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        diver.is_deleted = True
        diver.deleted_at = datetime.now(UTC) - timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS + 1)
        db.commit()
        diver_id, diver_uuid = diver.id, diver.uuid
        db.expunge_all()

        token = await create_restore_token(diver_uuid)
        with patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock):
            outcome = await restore_account(
                request=_request(), body=RestoreRequest(restore_token=token), response=Mock(), db=async_db
            )

        assert outcome.status == "authenticated"
        restored = db.get(User, diver_id)
        assert restored is not None
        assert restored.is_deleted is False
        assert restored.deleted_at is None

        await purge_deleted_accounts({})

        db.expire_all()
        assert db.get(User, diver_id) is not None

    @pytest.mark.asyncio
    async def test_the_token_is_spent_so_the_same_screen_cannot_be_submitted_twice(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        diver.is_deleted = True
        diver.deleted_at = datetime.now(UTC) - timedelta(days=1)
        db.commit()
        diver_uuid = diver.uuid
        db.expunge_all()

        token = await create_restore_token(diver_uuid)
        with patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock):
            await restore_account(
                request=_request(), body=RestoreRequest(restore_token=token), response=Mock(), db=async_db
            )

            with pytest.raises(UnauthorizedException):
                await restore_account(
                    request=_request(), body=RestoreRequest(restore_token=token), response=Mock(), db=async_db
                )

    @pytest.mark.asyncio
    async def test_an_account_the_purge_already_took_cannot_be_restored(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        diver.is_deleted = True
        diver.deleted_at = datetime.now(UTC) - timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS + 1)
        db.commit()
        diver_uuid = diver.uuid
        db.expunge_all()

        token = await create_restore_token(diver_uuid)
        await purge_deleted_accounts({})

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            pytest.raises(UnauthorizedException) as raised,
        ):
            await restore_account(
                request=_request(), body=RestoreRequest(restore_token=token), response=Mock(), db=async_db
            )

        assert "permanently deleted" in str(raised.value.detail)

    @pytest.mark.asyncio
    async def test_a_restored_account_resolves_as_authenticated_again(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """End to end through the real resolver: deleted, offered back, restored, and an
        ordinary sign-in afterwards."""
        diver = create_user(db)
        diver.is_deleted = True
        diver.deleted_at = datetime.now(UTC) - timedelta(days=1)
        db.commit()
        email, diver_uuid = diver.email, diver.uuid
        db.expunge_all()

        pending = await resolve_identity(async_db, provider="email", email=email)
        assert isinstance(pending, DeletionPending)

        token = await create_restore_token(diver_uuid)
        with patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock):
            await restore_account(
                request=_request(), body=RestoreRequest(restore_token=token), response=Mock(), db=async_db
            )

        assert isinstance(await resolve_identity(async_db, provider="email", email=email), AuthenticatedUser)
