"""Unit tests for the email-change confirmation flow (`api.v1.users`): requesting a
change requires confirming ownership of the new address via a magic link before it
takes effect - directly setting `email` via `PATCH /user/{uuid}` is no longer possible
(see `schemas.user.UserUpdate`).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from src.app.api.v1.users import request_email_change, verify_email_change
from src.app.core.exceptions.http_exceptions import (
    BadRequestException,
    DuplicateValueException,
    ForbiddenException,
    NotFoundException,
    RateLimitException,
    UnauthorizedException,
)
from src.app.schemas.email_change import EmailChangeRequest, EmailChangeVerifyRequest


def _request(ip: str = "1.2.3.4") -> Mock:
    request = Mock()
    request.client = Mock(host=ip)
    return request


class TestRequestEmailChange:
    """`POST /user/{uuid}/email-change/request`."""

    @pytest.mark.asyncio
    async def test_forbidden_for_another_users_uuid(self, mock_db, current_user_dict):
        from uuid6 import uuid7

        other_uuid = uuid7()

        with pytest.raises(ForbiddenException):
            await request_email_change(
                _request(),
                other_uuid,
                EmailChangeRequest(new_email="new@example.com"),
                current_user_dict,
                mock_db,
            )

    @pytest.mark.asyncio
    async def test_rejects_unchanged_email(self, mock_db, current_user_dict):
        with pytest.raises(BadRequestException):
            await request_email_change(
                _request(),
                current_user_dict["uuid"],
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
                    current_user_dict["uuid"],
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
                current_user_dict["uuid"],
                EmailChangeRequest(new_email="new@example.com"),
                current_user_dict,
                mock_db,
            )

            mock_crud.update.assert_called_once()
            kwargs = mock_crud.update.call_args.kwargs
            assert kwargs["allow_multiple"] is True
            assert kwargs["user_id"] == current_user_dict["id"]
            assert kwargs["purpose"] == "email_change"
            assert kwargs["used_at"] is None

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
                current_user_dict["uuid"],
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
                current_user_dict["uuid"],
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
    async def test_reused_token_raises_unauthorized(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            with pytest.raises(UnauthorizedException, match="already been used"):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="used"), mock_db)

    @pytest.mark.asyncio
    async def test_expired_token_raises_unauthorized(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
        }
        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            with pytest.raises(UnauthorizedException, match="expired"):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="expired"), mock_db)

    @pytest.mark.asyncio
    async def test_user_not_found_raises_not_found(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "user_id": 7,
            "used_at": None,
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
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }

        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.users.crud_users") as mock_users,
            patch("src.app.api.v1.users.send_email_changed_notification", new_callable=AsyncMock) as mock_notify,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_requests.update = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value={"id": 7, "email": "old@example.com"})
            mock_users.exists = AsyncMock(return_value=False)
            mock_users.update = AsyncMock(return_value=None)

            result = await verify_email_change(_request(), EmailChangeVerifyRequest(token="good"), mock_db)

            assert result.email == "new@example.com"
            mock_users.update.assert_called_once_with(db=mock_db, object={"email": "new@example.com"}, id=7)
            mock_requests.update.assert_called_once()
            assert mock_requests.update.call_args.kwargs["id"] == 1
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

            with pytest.raises(DuplicateValueException):
                await verify_email_change(_request(), EmailChangeVerifyRequest(token="good"), mock_db)

            mock_db.rollback.assert_called_once()
