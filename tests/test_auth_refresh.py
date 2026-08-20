"""Unit tests for the `POST /auth/refresh` endpoint (now part of `api.v1.auth` - see
`tests/test_auth.py` for the unified email/Google/onboarding flow, and `DECISIONS.md`
for why `/refresh`/`/logout` moved under `/auth`, and for why the presented refresh
token is rotated rather than reused).
"""

import logging
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import Response

from src.app.api.v1.auth import _elapsed, refresh_access_token
from src.app.core.exceptions.http_exceptions import UnauthorizedException
from src.app.core.schemas import TokenData
from src.app.services.auth_service import issue_tokens
from tests.helpers.mocks import FakeTokenBlacklist, FrozenSecurityClock

USER_UUID = uuid_pkg.uuid4()
AUTH_LOGGER = "src.app.api.v1.auth"


def _request(cookies: dict[str, str]) -> Mock:
    request = Mock()
    request.cookies = cookies
    request.client.host = "203.0.113.7"
    return request


def _refresh_cookie(response: Response) -> str:
    """Reads the refresh token back out of `Set-Cookie`, the way the browser will.

    `issue_tokens` returns only the access token - the refresh token exists solely as an
    httpOnly cookie on the response, so this is the only way to get at the value the next
    `/auth/refresh` will present.
    """
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        jar.load(header)
    return jar["refresh_token"].value


class TestRefreshAccessToken:
    @pytest.mark.asyncio
    async def test_missing_cookie_raises_unauthorized(self, mock_db):
        with pytest.raises(UnauthorizedException, match="Refresh token missing."):
            await refresh_access_token(_request({}), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_invalid_refresh_token_raises_unauthorized(self, mock_db):
        with (
            # The failure path asks the blacklist whether this token was one we revoked
            # (see `_warn_if_revoked`), so it needs somewhere to ask even when the answer
            # is "never seen it" - `mock_db` alone can't answer a real query.
            patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()),
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
        ):
            mock_verify.return_value = None

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": "bad-token"}), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_invalid_refresh_token_is_not_blacklisted(self, mock_db):
        """A token that didn't verify must not be written to the blacklist table - that
        would let anyone grow the table by posting garbage cookies.
        """
        with (
            patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()),
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
            mock_verify.return_value = TokenData(user_uuid=USER_UUID)
            mock_issue.return_value = {"access_token": "new-access-token", "token_type": "bearer"}

            result = await refresh_access_token(_request({"refresh_token": "good-token"}), response, mock_db)

            assert result == {"access_token": "new-access-token", "token_type": "bearer"}
            # A fresh refresh cookie is set on the same response, not just an access token.
            # The replacement carries the presented token's subject through unchanged -
            # which is safe only because that subject is an immutable uuid.
            mock_issue.assert_called_once_with(response, USER_UUID)

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
            mock_verify.return_value = TokenData(user_uuid=USER_UUID)
            mock_issue.return_value = {"access_token": "new-access-token", "token_type": "bearer"}

            await refresh_access_token(_request({"refresh_token": "good-token"}), Mock(), mock_db)

            mock_blacklist.assert_called_once_with("good-token", mock_db)


class TestRefreshRotationWithRealTokens:
    """Rotation driven end to end over the real helpers: tokens are genuinely minted,
    verified and blacklisted, with only the blacklist *table* swapped for an in-memory
    set.

    The tests above mock `verify_token`/`blacklist_token`/`issue_tokens` out, which is
    precisely how the one-second collision survived them - every assertion held against
    tokens that were never actually minted, so the endpoint handing back a token it had
    just revoked was invisible. These run with the clock frozen, so *every* token in the
    exchange shares one `exp`.
    """

    @pytest.mark.asyncio
    async def test_rotation_hands_back_a_token_that_is_not_already_revoked(self, mock_db):
        """The reported bug: sign in, refresh once, and the replacement cookie was the
        token just blacklisted - so the *next* refresh 401'd and the user landed on
        /signin for no reason they could see.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            sign_in = Response()
            await issue_tokens(sign_in, USER_UUID)
            original = _refresh_cookie(sign_in)

            rotated_response = Response()
            await refresh_access_token(_request({"refresh_token": original}), rotated_response, mock_db)
            rotated = _refresh_cookie(rotated_response)

            assert rotated != original
            assert rotated not in blacklist.tokens

            # The replacement has to survive being presented, which is what actually
            # failed: the second refresh is the one the user saw as a random logout.
            second_rotation = Response()
            result = await refresh_access_token(_request({"refresh_token": rotated}), second_rotation, mock_db)

            assert result["token_type"] == "bearer"
            assert _refresh_cookie(second_rotation) not in (original, rotated)

    @pytest.mark.asyncio
    async def test_spent_refresh_token_cannot_be_reused(self, mock_db):
        """The other half of rotation: replaying the cookie that was already exchanged
        fails, even though it is nowhere near its `exp`.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            sign_in = Response()
            await issue_tokens(sign_in, USER_UUID)
            original = _refresh_cookie(sign_in)

            await refresh_access_token(_request({"refresh_token": original}), Response(), mock_db)

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": original}), Response(), mock_db)

    @pytest.mark.asyncio
    async def test_concurrent_sign_ins_issue_independent_sessions(self, mock_db):
        """Two sign-ins for one account in the same second are two sessions: refreshing
        (and so revoking) one must leave the other usable. Identical tokens made them one
        session that either could end.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            first_device, second_device = Response(), Response()
            first_tokens = await issue_tokens(first_device, USER_UUID)
            second_tokens = await issue_tokens(second_device, USER_UUID)

            assert first_tokens["access_token"] != second_tokens["access_token"]

            await refresh_access_token(_request({"refresh_token": _refresh_cookie(first_device)}), Response(), mock_db)

            # The second device never refreshed, so its cookie is untouched.
            still_valid = Response()
            second_cookie = _refresh_cookie(second_device)
            await refresh_access_token(_request({"refresh_token": second_cookie}), still_valid, mock_db)

            assert _refresh_cookie(still_valid) not in blacklist.tokens


class TestRefreshTokenReuseLogging:
    """Presenting a refresh token that was already spent is the strongest evidence this
    app gets that a cookie has been stolen, and until it was logged it produced nothing an
    operator could ever see - `verify_token` answers `None` for a revoked token and for
    garbage alike.

    Two things are load-bearing and both are asserted here: the level (`WARNING`, because
    nothing configures logging below it - see `_warn_if_revoked`) and the silence on a
    malformed token, which is noise rather than a security event.
    """

    async def _spent_token(self, mock_db) -> str:
        """Sign in and refresh once, returning the cookie that was spent doing so."""
        sign_in = Response()
        await issue_tokens(sign_in, USER_UUID)
        spent = _refresh_cookie(sign_in)
        await refresh_access_token(_request({"refresh_token": spent}), Response(), mock_db)
        return spent

    @pytest.mark.asyncio
    async def test_reuse_logs_a_warning_naming_the_account(self, mock_db, caplog):
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            spent = await self._spent_token(mock_db)

            # Captured from `DEBUG` up on purpose: capturing at `WARNING` would pass just
            # as well against an `info` call that a real deployment never prints.
            with caplog.at_level(logging.DEBUG, logger=AUTH_LOGGER):
                with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                    await refresh_access_token(_request({"refresh_token": spent}), Response(), mock_db)

        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        message = caplog.records[0].getMessage()
        assert str(USER_UUID) in message
        assert "after it was revoked" in message

    @pytest.mark.asyncio
    async def test_malformed_token_logs_nothing(self, mock_db, caplog):
        """A cookie that was never a token this server issued is noise. Logging it would
        let anyone fill the operator's log by posting garbage, and would drown the one
        line that means something.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            caplog.at_level(logging.DEBUG, logger=AUTH_LOGGER),
            pytest.raises(UnauthorizedException, match="Invalid refresh token."),
        ):
            await refresh_access_token(_request({"refresh_token": "not-a-jwt"}), Response(), mock_db)

        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_the_response_is_identical_either_way(self, mock_db):
        """The log is the *only* place the two cases differ. If the 401 differed, it would
        be an oracle for whether a given cookie was ever a real token.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            spent = await self._spent_token(mock_db)

            with pytest.raises(UnauthorizedException) as reused:
                await refresh_access_token(_request({"refresh_token": spent}), Response(), mock_db)

            with pytest.raises(UnauthorizedException) as garbage:
                await refresh_access_token(_request({"refresh_token": "not-a-jwt"}), Response(), mock_db)

        assert reused.value.detail == garbage.value.detail
        assert reused.value.status_code == garbage.value.status_code

    @pytest.mark.asyncio
    async def test_revocation_is_timestamped_when_the_token_is_spent(self, mock_db):
        """`expires_at` is the token's own `exp`, i.e. when it was *issued* - so without
        `revoked_at` there is nothing to measure the replay against.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            spent = await self._spent_token(mock_db)

        entry = blacklist.entries[spent]
        assert entry.revoked_at == FrozenSecurityClock.now(UTC)
        assert entry.revoked_at != entry.expires_at

    def test_elapsed_stays_readable_at_both_ends_of_the_range(self):
        """Sub-second resolution is what separates the documented two-tab race from a
        replay, and a replay can land days later - where a bare seconds count says nothing
        at a glance.
        """
        now = datetime.now(UTC)

        assert _elapsed(now - timedelta(milliseconds=12)).endswith("s")
        assert float(_elapsed(now - timedelta(milliseconds=12)).rstrip("s")) < 1
        assert "day" in _elapsed(now - timedelta(days=3))
