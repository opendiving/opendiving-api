"""Unit tests for the `POST /auth/refresh` endpoint (now part of `api.v1.auth` - see
`tests/test_auth.py` for the unified email/Google/onboarding flow, and `DECISIONS.md`
for why `/refresh`/`/logout` moved under `/auth`).
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.app.api.v1.auth import refresh_access_token
from src.app.core.exceptions.http_exceptions import UnauthorizedException
from src.app.core.schemas import TokenData


class TestRefreshAccessToken:
    @pytest.mark.asyncio
    async def test_missing_cookie_raises_unauthorized(self, mock_db):
        request = Mock()
        request.cookies = {}

        with pytest.raises(UnauthorizedException, match="Refresh token missing."):
            await refresh_access_token(request, mock_db)

    @pytest.mark.asyncio
    async def test_invalid_refresh_token_raises_unauthorized(self, mock_db):
        request = Mock()
        request.cookies = {"refresh_token": "bad-token"}

        with patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = None

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(request, mock_db)

    @pytest.mark.asyncio
    async def test_valid_refresh_token_returns_new_access_token(self, mock_db):
        request = Mock()
        request.cookies = {"refresh_token": "good-token"}

        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.create_access_token", new_callable=AsyncMock) as mock_create,
        ):
            mock_verify.return_value = TokenData(username_or_email="someuser")
            mock_create.return_value = "new-access-token"

            result = await refresh_access_token(request, mock_db)

            assert result == {"access_token": "new-access-token", "token_type": "bearer"}
            mock_create.assert_called_once_with(data={"sub": "someuser"})
