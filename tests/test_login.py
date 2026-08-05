"""Unit tests for the login API endpoints, including Google sign in/up."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from uuid6 import uuid7

from src.app.api.v1.login import _generate_unique_username, login_with_google
from src.app.core.exceptions.http_exceptions import UnauthorizedException
from src.app.core.schemas import GoogleAuthRequest, GoogleUserInfo


class TestGenerateUniqueUsername:
    """Test the username auto-generation helper used by Google sign-up."""

    @pytest.mark.asyncio
    async def test_returns_sanitized_base_when_available(self, mock_db):
        with patch("src.app.api.v1.login.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=False)

            username = await _generate_unique_username("Jane.Doe123", mock_db)

            assert username == "janedoe123"

    @pytest.mark.asyncio
    async def test_appends_suffix_on_collision(self, mock_db):
        with patch("src.app.api.v1.login.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(side_effect=[True, False])

            username = await _generate_unique_username("janedoe", mock_db)

            assert username == "janedoe1"
            assert mock_crud.exists.call_count == 2

    @pytest.mark.asyncio
    async def test_pads_too_short_base(self, mock_db):
        with patch("src.app.api.v1.login.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=False)

            username = await _generate_unique_username("a", mock_db)

            assert username == "auser"


class TestLoginWithGoogle:
    """Test the `/login/google` endpoint's find-or-create/sign in logic."""

    @pytest.mark.asyncio
    async def test_invalid_credential_raises_unauthorized(self, mock_db):
        with patch("src.app.api.v1.login.verify_google_id_token", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = None

            with pytest.raises(UnauthorizedException):
                await login_with_google(Mock(), GoogleAuthRequest(credential="bad"), mock_db)

    @pytest.mark.asyncio
    async def test_existing_google_user_signs_in(self, mock_db):
        google_user = GoogleUserInfo(google_id="g-123", email="user@example.com", name="Jane Doe")
        db_user = {"id": 1, "uuid": uuid7(), "username": "janedoe", "email": "user@example.com"}

        with (
            patch("src.app.api.v1.login.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.login.crud_users") as mock_crud,
        ):
            mock_verify.return_value = google_user
            mock_crud.get = AsyncMock(return_value=db_user)

            response = Mock()
            result = await login_with_google(response, GoogleAuthRequest(credential="good"), mock_db)

            assert result["token_type"] == "bearer"
            assert "access_token" in result
            mock_crud.get.assert_called_once_with(db=mock_db, google_id="g-123", is_deleted=False)
            response.set_cookie.assert_called_once()

    @pytest.mark.asyncio
    async def test_links_existing_email_account(self, mock_db):
        """A regular (password) account with a matching, Google-verified email gets
        `google_id` linked onto it rather than a duplicate account being created."""
        google_user = GoogleUserInfo(google_id="g-123", email="user@example.com", name="Jane Doe")
        existing_user = {"id": 1, "uuid": uuid7(), "username": "janedoe", "email": "user@example.com"}

        with (
            patch("src.app.api.v1.login.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.login.crud_users") as mock_crud,
        ):
            mock_verify.return_value = google_user
            mock_crud.get = AsyncMock(side_effect=[None, existing_user])
            mock_crud.update = AsyncMock(return_value=None)

            response = Mock()
            result = await login_with_google(response, GoogleAuthRequest(credential
="good"), mock_db)

            assert "access_token" in result
            mock_crud.update.assert_called_once_with(
                db=mock_db, object={"google_id": "g-123"}, uuid=existing_user["uuid"]
            )

    @pytest.mark.asyncio
    async def test_creates_new_user_when_none_found(self, mock_db):
        google_user = GoogleUserInfo(google_id="g-123", email="newperson@example.com", name="New Person")
        created_user_model = Mock(id=42)
        new_db_user = {"id": 42, "uuid": uuid7(), "username": "newperson", "email": "newperson@example.com"}

        with (
            patch("src.app.api.v1.login.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.login.crud_users") as mock_crud,
        ):
            mock_verify.return_value = google_user
            # get(google_id=...) -> None, get(email=...) -> None, get(id=...) -> new user
            mock_crud.get = AsyncMock(side_effect=[None, None, new_db_user])
            mock_crud.exists = AsyncMock(return_value=False)
            mock_crud.create = AsyncMock(return_value=created_user_model)

            response = Mock()
            result = await login_with_google(response, GoogleAuthRequest(credential="good"), mock_db)

            assert "access_token" in result
            mock_crud.create.assert_called_once()
            created_object = mock_crud.create.call_args.kwargs["object"]
            assert created_object.username == "newperson"
            assert created_object.email == "newperson@example.com"
            assert created_object.google_id == "g-123"
            assert created_object.hashed_password is None
