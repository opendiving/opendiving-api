"""Unit tests for the email-change confirmation flow (`api.v1.users`): requesting a
change requires confirming ownership of the new address via a magic link before it
takes effect - directly setting `email` via `PATCH /user` is no longer possible
(see `schemas.user.UserUpdate`).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1.users import check_email_change_link, request_email_change, verify_email_change
from src.app.core.exceptions.http_exceptions import (
    BadRequestException,
    DuplicateValueException,
    NotFoundException,
    RateLimitException,
    UnauthorizedException,
)
from src.app.core.security import hash_token
from src.app.models.authentication_request import AuthenticationRequest
from src.app.models.user import User
from src.app.schemas.email_change import EmailChangeRequest, EmailChangeVerifyRequest
from tests.conftest import db_available, unique_email
from tests.helpers.mocks import claimed_used_at_sql, fake_request, stub_claim

_request = fake_request


def _used_at(db: Session, request_id: int) -> datetime | None:
    """The committed `used_at` of one request row, read fresh.

    A column select rather than `db.get`, which would hand back whatever the sync
    session already has in its identity map - the stale `None` it wrote itself.
    """
    return db.scalar(select(AuthenticationRequest.used_at).where(AuthenticationRequest.id == request_id))


class TestRequestEmailChange:
    """`POST /user/email-change/request`."""

    @pytest.mark.asyncio
    async def test_rejects_unchanged_email(self, mock_db, current_user_dict):
        with pytest.raises(BadRequestException):
            await request_email_change(
                _request(),
                EmailChangeRequest(new_email=current_user_dict["email"].upper()),
                current_user_dict,
                mock_db,
            )

    @pytest.mark.asyncio
    async def test_rate_limited_requests_are_rejected(self, mock_db, current_user_dict):
        with patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock) as mock_limit:
            mock_limit.side_effect = RateLimitException("Too many requests. Please try again later.")

            with pytest.raises(RateLimitException):
                await request_email_change(
                    _request(),
                    EmailChangeRequest(new_email="new@example.com"),
                    current_user_dict,
                    mock_db,
                )

    @pytest.mark.asyncio
    async def test_invalidates_previous_pending_request(self, mock_db, current_user_dict):
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.users.send_email_change_confirmation_email", new_callable=AsyncMock),
        ):
            mock_crud.count = AsyncMock(return_value=1)
            mock_crud.update = AsyncMock(return_value=None)
            mock_crud.create = AsyncMock(return_value=None)

            await request_email_change(
                _request(),
                EmailChangeRequest(new_email="new@example.com"),
                current_user_dict,
                mock_db,
            )

            mock_crud.update.assert_called_once()
            kwargs = mock_crud.update.call_args.kwargs
            assert kwargs["allow_multiple"] is True
            assert kwargs["user_id"] == current_user_dict["id"]
            assert kwargs["purpose"] == "email_change"
            assert kwargs["invalidated_at"] is None
            assert kwargs["object"].invalidated_at is not None

    @pytest.mark.asyncio
    async def test_the_row_it_mints_carries_no_sign_in_code(self, mock_db, current_user_dict):
        """`authentication_request` backs both flows, and only sign-in puts a code on the
        row. An email change is confirmed by opening the link *in the new mailbox*, so a
        code typed back into the tab that asked would prove nothing about that mailbox -
        it would turn a possession proof into a click.
        """
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.users.send_email_change_confirmation_email", new_callable=AsyncMock),
        ):
            mock_crud.count = AsyncMock(return_value=0)
            mock_crud.create = AsyncMock(return_value=None)

            await request_email_change(
                _request(),
                EmailChangeRequest(new_email="new@example.com"),
                current_user_dict,
                mock_db,
            )

            assert mock_crud.create.call_args.kwargs["object"].code_hash is None

    @pytest.mark.asyncio
    async def test_does_not_call_update_when_there_is_nothing_pending(self, mock_db, current_user_dict):
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.users.send_email_change_confirmation_email", new_callable=AsyncMock),
        ):
            mock_crud.count = AsyncMock(return_value=0)
            mock_crud.update = AsyncMock(return_value=None)
            mock_crud.create = AsyncMock(return_value=None)

            await request_email_change(
                _request(),
                EmailChangeRequest(new_email="new@example.com"),
                current_user_dict,
                mock_db,
            )

            mock_crud.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_request_and_sends_confirmation_email(self, mock_db, current_user_dict):
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.users.send_email_change_confirmation_email", new_callable=AsyncMock) as mock_send,
        ):
            mock_crud.count = AsyncMock(return_value=0)
            mock_crud.create = AsyncMock(return_value=None)

            result = await request_email_change(
                _request(),
                EmailChangeRequest(new_email="new@example.com"),
                current_user_dict,
                mock_db,
            )

            assert result.message == "Check your new email address to confirm the change."
            mock_crud.create.assert_called_once()
            created_object = mock_crud.create.call_args.kwargs["object"]
            assert created_object.email == "new@example.com"
            assert created_object.purpose == "email_change"
            assert created_object.user_id == current_user_dict["id"]
            mock_send.assert_called_once()
            assert mock_send.call_args.kwargs["new_email"] == "new@example.com"


class TestCheckEmailChangeLink:
    """`GET /user/email-change/verify/check` - the side-effect-free precheck used by
    the confirmation page before it shows the "Confirm email change" button."""

    @pytest.mark.asyncio
    async def test_rate_limited_requests_are_rejected(self, mock_db):
        with patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock) as mock_limit:
            mock_limit.side_effect = RateLimitException("Too many requests. Please try again later.")

            with pytest.raises(RateLimitException):
                await check_email_change_link(_request(), Response(), "any", mock_db)

    @pytest.mark.asyncio
    async def test_not_found_is_invalid(self, mock_db):
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=None)

            result = await check_email_change_link(_request(), Response(), "bad", mock_db)

            assert result.valid is False
            assert result.email is None

    @pytest.mark.asyncio
    async def test_invalidated_token_is_invalid(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            result = await check_email_change_link(_request(), Response(), "stale", mock_db)

            assert result.valid is False

    @pytest.mark.asyncio
    async def test_already_used_token_is_invalid(self, mock_db):
        """This is what actually keeps a human from re-confirming after already
        confirming once (e.g. via the browser's back button) - unlike
        `verify_email_change`'s idempotent-reuse leniency, this precheck must treat
        an already-used token as invalid so no button is even shown."""
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": datetime.now(UTC),
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            result = await check_email_change_link(_request(), Response(), "already-used", mock_db)

            assert result.valid is False

    @pytest.mark.asyncio
    async def test_expired_token_is_invalid(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            result = await check_email_change_link(_request(), Response(), "expired", mock_db)

            assert result.valid is False

    @pytest.mark.asyncio
    async def test_fresh_token_is_valid_and_returns_target_email(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            result = await check_email_change_link(_request(), Response(), "good", mock_db)

            assert result.valid is True
            assert result.email == "new@example.com"


class TestVerifyEmailChange:
    """`POST /user/email-change/verify`."""

    @pytest.mark.asyncio
    async def test_invalid_token_raises_unauthorized(self, mock_db):
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=None)

            with pytest.raises(UnauthorizedException, match="invalid"):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="bad"), mock_db)

    @pytest.mark.asyncio
    async def test_invalidated_token_raises_unauthorized(self, mock_db):
        """Superseded by a newer change request - a hard reject, unlike a merely
        already-used-but-still-live token (see the idempotent-reuse test below)."""
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            with pytest.raises(UnauthorizedException, match="no longer valid"):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="stale"), mock_db)

    @pytest.mark.asyncio
    async def test_already_used_but_live_token_is_idempotent(self, mock_db):
        """Re-opening the same confirmation link again (e.g. a mail client's
        link-preview/security-scanning feature having already applied it, or the
        user clicking twice) must not error - it's a no-op that just reports the
        change as already applied, without touching `crud_users` again."""
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": datetime.now(UTC),
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.users.crud_users") as mock_users,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(return_value={"id": 7, "email": "new@example.com"})
            stub_claim(mock_db)

            result = await verify_email_change(_request(), EmailChangeVerifyRequest(token="already-used"), mock_db)

            assert result.email == "new@example.com"
            mock_users.update.assert_not_called()
            mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_reused_token_with_mismatched_email_raises_unauthorized(self, mock_db):
        """A used token whose target email no longer matches the account's current
        one - e.g. the account's email changed again since - is a genuine, rejected
        reuse, not a harmless repeat."""
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": datetime.now(UTC),
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.users.crud_users") as mock_users,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(return_value={"id": 7, "email": "someone-else@example.com"})

            with pytest.raises(UnauthorizedException, match="already been used"):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="stale-used"), mock_db)

    @pytest.mark.asyncio
    async def test_losing_the_claim_reports_the_change_that_landed(self, mock_db):
        """Two verifications of the same still-live link race, and this one loses the
        conditional `used_at` UPDATE. Deliberately *not* the 401 sign-in's loser gets:
        the winner applied exactly the change this token asked for, so re-reading the
        account's email lands on the same tolerated-replay rule as an already-used row.
        The second `crud_users.get` is the read that happens after the winner committed."""
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.users.crud_users") as mock_users,
            patch("src.app.api.v1.users.send_email_changed_notification", new_callable=AsyncMock) as mock_notify,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(
                side_effect=[{"id": 7, "email": "old@example.com"}, {"id": 7, "email": "new@example.com"}]
            )
            mock_users.exists = AsyncMock(return_value=False)
            mock_users.update = AsyncMock(return_value=None)
            stub_claim(mock_db, won=False)

            result = await verify_email_change(_request(), EmailChangeVerifyRequest(token="raced"), mock_db)

            assert result.email == "new@example.com"
            mock_users.update.assert_not_called()
            # The winner already sent it; a second one would tell the old address twice.
            mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_losing_the_claim_rejects_when_the_account_moved_on(self, mock_db):
        """Same race, but the account's email is no longer what this token asked for -
        a later change request won instead. That is a genuine reuse, and the leniency
        above must not swallow it."""
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.users.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(
                side_effect=[{"id": 7, "email": "old@example.com"}, {"id": 7, "email": "newer@example.com"}]
            )
            mock_users.exists = AsyncMock(return_value=False)
            mock_users.update = AsyncMock(return_value=None)
            stub_claim(mock_db, won=False)

            with pytest.raises(UnauthorizedException, match="already been used"):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="raced"), mock_db)

            mock_users.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_losing_the_claim_to_a_deleted_account_is_not_found(self, mock_db):
        """Hard-deleting a user cascades to their `authentication_request` rows, so the
        claim can lose to the row simply going away. The re-read has to be checked rather
        than reusing the email fetched before it - reporting success for an account that
        no longer exists is the one answer that would be wrong."""
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.users.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(side_effect=[{"id": 7, "email": "old@example.com"}, None])
            mock_users.exists = AsyncMock(return_value=False)
            mock_users.update = AsyncMock(return_value=None)
            stub_claim(mock_db, won=False)

            with pytest.raises(NotFoundException):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="raced"), mock_db)

    @pytest.mark.asyncio
    async def test_expired_token_raises_unauthorized(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.users.crud_users") as mock_users,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(return_value={"id": 7, "email": "old@example.com"})

            with pytest.raises(UnauthorizedException, match="expired"):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="expired"), mock_db)

    @pytest.mark.asyncio
    async def test_user_not_found_raises_not_found(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.users.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(return_value=None)

            with pytest.raises(NotFoundException):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="good"), mock_db)

    @pytest.mark.asyncio
    async def test_new_email_already_taken_raises_duplicate(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "taken@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.users.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(return_value={"id": 7, "email": "old@example.com"})
            mock_users.exists = AsyncMock(return_value=True)

            with pytest.raises(DuplicateValueException):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="good"), mock_db)

    @pytest.mark.asyncio
    async def test_successful_change_updates_user_and_notifies_old_email(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }

        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.users.crud_users") as mock_users,
            patch("src.app.api.v1.users.send_email_changed_notification", new_callable=AsyncMock) as mock_notify,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(return_value={"id": 7, "email": "old@example.com"})
            mock_users.exists = AsyncMock(return_value=False)
            mock_users.update = AsyncMock(return_value=None)
            stub_claim(mock_db)

            result = await verify_email_change(_request(), EmailChangeVerifyRequest(token="good"), mock_db)

            assert result.email == "new@example.com"
            mock_users.update.assert_called_once_with(
                db=mock_db, object={"email": "new@example.com"}, id=7, commit=False
            )
            assert "used_at IS NULL" in claimed_used_at_sql(mock_db)
            # One transaction covering both writes: neither writer commits for itself, so
            # the token can never be spent without the email having moved with it.
            mock_db.commit.assert_awaited_once()
            mock_notify.assert_called_once_with(old_email="old@example.com", new_email="new@example.com")

    @pytest.mark.asyncio
    async def test_concurrent_change_rolls_back_and_raises_duplicate(self, mock_db):
        """Two verifications racing to claim the same email: whichever loses the DB's
        unique-constraint race must fail cleanly, not silently overwrite."""
        auth_request = {
            "id": 1,
            "email": "race@example.com",
            "user_id": 7,
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }

        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.users.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(return_value={"id": 7, "email": "old@example.com"})
            mock_users.exists = AsyncMock(return_value=False)
            mock_users.update = AsyncMock(side_effect=IntegrityError("update", {}, Exception("duplicate key")))
            mock_db.rollback = AsyncMock(return_value=None)
            stub_claim(mock_db)

            with pytest.raises(DuplicateValueException):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="good"), mock_db)

            mock_db.rollback.assert_called_once()


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestVerifyEmailChangeAgainstPostgres:
    """The half of `verify_email_change` a mocked session cannot judge: whether the two
    writes it makes actually land, and land together.

    Both now go through `commit=False` and share one `db.commit()`, so a mock can only
    confirm the arguments were spelled right - it will happily report success for a
    transaction that was never committed, or for one that committed half of itself. See
    *"A filter on a FastCRUD `update` is a `count()`, not an atomic condition"* in
    `DECISIONS.md` for what the pairing is protecting against.
    """

    @pytest.mark.asyncio
    async def test_the_email_and_the_used_stamp_commit_together(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        new_email = unique_email()
        raw_token = "e2e-change-" + uuid7().hex
        request_row = AuthenticationRequest(
            email=new_email,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            purpose="email_change",
            user_id=diver.id,
        )
        db.add(request_row)
        db.commit()

        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.send_email_changed_notification", new_callable=AsyncMock),
        ):
            result = await verify_email_change(_request(), EmailChangeVerifyRequest(token=raw_token), async_db)

        assert result.email == new_email

        # Read on a connection of its own - the sync session the fixtures use - so this
        # asserts against committed state rather than against `async_db`'s own view of an
        # open transaction.
        assert db.scalar(select(User.email).where(User.id == diver.id)) == new_email
        assert _used_at(db, request_row.id) is not None

    @pytest.mark.asyncio
    async def test_a_second_verification_reports_the_change_without_rewriting_it(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The tolerated replay, end to end: `used_at` keeps the timestamp the first
        verification wrote rather than being restamped."""
        new_email = unique_email()
        raw_token = "e2e-replay-" + uuid7().hex
        request_row = AuthenticationRequest(
            email=new_email,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            purpose="email_change",
            user_id=diver.id,
        )
        db.add(request_row)
        db.commit()

        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.send_email_changed_notification", new_callable=AsyncMock) as mock_notify,
        ):
            await verify_email_change(_request(), EmailChangeVerifyRequest(token=raw_token), async_db)
            first_stamp = _used_at(db, request_row.id)

            result = await verify_email_change(_request(), EmailChangeVerifyRequest(token=raw_token), async_db)

            assert result.email == new_email
            # Only the first verification tells the old address it moved.
            mock_notify.assert_called_once()

        assert _used_at(db, request_row.id) == first_stamp
