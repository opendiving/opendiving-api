"""Unit tests for user API endpoints."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.app.api.v1.users import erase_user, patch_user, read_current_user
from src.app.schemas.user import UserUpdate

# Note: there is no `POST /user` endpoint to test here anymore - account creation only
# happens via `POST /auth/complete` (see `tests/test_auth.py`).

# Note: there is no `GET /user/{uuid}` or `GET /users` endpoint to test here (yet) -
# looking up *other* users (individually or as a list) will be added later as a
# separate public-profile endpoint.


class TestReadCurrentUser:
    """Test current-user retrieval endpoint."""

    @pytest.mark.asyncio
    async def test_read_current_user_success(self, current_user_dict):
        """`GET /user` just returns whatever `get_current_user` resolved from the token."""
        result = await read_current_user(Mock(), current_user_dict)

        assert result == current_user_dict


class TestPatchUser:
    """Test user update endpoint."""

    @pytest.mark.asyncio
    async def test_patch_user_success(self, mock_db, current_user_dict):
        """Test successful user update - always operates on the caller's own account."""
        user_update = UserUpdate(name="New Name")

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=False)
            mock_crud.update = AsyncMock(return_value=None)

            result = await patch_user(Mock(), user_update, current_user_dict, mock_db)

            assert result == {"message": "User updated"}
            mock_crud.update.assert_called_once_with(
                db=mock_db, object=user_update, uuid=current_user_dict["uuid"]
            )

    @pytest.mark.asyncio
    async def test_patch_user_duplicate_username(self, mock_db, current_user_dict):
        """Test user update when the requested username is already taken."""
        user_update = UserUpdate(username="taken")

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=True)

            from src.app.core.exceptions.http_exceptions import DuplicateValueException

            with pytest.raises(DuplicateValueException):
                await patch_user(Mock(), user_update, current_user_dict, mock_db)


class TestEraseUser:
    """Test user deletion endpoint - always operates on the caller's own account."""

    @pytest.mark.asyncio
    async def test_erase_user_success(self, mock_db, current_user_dict):
        """Test successful user deletion blacklists both tokens and clears the refresh cookie
        when a refresh token is present."""
        access_token = "mock_access_token"
        refresh_token = "mock_refresh_token"
        mock_response = Mock()

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.delete = AsyncMock(return_value=None)

            with patch("src.app.api.v1.users.blacklist_tokens", new_callable=AsyncMock) as mock_blacklist_tokens:
                result = await erase_user(
                    request=Mock(),
                    response=mock_response,
                    current_user=current_user_dict,
                    db=mock_db,
                    access_token=access_token,
                    refresh_token=refresh_token,
                )

                assert result == {"message": "User deleted"}
                mock_crud.delete.assert_called_once_with(db=mock_db, uuid=current_user_dict["uuid"])
                mock_blacklist_tokens.assert_called_once_with(
                    access_token=access_token, refresh_token=refresh_token, db=mock_db
                )
                mock_response.delete_cookie.assert_called_once_with(key="refresh_token")

    @pytest.mark.asyncio
    async def test_erase_user_success_without_refresh_token(self, mock_db, current_user_dict):
        """Test user deletion falls back to blacklisting just the access token when no
        refresh token cookie is present."""
        access_token = "mock_access_token"

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.delete = AsyncMock(return_value=None)

            with patch("src.app.api.v1.users.blacklist_token", new_callable=AsyncMock) as mock_blacklist_token:
                result = await erase_user(
                    request=Mock(),
                    response=Mock(),
                    current_user=current_user_dict,
                    db=mock_db,
                    access_token=access_token,
                    refresh_token=None,
                )

                assert result == {"message": "User deleted"}
                mock_blacklist_token.assert_called_once_with(token=access_token, db=mock_db)
