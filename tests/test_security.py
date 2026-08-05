"""Unit tests for the auth/security helpers."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from jose import jwt

from src.app.core.security import (
    ALGORITHM,
    SECRET_KEY,
    TokenType,
    authenticate_user,
    blacklist_token,
    blacklist_tokens,
    create_access_token,
    create_refresh_token,
    get_password_hash,
    verify_password,
    verify_token,
)


class TestPasswordHashing:
    """Test password hashing and verification."""

    @pytest.mark.asyncio
    async def test_hash_and_verify_roundtrip(self):
        """A password hashed with get_password_hash should verify successfully."""
        password = "S3cur3P@ssword!"
        hashed = get_password_hash(password)

        assert hashed != password
        assert await verify_password(password, hashed) is True

    @pytest.mark.asyncio
    async def test_verify_wrong_password_fails(self):
        """A different password should not verify against an existing hash."""
        hashed = get_password_hash("correct-password")

        assert await verify_password("wrong-password", hashed) is False

    def test_hash_is_not_deterministic(self):
        """Hashing the same password twice should produce different hashes (unique salt)."""
        password = "same-password"

        assert get_password_hash(password) != get_password_hash(password)


class TestTokenCreation:
    """Test JWT access/refresh token creation."""

    @pytest.mark.asyncio
    async def test_create_access_token_contains_expected_claims(self):
        token = await create_access_token({"sub": "someuser"})
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])

        assert payload["sub"] == "someuser"
        assert payload["token_type"] == TokenType.ACCESS
        assert "exp" in payload

    @pytest.mark.asyncio
    async def test_create_refresh_token_contains_expected_claims(self):
        token = await create_refresh_token({"sub": "someuser"})
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])

        assert payload["sub"] == "someuser"
        assert payload["token_type"] == TokenType.REFRESH
        assert "exp" in payload

    @pytest.mark.asyncio
    async def test_create_access_token_respects_custom_expiry(self):
        expires_delta = timedelta(minutes=5)
        before = datetime.now(UTC).replace(tzinfo=None)

        token = await create_access_token({"sub": "someuser"}, expires_delta=expires_delta)
        payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
        exp = datetime.fromtimestamp(payload["exp"], tz=UTC).replace(tzinfo=None)

        assert before + timedelta(minutes=4) < exp <= before + timedelta(minutes=5, seconds=1)


class TestVerifyToken:
    """Test JWT token verification."""

    @pytest.mark.asyncio
    async def test_verify_valid_access_token(self, mock_db):
        token = await create_access_token({"sub": "someuser"})

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=False)

            token_data = await verify_token(token, TokenType.ACCESS, mock_db)

            assert token_data is not None
            assert token_data.username_or_email == "someuser"

    @pytest.mark.asyncio
    async def test_verify_token_wrong_type_returns_none(self, mock_db):
        """A refresh token presented where an access token is expected should fail."""
        token = await create_refresh_token({"sub": "someuser"})

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=False)

            token_data = await verify_token(token, TokenType.ACCESS, mock_db)

            assert token_data is None

    @pytest.mark.asyncio
    async def test_verify_blacklisted_token_returns_none(self, mock_db):
        token = await create_access_token({"sub": "someuser"})

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=True)

            token_data = await verify_token(token, TokenType.ACCESS, mock_db)

            assert token_data is None

    @pytest.mark.asyncio
    async def test_verify_malformed_token_returns_none(self, mock_db):
        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=False)

            token_data = await verify_token("not-a-valid-jwt", TokenType.ACCESS, mock_db)

            assert token_data is None

    @pytest.mark.asyncio
    async def test_verify_expired_token_returns_none(self, mock_db):
        token = await create_access_token({"sub": "someuser"}, expires_delta=timedelta(minutes=-5))

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=False)

            token_data = await verify_token(token, TokenType.ACCESS, mock_db)

            assert token_data is None


class TestAuthenticateUser:
    """Test the authenticate_user helper."""

    @pytest.mark.asyncio
    async def test_authenticate_with_email_success(self, mock_db):
        password = "correct-password"
        db_user = {"username": "someuser", "email": "user@example.com", "hashed_password": get_password_hash(password)}

        with patch("src.app.core.security.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=db_user)

            result = await authenticate_user("user@example.com", password, mock_db)

            assert result == db_user
            mock_crud.get.assert_called_once_with(db=mock_db, email="user@example.com", is_deleted=False)

    @pytest.mark.asyncio
    async def test_authenticate_with_username_success(self, mock_db):
        password = "correct-password"
        db_user = {"username": "someuser", "email": "user@example.com", "hashed_password": get_password_hash(password)}

        with patch("src.app.core.security.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=db_user)

            result = await authenticate_user("someuser", password, mock_db)

            assert result == db_user
            mock_crud.get.assert_called_once_with(db=mock_db, username="someuser", is_deleted=False)

    @pytest.mark.asyncio
    async def test_authenticate_unknown_user_returns_false(self, mock_db):
        with patch("src.app.core.security.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=None)

            result = await authenticate_user("nobody@example.com", "any-password", mock_db)

            assert result is False

    @pytest.mark.asyncio
    async def test_authenticate_wrong_password_returns_false(self, mock_db):
        db_user = {"username": "someuser", "email": "user@example.com", "hashed_password": get_password_hash("correct")}

        with patch("src.app.core.security.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=db_user)

            result = await authenticate_user("someuser", "wrong-password", mock_db)

            assert result is False


class TestBlacklistToken:
    """Test token blacklisting helpers."""

    @pytest.mark.asyncio
    async def test_blacklist_token_creates_blacklist_entry(self, mock_db):
        token = await create_access_token({"sub": "someuser"})

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.create = AsyncMock(return_value=None)

            await blacklist_token(token, mock_db)

            mock_blacklist.create.assert_called_once()
            _, kwargs = mock_blacklist.create.call_args
            assert kwargs["object"].token == token

    @pytest.mark.asyncio
    async def test_blacklist_tokens_blacklists_both(self, mock_db):
        access_token = await create_access_token({"sub": "someuser"})
        refresh_token = await create_refresh_token({"sub": "someuser"})

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.create = AsyncMock(return_value=None)

            await blacklist_tokens(access_token, refresh_token, mock_db)

            assert mock_blacklist.create.call_count == 2
            blacklisted_tokens = {call.kwargs["object"].token for call in mock_blacklist.create.call_args_list}
            assert blacklisted_tokens == {access_token, refresh_token}
