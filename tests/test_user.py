"""Unit tests for user API endpoints."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from uuid6 import uuid7

from src.app.api.v1.users import erase_user, patch_user, read_user, read_users
from src.app.core.exceptions.http_exceptions import ForbiddenException, NotFoundException
from src.app.schemas.user import UserRead, UserUpdate

# Note: there is no `POST /user` endpoint to test here anymore - account creation only
# happens via `POST /auth/complete` (see `tests/test_auth.py`).


class TestReadUser:
    """Test user retrieval endpoint."""

    @pytest.mark.asyncio
    async def test_read_user_success(self, mock_db, sample_user_read):
        """Test successful user retrieval."""
        user_uuid = sample_user_read.uuid

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=sample_user_read)

            result = await read_user(Mock(), user_uuid, mock_db)

            assert result == sample_user_read
            mock_crud.get.assert_called_once_with(
                db=mock_db, uuid=user_uuid, is_deleted=False, schema_to_select=UserRead, return_as_model=True
            )

    @pytest.mark.asyncio
    async def test_read_user_not_found(self, mock_db):
        """Test user retrieval when user doesn't exist."""
        user_uuid = uuid7()

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=None)

            with pytest.raises(NotFoundException, match="User not found"):
                await read_user(Mock(), user_uuid, mock_db)


class TestReadUsers:
    """Test users list endpoint."""

    @pytest.mark.asyncio
    async def test_read_users_success(self, mock_db):
        """Test successful users list retrieval."""
        mock_users_data = {"data": [{"id": 1}, {"id": 2}], "count": 2}

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.get_multi = AsyncMock(return_value=mock_users_data)

            with patch("src.app.api.v1.users.paginated_response") as mock_paginated:
                expected_response = {"data": [{"id": 1}, {"id": 2}], "pagination": {}}
                mock_paginated.return_value = expected_response

                result = await read_users(Mock(), mock_db, page=1, items_per_page=10)

                assert result == expected_response
                mock_crud.get_multi.assert_called_once()
                mock_paginated.assert_called_once()


class TestPatchUser:
    """Test user update endpoint."""

    @pytest.mark.asyncio
    async def test_patch_user_success(self, mock_db, current_user_dict, sample_user_read):
        """Test successful user update."""
        user_uuid = current_user_dict["uuid"]
        user_update = UserUpdate(name="New Name")

        user_dict = sample_user_read.model_dump()
        user_dict["uuid"] = user_uuid

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=user_dict)
            mock_crud.exists = AsyncMock(return_value=False)
            mock_crud.update = AsyncMock(return_value=None)

            result = await patch_user(Mock(), user_update, user_uuid, current_user_dict, mock_db)

            assert result == {"message": "User updated"}
            mock_crud.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_patch_user_forbidden(self, mock_db, current_user_dict, sample_user_read):
        """Test user update when user tries to update another user."""
        other_user_uuid = uuid7()
        user_update = UserUpdate(name="New Name")
        user_dict = sample_user_read.model_dump()
        user_dict["uuid"] = other_user_uuid

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=user_dict)

            with pytest.raises(ForbiddenException):
                await patch_user(Mock(), user_update, other_user_uuid, current_user_dict, mock_db)


class TestEraseUser:
    """Test user deletion endpoint."""

    @pytest.mark.asyncio
    async def test_erase_user_success(self, mock_db, current_user_dict, sample_user_read):
        """Test successful user deletion blacklists both tokens and clears the refresh cookie
        when a refresh token is present."""
        user_uuid = current_user_dict["uuid"]
        sample_user_read.uuid = user_uuid
        access_token = "mock_access_token"
        refresh_token = "mock_refresh_token"
        mock_response = Mock()

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=sample_user_read)
            mock_crud.delete = AsyncMock(return_value=None)

            with patch("src.app.api.v1.users.blacklist_tokens", new_callable=AsyncMock) as mock_blacklist_tokens:
                result = await erase_user(
                    request=Mock(),
                    response=mock_response,
                    uuid=user_uuid,
                    current_user=current_user_dict,
                    db=mock_db,
                    access_token=access_token,
                    refresh_token=refresh_token,
                )

                assert result == {"message": "User deleted"}
                mock_crud.delete.assert_called_once_with(db=mock_db, uuid=user_uuid)
                mock_blacklist_tokens.assert_called_once_with(
                    access_token=access_token, refresh_token=refresh_token, db=mock_db
                )
                mock_response.delete_cookie.assert_called_once_with(key="refresh_token")

    @pytest.mark.asyncio
    async def test_erase_user_success_without_refresh_token(self, mock_db, current_user_dict, sample_user_read):
        """Test user deletion falls back to blacklisting just the access token when no
        refresh token cookie is present."""
        user_uuid = current_user_dict["uuid"]
        sample_user_read.uuid = user_uuid
        access_token = "mock_access_token"

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=sample_user_read)
            mock_crud.delete = AsyncMock(return_value=None)

            with patch("src.app.api.v1.users.blacklist_token", new_callable=AsyncMock) as mock_blacklist_token:
                result = await erase_user(
                    request=Mock(),
                    response=Mock(),
                    uuid=user_uuid,
                    current_user=current_user_dict,
                    db=mock_db,
                    access_token=access_token,
                    refresh_token=None,
                )

                assert result == {"message": "User deleted"}
                mock_blacklist_token.assert_called_once_with(token=access_token, db=mock_db)

    @pytest.mark.asyncio
    async def test_erase_user_not_found(self, mock_db, current_user_dict):
        """Test user deletion when user doesn't exist."""
        user_uuid = uuid7()

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=None)

            with pytest.raises(NotFoundException, match="User not found"):
                await erase_user(
                    request=Mock(),
                    response=Mock(),
                    uuid=user_uuid,
                    current_user=current_user_dict,
                    db=mock_db,
                    access_token="mock_token",
                    refresh_token=None,
                )

    @pytest.mark.asyncio
    async def test_erase_user_forbidden(self, mock_db, current_user_dict, sample_user_read):
        """Test user deletion when user tries to delete another user."""
        other_user_uuid = uuid7()
        sample_user_read.uuid = other_user_uuid

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=sample_user_read)

            with pytest.raises(ForbiddenException):
                await erase_user(
                    request=Mock(),
                    response=Mock(),
                    uuid=other_user_uuid,
                    current_user=current_user_dict,
                    db=mock_db,
                    access_token="mock_token",
                    refresh_token=None,
                )
