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
    verify_google_id_token,
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

    @pytest.mark.asyncio
    async def test_authenticate_google_only_account_returns_false(self, mock_db):
        """A Google-only account (see `verify_google_id_token`) has no password to check
        against - password sign-in must fail rather than crash on a `None` hash."""
        db_user = {"username": "someuser", "email": "user@example.com", "hashed_password": None}

        with patch("src.app.core.security.crud_users") as mock_crud:
            mock_crud.get = AsyncMock(return_value=db_user)

            result = await authenticate_user("someuser", "any-password", mock_db)

            assert result is False


class TestVerifyGoogleIdToken:
    """Test the Google ID token verification helper backing `/login/google`."""

    @pytest.mark.asyncio
    async def test_returns_none_when_client_id_not_configured(self):
        with patch("src.app.core.security.settings") as mock_settings:
            mock_settings.GOOGLE_CLIENT_ID = None

            result = await verify_google_id_token("some-credential")

            assert result is None

    @pytest.mark.asyncio
    async def test_returns_user_info_for_valid_token(self):
        payload = {
            "sub": "google-123",
            "email": "user@example.com",
            "email_verified": True,
            "name": "Jane Doe",
        }

        with (
            patch("src.app.core.security.settings") as mock_settings,
            patch("src.app.core.security.google_id_token.verify_oauth2_token") as mock_verify,
        ):
            mock_settings.GOOGLE_CLIENT_ID = "client-id"
            mock_verify.return_value = payload

            result = await verify_google_id_token("good-credential")

            assert result is not None
            assert result.google_id == "google-123"
            assert result.email == "user@example.com"
            assert result.name == "Jane Doe"

    @pytest.mark.asyncio
    async def test_returns_none_for_unverified_email(self):
        payload = {"sub": "google-123", "email": "user@example.com", "email_verified": False, "name": "Jane Doe"}

        with (
            patch("src.app.core.security.settings") as mock_settings,
            patch("src.app.core.security.google_id_token.verify_oauth2_token") as mock_verify,
        ):
            mock_settings.GOOGLE_CLIENT_ID = "client-id"
            mock_verify.return_value = payload

            result = await verify_google_id_token("good-credential")

            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_invalid_token(self):
        with (
            patch("src.app.core.security.settings") as mock_settings,
            patch("src.app.core.security.google_id_token.verify_oauth2_token") as mock_verify,
        ):
            mock_settings.GOOGLE_CLIENT_ID = "client-id"
            mock_verify.side_effect = ValueError("bad token")

            result = await verify_google_id_token("bad-credential")

            assert result is None

    @pytest.mark.asyncio
    async def test_falls_back_to_email_local_part_when_name_missing(self):
        payload = {"sub": "google-123", "email": "jane@example.com", "email_verified": True}

        with (
            patch("src.app.core.security.settings") as mock_settings,
            patch("src.app.core.security.google_id_token.verify_oauth2_token") as mock_verify,
        ):
            mock_settings.GOOGLE_CLIENT_ID = "client-id"
            mock_verify.return_value = payload

            result = await verify_google_id_token("good-credential")

            assert result is not None
            assert result.name == "jane"


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
