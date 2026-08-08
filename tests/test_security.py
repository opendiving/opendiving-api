"""Unit tests for the auth/security helpers."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from jose import jwt

from src.app.core.schemas import GoogleUserInfo, OnboardingTokenData
from src.app.core.security import (
    ALGORITHM,
    SECRET_KEY,
    TokenType,
    blacklist_token,
    blacklist_tokens,
    create_access_token,
    create_onboarding_token,
    create_refresh_token,
    generate_secure_token,
    hash_token,
    verify_google_id_token,
    verify_onboarding_token,
    verify_token,
)


class TestMagicLinkTokens:
    """Test the magic-link token generation/hashing helpers."""

    def test_generate_secure_token_is_url_safe_and_high_entropy(self):
        token = generate_secure_token()

        assert isinstance(token, str)
        assert len(token) >= 32
        # url-safe base64 alphabet only
        assert all(c.isalnum() or c in "-_" for c in token)

    def test_generate_secure_token_is_not_deterministic(self):
        assert generate_secure_token() != generate_secure_token()

    def test_hash_token_is_deterministic(self):
        token = generate_secure_token()

        assert hash_token(token) == hash_token(token)

    def test_hash_token_differs_for_different_tokens(self):
        assert hash_token(generate_secure_token()) != hash_token(generate_secure_token())

    def test_hash_token_does_not_return_the_raw_token(self):
        token = generate_secure_token()

        assert hash_token(token) != token


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


class TestOnboardingTokens:
    """Test the temporary onboarding-session JWT helpers backing `POST /auth/complete`."""

    @pytest.mark.asyncio
    async def test_create_and_verify_roundtrip(self, mock_db):
        data = OnboardingTokenData(
            email="new@example.com", provider="google", provider_user_id="g-1", name="New Person", avatar=None
        )

        token = await create_onboarding_token(data)

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=False)

            result = await verify_onboarding_token(token, mock_db)

            assert result == data

    @pytest.mark.asyncio
    async def test_verify_rejects_blacklisted_token(self, mock_db):
        data = OnboardingTokenData(email="new@example.com", provider="email")
        token = await create_onboarding_token(data)

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=True)

            result = await verify_onboarding_token(token, mock_db)

            assert result is None

    @pytest.mark.asyncio
    async def test_verify_rejects_expired_token(self, mock_db):
        data = OnboardingTokenData(email="new@example.com", provider="email")

        with patch("src.app.core.security.settings") as mock_settings:
            mock_settings.ONBOARDING_TOKEN_EXPIRE_MINUTES = -5
            token = await create_onboarding_token(data)

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=False)

            result = await verify_onboarding_token(token, mock_db)

            assert result is None

    @pytest.mark.asyncio
    async def test_verify_rejects_wrong_token_type(self, mock_db):
        """An access token presented as an onboarding token should be rejected."""
        token = await create_access_token({"sub": "someuser"})

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=False)

            result = await verify_onboarding_token(token, mock_db)

            assert result is None

    @pytest.mark.asyncio
    async def test_verify_rejects_malformed_token(self, mock_db):
        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=False)

            result = await verify_onboarding_token("not-a-valid-jwt", mock_db)

            assert result is None


class TestVerifyGoogleIdToken:
    """Test the Google ID token verification helper backing `POST /auth/google`."""

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
            "picture": "https://example.com/avatar.png",
        }

        with (
            patch("src.app.core.security.settings") as mock_settings,
            patch("src.app.core.security.google_id_token.verify_oauth2_token") as mock_verify,
        ):
            mock_settings.GOOGLE_CLIENT_ID = "client-id"
            mock_verify.return_value = payload

            result = await verify_google_id_token("good-credential")

            assert result == GoogleUserInfo(
                google_id="google-123",
                email="user@example.com",
                name="Jane Doe",
                avatar="https://example.com/avatar.png",
            )

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
            assert result.avatar is None


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
