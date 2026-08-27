"""Unit tests for the auth/security helpers."""

import logging
import uuid as uuid_pkg
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi import HTTPException
from jose import jwt
from pydantic import SecretStr

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
    exchange_google_code,
    generate_secure_token,
    hash_token,
    verify_google_id_token,
    verify_onboarding_token,
    verify_token,
)
from tests.helpers.mocks import FrozenSecurityClock


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


class TestTokensMintedInTheSameSecond:
    """Two tokens for one subject minted inside the same wall-clock second must still be
    different tokens.

    `exp` has one-second resolution, so before `jti` existed they were byte-identical -
    and since revocation stores the token *string* (`token_blacklist.token`), one
    blacklist entry covered both. `/auth/refresh` ran into it against itself: it spends
    the presented cookie and then mints a replacement, so a collision handed the caller a
    refresh token that was already revoked and 401'd their next refresh. See
    `core.security._new_jti`.

    The clock is frozen rather than the calls merely being made back to back - the
    collision only shows up when both land in the same second, which is exactly what made
    the original bug intermittent.
    """

    @staticmethod
    def _claims(token: str) -> dict[str, Any]:
        # `jwt.decode` is typed as returning `Any`; a decoded JWT payload is a dict.
        claims: dict[str, Any] = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
        return claims

    @pytest.mark.asyncio
    async def test_refresh_tokens_are_distinct(self):
        with patch("src.app.core.security.datetime", FrozenSecurityClock):
            first = await create_refresh_token({"sub": "someuser"})
            second = await create_refresh_token({"sub": "someuser"})

        assert first != second
        # Nothing else in the payload separates them, so `jti` has to.
        assert self._claims(first)["exp"] == self._claims(second)["exp"]
        assert self._claims(first)["jti"] != self._claims(second)["jti"]

    @pytest.mark.asyncio
    async def test_access_tokens_are_distinct(self):
        """Access tokens collide the same way, and `/auth/logout` blacklists them by
        value - so without this, signing out of one session revokes any other session
        whose access token was minted in the same second.
        """
        with patch("src.app.core.security.datetime", FrozenSecurityClock):
            first = await create_access_token({"sub": "someuser"})
            second = await create_access_token({"sub": "someuser"})

        assert first != second
        assert self._claims(first)["exp"] == self._claims(second)["exp"]
        assert self._claims(first)["jti"] != self._claims(second)["jti"]

    @pytest.mark.asyncio
    async def test_onboarding_tokens_are_distinct(self):
        """Onboarding tokens are blacklisted to make them single-use, so a collision
        spends both: a magic link opened twice in the same second yields one usable
        `/auth/complete` and one dead session.
        """
        data = OnboardingTokenData(email="new@example.com", provider="email")

        with patch("src.app.core.security.datetime", FrozenSecurityClock):
            first = await create_onboarding_token(data)
            second = await create_onboarding_token(data)

        assert first != second
        assert self._claims(first)["exp"] == self._claims(second)["exp"]
        assert self._claims(first)["jti"] != self._claims(second)["jti"]


class TestVerifyToken:
    """Test JWT token verification."""

    @pytest.mark.asyncio
    async def test_verify_valid_access_token(self, mock_db):
        user_uuid = uuid_pkg.uuid4()
        token = await create_access_token({"sub": str(user_uuid)})

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=False)

            token_data = await verify_token(token, TokenType.ACCESS, mock_db)

            assert token_data is not None
            assert token_data.user_uuid == user_uuid

    @pytest.mark.asyncio
    async def test_verify_non_uuid_subject_returns_none(self, mock_db):
        """The subject used to be a username. Such a token is a 401, not the 500 an
        unhandled `ValueError` out of `uuid.UUID("someuser")` would produce - which is
        what every token minted before the cutover now hits.
        """
        token = await create_access_token({"sub": "someuser"})

        with patch("src.app.core.security.crud_token_blacklist") as mock_blacklist:
            mock_blacklist.exists = AsyncMock(return_value=False)

            token_data = await verify_token(token, TokenType.ACCESS, mock_db)

            assert token_data is None

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


_REAL_ASYNC_CLIENT = httpx.AsyncClient

GOOGLE_CLIENT_SECRET = "the-client-secret-nobody-may-see"


class _GoogleTokenEndpoint:
    """An `httpx.MockTransport` in place of the one `AsyncClient` `exchange_google_code`
    opens, following `tests/test_geocoding.py`.

    A real client over a fake transport rather than a mocked client, so the request these
    tests read is the one httpx would actually have put on the wire - which is the only way
    "the form carries a `code_verifier`" is worth asserting.
    """

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler
        self._patcher: Any = None

    def _record(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def __enter__(self) -> _GoogleTokenEndpoint:
        # `_REAL_ASYNC_CLIENT`, not `httpx.AsyncClient`: `core.security` reaches the class
        # through the same module object this file imported, so the patch below replaces it
        # here too and building one inside the factory would recurse into the mock.
        def build(**kwargs: Any) -> httpx.AsyncClient:
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(self._record), **kwargs)

        self._patcher = patch("src.app.core.security.httpx.AsyncClient", side_effect=build)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._patcher.stop()

    @property
    def form(self) -> dict[str, str]:
        """The single request's body, parsed back out of its form encoding."""
        assert len(self.requests) == 1
        return dict(parse_qsl(self.requests[0].content.decode()))


def _answers(payload: Any, status_code: int = 200) -> _GoogleTokenEndpoint:
    return _GoogleTokenEndpoint(lambda request: httpx.Response(status_code, json=payload))


def _google_configured(client_id: str | None = "client-id", secret: str | None = GOOGLE_CLIENT_SECRET) -> Any:
    """Both halves of the OAuth client, as a running instance is guaranteed to have them
    (`Settings._require_google_client_secret` refuses to boot without both)."""
    patcher = patch("src.app.core.security.settings")
    mock_settings = patcher.start()
    mock_settings.GOOGLE_CLIENT_ID = client_id
    mock_settings.GOOGLE_CLIENT_SECRET = SecretStr(secret) if secret is not None else None
    return patcher


class TestExchangeGoogleCode:
    """Redeeming an authorization code at Google's token endpoint.

    The two things worth pinning here are what goes *out* - a code with no PKCE verifier or
    no client secret is not an exchange Google will complete - and that "Google said no" and
    "Google was not there" stay two different answers all the way up.
    """

    @pytest.mark.asyncio
    async def test_the_form_carries_everything_google_needs(self):
        patcher = _google_configured()
        try:
            with _answers({"id_token": "an-id-token"}) as endpoint:
                await exchange_google_code(
                    code="the-code", code_verifier="the-verifier", redirect_uri="https://dive.example.com/cb"
                )
        finally:
            patcher.stop()

        assert endpoint.form == {
            "code": "the-code",
            "client_id": "client-id",
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code_verifier": "the-verifier",
            "redirect_uri": "https://dive.example.com/cb",
            "grant_type": "authorization_code",
        }

    @pytest.mark.asyncio
    async def test_it_posts_to_googles_token_endpoint(self):
        patcher = _google_configured()
        try:
            with _answers({"id_token": "an-id-token"}) as endpoint:
                await exchange_google_code(code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb")
        finally:
            patcher.stop()

        assert endpoint.requests[0].method == "POST"
        assert str(endpoint.requests[0].url) == "https://oauth2.googleapis.com/token"

    @pytest.mark.asyncio
    async def test_the_id_token_is_what_comes_back(self):
        patcher = _google_configured()
        try:
            with _answers({"access_token": "at", "id_token": "an-id-token", "token_type": "Bearer"}):
                result = await exchange_google_code(
                    code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb"
                )
        finally:
            patcher.stop()

        assert result == "an-id-token"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [400, 401, 403])
    async def test_a_code_google_rejects_is_none_rather_than_an_outage(self, status_code: int):
        """An expired, replayed or mismatched code is the caller's problem and becomes a 401
        upstream. It must not be dressed up as this server failing."""
        patcher = _google_configured()
        try:
            with _answers({"error": "invalid_grant"}, status_code=status_code):
                result = await exchange_google_code(
                    code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb"
                )
        finally:
            patcher.stop()

        assert result is None

    @pytest.mark.asyncio
    async def test_a_two_hundred_carrying_no_id_token_is_none(self):
        patcher = _google_configured()
        try:
            with _answers({"access_token": "at", "token_type": "Bearer"}):
                result = await exchange_google_code(
                    code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb"
                )
        finally:
            patcher.stop()

        assert result is None

    @pytest.mark.asyncio
    async def test_a_refusal_whose_body_is_unreadable_is_still_only_a_refusal(self, caplog):
        """A middlebox answering the 4xx with an HTML page must not turn a bad code into an
        exception raised while trying to read the reason.
        """
        patcher = _google_configured()
        try:
            with caplog.at_level(logging.DEBUG):
                with _GoogleTokenEndpoint(lambda request: httpx.Response(400, text="<html>blocked</html>")):
                    result = await exchange_google_code(
                        code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb"
                    )
        finally:
            patcher.stop()

        assert result is None
        assert "unreadable" in "\n".join(record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_being_throttled_by_google_is_not_a_bad_credential_either(self):
        """429 sits with the 5xx despite being a 4xx. A quota this server has exhausted is
        not a code the visitor got wrong, and "try signing in again" is the one piece of
        advice that cannot help.
        """
        patcher = _google_configured()
        try:
            with _answers({"error": "rate_limit_exceeded"}, status_code=429):
                with pytest.raises(HTTPException) as raised:
                    await exchange_google_code(code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb")
        finally:
            patcher.stop()

        assert raised.value.status_code == 503

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [500, 502, 503])
    async def test_google_failing_is_a_503_not_a_bad_credential(self, status_code: int):
        patcher = _google_configured()
        try:
            with _answers({"error": "backend_error"}, status_code=status_code):
                with pytest.raises(HTTPException) as raised:
                    await exchange_google_code(code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb")
        finally:
            patcher.stop()

        assert raised.value.status_code == 503

    @pytest.mark.asyncio
    async def test_google_being_unreachable_is_a_503(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host", request=request)

        patcher = _google_configured()
        try:
            with _GoogleTokenEndpoint(refuse):
                with pytest.raises(HTTPException) as raised:
                    await exchange_google_code(code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb")
        finally:
            patcher.stop()

        assert raised.value.status_code == 503

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_a_503(self):
        patcher = _google_configured()
        try:
            with _GoogleTokenEndpoint(lambda request: httpx.Response(200, text="<html>an error page</html>")):
                with pytest.raises(HTTPException) as raised:
                    await exchange_google_code(code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb")
        finally:
            patcher.stop()

        assert raised.value.status_code == 503

    @pytest.mark.asyncio
    async def test_an_unconfigured_instance_contacts_nobody(self):
        patcher = _google_configured(client_id=None, secret=None)
        try:
            with _answers({"id_token": "an-id-token"}) as endpoint:
                result = await exchange_google_code(
                    code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb"
                )
        finally:
            patcher.stop()

        assert result is None
        assert endpoint.requests == []

    @pytest.mark.asyncio
    async def test_the_client_secret_never_reaches_a_log_line(self, caplog):
        """`GOOGLE_CLIENT_SECRET` is the first genuinely secret Google value here, and logs
        get collected, shipped and kept. Nothing from Google's response is logged either -
        the refusal below carries an `error_description`, and only the status code is
        written at a level that ships.
        """
        patcher = _google_configured()
        try:
            with caplog.at_level(logging.DEBUG):
                with _answers(
                    {"error": "invalid_client", "error_description": "Unauthorized: bad client secret"},
                    status_code=401,
                ):
                    await exchange_google_code(code="c", code_verifier="v", redirect_uri="https://dive.example.com/cb")
        finally:
            patcher.stop()

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert GOOGLE_CLIENT_SECRET not in logged
        assert "Unauthorized: bad client secret" not in logged
        # The short OAuth enum is the one thing that is kept, and only under DEBUG.
        assert "invalid_client" in logged
        shipped = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)
        assert "invalid_client" not in shipped


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
