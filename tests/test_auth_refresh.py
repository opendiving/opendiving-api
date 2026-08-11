"""Unit tests for the `POST /auth/refresh` endpoint (now part of `api.v1.auth` - see
`tests/test_auth.py` for the unified email/Google/onboarding flow, and `DECISIONS.md`
for why `/refresh`/`/logout` moved under `/auth`, and for why the presented refresh
token is rotated rather than reused).
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.app.api.v1.auth import refresh_access_token
from src.app.core.exceptions.http_exceptions import UnauthorizedException
from src.app.core.schemas import TokenData


def _request(cookies: dict[str, str]) -> Mock:
    request = Mock()
    request.cookies = cookies
    request.client.host = "203.0.113.7"
    return request


class TestRefreshAccessToken:
    @pytest.mark.asyncio
    async def test_missing_cookie_raises_unauthorized(self, mock_db):
        with pytest.raises(UnauthorizedException, match="Refresh token missing."):
            await refresh_access_token(_request({}), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_invalid_refresh_token_raises_unauthorized(self, mock_db):
        with patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = None

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": "bad-token"}), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_invalid_refresh_token_is_not_blacklisted(self, mock_db):
        """A token that didn't verify must not be written to the blacklist table - that
        would let anyone grow the table by posting garbage cookies.
        """
        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock) as mock_blacklist,
        ):
            mock_verify.return_value = None

            with pytest.raises(UnauthorizedException):
                await refresh_access_token(_request({"refresh_token": "bad-token"}), Mock(), mock_db)

            mock_blacklist.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_refresh_token_returns_new_access_token(self, mock_db):
        response = Mock()

        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as mock_issue,
        ):
            mock_verify.return_value = TokenData(username_or_email="someuser")
            mock_issue.return_value = {"access_token": "new-access-token", "token_type": "bearer"}

            result = await refresh_access_token(_request({"refresh_token": "good-token"}), response, mock_db)

            assert result == {"access_token": "new-access-token", "token_type": "bearer"}
            # A fresh refresh cookie is set on the same response, not just an access token.
            mock_issue.assert_called_once_with(response, "someuser")

    @pytest.mark.asyncio
    async def test_presented_refresh_token_is_rotated_out(self, mock_db):
        """The whole point of rotation: the cookie that was handed in is spent, so a
        replay of the same value fails even though it hasn't reached its `exp` yet.
        """
        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock) as mock_blacklist,
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as mock_issue,
        ):
            mock_verify.return_value = TokenData(username_or_email="someuser")
            mock_issue.return_value = {"access_token": "new-access-token", "token_type": "bearer"}

            await refresh_access_token(_request({"refresh_token": "good-token"}), Mock(), mock_db)

            mock_blacklist.assert_called_once_with("good-token", mock_db)
