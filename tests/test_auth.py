"""Unit tests for the unified auth flow (`api.v1.auth`): email magic link, Google, and
profile completion. See `tests/test_auth_service.py` for the shared identity-resolution
logic, and `tests/test_security.py` for the underlying token helpers.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException, Response
from jose import jwt
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from src.app.api.v1.auth import (
    auth_with_google,
    check_email_link,
    complete_profile,
    request_email_link,
    verify_email_code,
    verify_email_link,
)
from src.app.core.config import FrontendSettings, settings
from src.app.core.exceptions.http_exceptions import (
    BadRequestException,
    DuplicateValueException,
    RateLimitException,
    UnauthorizedException,
)
from src.app.core.schemas import GoogleUserInfo, OnboardingTokenData
from src.app.core.security import ALGORITHM, SECRET_KEY, hash_sign_in_code
from src.app.schemas.auth import (
    EmailAuthRequest,
    EmailCodeVerifyRequest,
    EmailVerifyRequest,
    GoogleAuthRequest,
    ProfileCompletionRequest,
)
from src.app.services.user_avatars import StoredAvatar
from tests.helpers.mocks import (
    GOOGLE_CODE_VERIFIER,
    claimed_used_at_sql,
    fake_request,
    google_auth_body,
    stub_claim,
)

# Every sign-in path subjects its tokens to this, not to the account's username - see
# `services.auth_service.issue_tokens`.
USER_UUID = uuid_pkg.uuid4()

# The public uuid of the `authentication_request` row - handed back as `request_id` by
# `POST /auth/email/request` and the only handle on `POST /auth/email/verify-code`.
REQUEST_UUID = uuid_pkg.uuid4()


# Shared with `test_account_restore.py`, which had the identical copy of it.
_request = fake_request


def _created_row(request_uuid: uuid_pkg.UUID = REQUEST_UUID) -> Mock:
    """What `crud_authentication_requests.create` hands back now that the route asks for
    the model: `request_email_link` reads `.uuid` off it to answer with a `request_id`.
    """
    return Mock(uuid=request_uuid)


# What `google_auth_body` builds bodies against, named here because these tests assert on
# it as well as send it.
FRONTEND_CALLBACK = settings.google_redirect_uri


class TestRequestEmailLink:
    """`POST /auth/email/request` - step 1 of the email flow."""

    @pytest.mark.asyncio
    async def test_returns_generic_message_and_sends_email(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock) as mock_send,
        ):
            mock_crud.count = AsyncMock(return_value=0)
            mock_crud.update = AsyncMock(return_value=None)
            mock_crud.create = AsyncMock(return_value=_created_row())

            result = await request_email_link(_request(), EmailAuthRequest(email="new@example.com"), mock_db)

            assert result.message == "Check your email for the next step."
            mock_send.assert_called_once()
            assert mock_send.call_args.kwargs["email"] == "new@example.com"
            mock_crud.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_hands_the_asking_browser_the_request_id_for_its_code(self, mock_db):
        """The `request_id` is the whole reason `POST /auth/email/verify-code` is safe to
        expose anonymously: without it there is no way to name a row, so nobody but the
        browser that asked can spend a diver's code attempts.
        """
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
        ):
            mock_crud.count = AsyncMock(return_value=0)
            mock_crud.create = AsyncMock(return_value=_created_row())

            result = await request_email_link(_request(), EmailAuthRequest(email="new@example.com"), mock_db)

            assert result.request_id == REQUEST_UUID

    @pytest.mark.asyncio
    async def test_emails_a_six_digit_code_and_stores_only_its_hash(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock) as mock_send,
        ):
            mock_crud.count = AsyncMock(return_value=0)
            mock_crud.create = AsyncMock(return_value=_created_row())

            await request_email_link(_request(), EmailAuthRequest(email="new@example.com"), mock_db)

            code = mock_send.call_args.kwargs["code"]
            assert len(code) == 6
            assert code.isdigit()

            stored = mock_crud.create.call_args.kwargs["object"]
            assert stored.code_hash == hash_sign_in_code(code)
            assert code not in str(stored)

    @pytest.mark.asyncio
    async def test_never_queries_whether_the_user_exists(self, mock_db):
        """Email enumeration protection: the response (and the code path leading to
        it) must be identical whether or not an account exists for this email - this
        endpoint doesn't even look at `crud_users`.

        **And it must not learn whether the address is invited either.** Once registration
        can be closed, "is this address allowed in" is a second fact about a stranger that a
        differing code path here would leak, and it is the more sensitive of the two on an
        instance whose whole membership is an invitation list. The refusal happens later, at
        the onboarding branch and at account creation, where the caller has already proven
        the address and nothing is disclosed - so the assertions below are extended rather
        than the guarantee weakened.
        """
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.auth.crud_users") as mock_users,
            patch("src.app.api.v1.auth.refuse_uninvited", new_callable=AsyncMock) as gate,
            patch("src.app.api.v1.auth.accept_invitations", new_callable=AsyncMock) as accepted,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
        ):
            mock_crud.count = AsyncMock(return_value=0)
            mock_crud.update = AsyncMock(return_value=None)
            mock_crud.create = AsyncMock(return_value=_created_row())

            result = await request_email_link(_request(), EmailAuthRequest(email="anyone@example.com"), mock_db)

            assert result.message == "Check your email for the next step."
            mock_users.exists.assert_not_called()
            mock_users.get.assert_not_called()
            gate.assert_not_awaited()
            accepted.assert_not_awaited()

    def test_the_request_handler_names_no_invitation_table(self):
        """The structural half of the guarantee above, which the mocks cannot give.

        A patch proves the symbols this module imports were not called; it says nothing
        about a query issued through `db.execute` inside the handler. Reading the source is
        what covers that, and it is the same shape `test_picker_search.py` uses to assert a
        handler's own body calls `clamp_pagination`.
        """
        import inspect

        from src.app.api.v1 import auth as auth_module

        body = inspect.getsource(auth_module.request_email_link)
        assert "Invitation" not in body
        assert "InviteRequest" not in body
        assert "invitation" not in body.lower().split('"""')[-1]

    @pytest.mark.asyncio
    async def test_invalidates_previous_pending_tokens_for_the_same_email(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
        ):
            mock_crud.count = AsyncMock(return_value=1)
            mock_crud.update = AsyncMock(return_value=None)
            mock_crud.create = AsyncMock(return_value=_created_row())

            await request_email_link(_request(), EmailAuthRequest(email="repeat@example.com"), mock_db)

            mock_crud.update.assert_called_once()
            kwargs = mock_crud.update.call_args.kwargs
            assert kwargs["allow_multiple"] is True
            assert kwargs["email"] == "repeat@example.com"
            assert kwargs["purpose"] == "sign_in"
            assert kwargs["invalidated_at"] is None
            assert kwargs["object"].invalidated_at is not None

    @pytest.mark.asyncio
    async def test_does_not_call_update_when_there_is_nothing_pending(self, mock_db):
        """Regression test: FastCRUD's `update(..., allow_multiple=True)` raises
        `NoResultFound` when zero rows match, so this must be checked via `count()`
        first rather than calling `update()` unconditionally (the common case - a
        first-time request - has nothing pending to invalidate)."""
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
        ):
            mock_crud.count = AsyncMock(return_value=0)
            mock_crud.update = AsyncMock(return_value=None)
            mock_crud.create = AsyncMock(return_value=_created_row())

            await request_email_link(_request(), EmailAuthRequest(email="first-time@example.com"), mock_db)

            mock_crud.count.assert_called_once_with(
                mock_db, email="first-time@example.com", purpose="sign_in", invalidated_at=None
            )
            mock_crud.update.assert_not_called()
            mock_crud.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limited_requests_are_rejected(self, mock_db):
        with patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock) as mock_limit:
            mock_limit.side_effect = RateLimitException("Too many requests. Please try again later.")

            with pytest.raises(RateLimitException):
                await request_email_link(_request(), EmailAuthRequest(email="spammed@example.com"), mock_db)


class TestCheckEmailLink:
    """`GET /auth/email/verify/check` - the side-effect-free precheck used by the
    sign-in landing page before it shows the "Sign in" button."""

    @pytest.mark.asyncio
    async def test_rate_limited_requests_are_rejected(self, mock_db):
        with patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock) as mock_limit:
            mock_limit.side_effect = RateLimitException("Too many requests. Please try again later.")

            with pytest.raises(RateLimitException):
                await check_email_link(_request(), Response(), "any", mock_db)

    @pytest.mark.asyncio
    async def test_not_found_is_invalid(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=None)

            result = await check_email_link(_request(), Response(), "bad", mock_db)

            assert result.valid is False
            assert result.email is None

    @pytest.mark.asyncio
    async def test_invalidated_token_is_invalid(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "a@example.com",
            "used_at": None,
            "invalidated_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            result = await check_email_link(_request(), Response(), "stale", mock_db)

            assert result.valid is False

    @pytest.mark.asyncio
    async def test_already_used_token_is_invalid(self, mock_db):
        """`verify_email_link` rejects a used token too, so this precheck isn't the
        security boundary - it's what turns that rejection into an error message on
        page load instead of a button that looks clickable and then 401s (e.g. after
        the browser's back button lands a user back on an already-followed link)."""
        auth_request = {
            "id": 1,
            "email": "a@example.com",
            "used_at": datetime.now(UTC),
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            result = await check_email_link(_request(), Response(), "already-used", mock_db)

            assert result.valid is False

    @pytest.mark.asyncio
    async def test_expired_token_is_invalid(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "a@example.com",
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
        }
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            result = await check_email_link(_request(), Response(), "expired", mock_db)

            assert result.valid is False

    @pytest.mark.asyncio
    async def test_fresh_token_is_valid_and_returns_email(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "a@example.com",
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.auth.crud_users") as mock_users,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(return_value=None)

            result = await check_email_link(_request(), Response(), "good", mock_db)

            assert result.valid is True
            assert result.email == "a@example.com"
            assert result.deletion_pending is False
            assert result.purge_after is None


class TestVerifyEmailLink:
    """`POST /auth/email/verify` - step 2 of the email flow."""

    @pytest.mark.asyncio
    async def test_invalid_token_raises_unauthorized(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=None)

            with pytest.raises(UnauthorizedException, match="invalid"):
                await verify_email_link(_request(), EmailVerifyRequest(token="bad"), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_invalidated_token_raises_unauthorized(self, mock_db):
        """Superseded by a newer request (see `request_email_link`). Checked before
        `used_at`, so a link that is both superseded and used reports the supersession
        - the one of the two that tells the user a newer link is waiting for them."""
        auth_request = {
            "id": 1,
            "email": "a@example.com",
            "used_at": None,
            "invalidated_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            with pytest.raises(UnauthorizedException, match="no longer valid"):
                await verify_email_link(_request(), EmailVerifyRequest(token="stale"), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_expired_token_raises_unauthorized(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "a@example.com",
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
        }
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            with pytest.raises(UnauthorizedException, match="expired"):
                await verify_email_link(_request(), EmailVerifyRequest(token="expired"), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_already_used_but_live_token_raises_unauthorized(self, mock_db):
        """The link is single-use even while it is still inside its expiry window.
        Accepting the replay would mint a second refresh cookie outliving the link
        by `REFRESH_TOKEN_EXPIRE_DAYS`, so whoever reads the mail after the recipient
        clicked it - a shared mailbox, an archive, a retaining gateway - would get a
        session of their own. No session is issued and `used_at` is left alone."""
        auth_request = {
            "id": 1,
            "email": "existing@example.com",
            "used_at": datetime.now(UTC),
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            stub_claim(mock_db)

            response = Mock()
            with pytest.raises(UnauthorizedException, match="already been used"):
                await verify_email_link(_request(), EmailVerifyRequest(token="already-used"), response, mock_db)

            mock_db.execute.assert_not_called()
            response.set_cookie.assert_not_called()

    @pytest.mark.asyncio
    async def test_used_token_is_rejected_as_used_not_expired(self, mock_db):
        """Ordering check: a token that is both used and expired reports "used", the
        more specific of the two - the order `check_email_link` evaluates them in."""
        auth_request = {
            "id": 1,
            "email": "existing@example.com",
            "used_at": datetime.now(UTC) - timedelta(hours=2),
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
        }

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)

            with pytest.raises(UnauthorizedException, match="already been used"):
                await verify_email_link(_request(), EmailVerifyRequest(token="used-and-expired"), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_losing_the_claim_to_a_concurrent_verification_raises_unauthorized(self, mock_db):
        """The `used_at` read above cannot settle this on its own - two submissions of the
        same still-live link both see it null. The conditional `UPDATE ... WHERE used_at
        IS NULL` is the gate that decides, and the loser (`rowcount == 0`) must stop
        before `resolve_identity`, let alone before a refresh cookie."""
        auth_request = {
            "id": 1,
            "email": "existing@example.com",
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.auth.resolve_identity", new_callable=AsyncMock) as mock_resolve,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            stub_claim(mock_db, won=False)

            response = Mock()
            with pytest.raises(UnauthorizedException, match="already been used"):
                await verify_email_link(_request(), EmailVerifyRequest(token="raced"), response, mock_db)

            mock_resolve.assert_not_called()
            response.set_cookie.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_user_is_authenticated_and_token_marked_used(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "existing@example.com",
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        db_user = {
            "id": 1,
            "uuid": USER_UUID,
            "username": "existinguser",
            "email": "existing@example.com",
            "is_deleted": False,
        }

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(return_value=db_user)
            mock_providers.exists = AsyncMock(return_value=True)
            stub_claim(mock_db)

            response = Mock()
            outcome = await verify_email_link(_request(), EmailVerifyRequest(token="good"), response, mock_db)

            assert outcome.status == "authenticated"
            assert outcome.access_token is not None
            response.set_cookie.assert_called_once()
            # The claim runs exactly once - two would mean the single-use gate had been
            # opened twice on one request. Counted by table rather than by total statements,
            # which is no longer one: minting the session runs a cap eviction of its own.
            claims = [
                call.args[0]
                for call in mock_db.execute.call_args_list
                if call.args and "UPDATE authentication_request" in str(call.args[0])
            ]
            assert len(claims) == 1
            assert "used_at IS NULL" in claimed_used_at_sql(mock_db)

    @pytest.mark.asyncio
    async def test_new_user_gets_onboarding_session(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "new@example.com",
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_users.get = AsyncMock(return_value=None)
            stub_claim(mock_db)

            outcome = await verify_email_link(_request(), EmailVerifyRequest(token="good"), Mock(), mock_db)

            assert outcome.status == "onboarding_required"
            assert outcome.onboarding_token is not None
            assert outcome.email == "new@example.com"


CODE = "481052"


def _code_request(**overrides) -> dict:
    """A live `sign_in` row carrying a code, as `crud.get` hands it back."""
    row = {
        "id": 1,
        "uuid": REQUEST_UUID,
        "email": "existing@example.com",
        "code_hash": hash_sign_in_code(CODE),
        "used_at": None,
        "invalidated_at": None,
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
    }
    row.update(overrides)
    return row


class TestVerifyEmailCode:
    """`POST /auth/email/verify-code` - the code from the sign-in email, typed back into
    the tab that asked for it. The other half of step 2, beside the link.
    """

    @staticmethod
    def _body(code: str = CODE) -> EmailCodeVerifyRequest:
        return EmailCodeVerifyRequest(request_id=REQUEST_UUID, code=code)

    @pytest.mark.asyncio
    async def test_rate_limited_requests_are_rejected(self, mock_db):
        with patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock) as mock_limit:
            mock_limit.side_effect = RateLimitException("Too many requests. Please try again later.")

            with pytest.raises(RateLimitException):
                await verify_email_code(_request(), self._body(), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_the_row_is_found_by_uuid_and_never_by_email(self, mock_db):
        """Load-bearing, not an implementation detail. An email-keyed lookup would put
        every diver's code attempts in reach of anyone who knows their address, and would
        also have to pick between the several live rows two racing tabs can leave.
        """
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=_code_request())
            mock_users.get = AsyncMock(
                return_value={"id": 1, "uuid": USER_UUID, "email": "existing@example.com", "is_deleted": False}
            )
            mock_providers.exists = AsyncMock(return_value=True)
            stub_claim(mock_db)

            await verify_email_code(_request(), self._body(), Mock(), mock_db)

            kwargs = mock_requests.get.call_args.kwargs
            assert kwargs["uuid"] == REQUEST_UUID
            assert kwargs["purpose"] == "sign_in"
            assert "email" not in kwargs

    @pytest.mark.asyncio
    async def test_an_unknown_request_id_and_a_wrong_code_answer_identically(self, mock_db):
        """The no-oracle rule for this endpoint. Someone holding a `request_id` learns
        nothing about the row it names, and someone holding none learns nothing about
        whether it ever existed.
        """
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.auth.register_failed_code_attempt", new_callable=AsyncMock),
        ):
            mock_requests.get = AsyncMock(return_value=None)
            with pytest.raises(UnauthorizedException) as unknown_id:
                await verify_email_code(_request(), self._body(), Mock(), mock_db)

            mock_requests.get = AsyncMock(return_value=_code_request())
            with pytest.raises(UnauthorizedException) as wrong_code:
                await verify_email_code(_request(), self._body(code="000000"), Mock(), mock_db)

        assert unknown_id.value.detail == wrong_code.value.detail

    @pytest.mark.asyncio
    async def test_a_wrong_code_is_charged_against_the_attempt_cap(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.auth.register_failed_code_attempt", new_callable=AsyncMock) as mock_charge,
            patch("src.app.api.v1.auth.resolve_identity", new_callable=AsyncMock) as mock_resolve,
        ):
            mock_requests.get = AsyncMock(return_value=_code_request())

            response = Mock()
            with pytest.raises(UnauthorizedException):
                await verify_email_code(_request(), self._body(code="000000"), response, mock_db)

            mock_charge.assert_awaited_once()
            assert mock_charge.await_args.kwargs["request_id"] == 1
            assert mock_charge.await_args.kwargs["max_attempts"] == settings.SIGN_IN_CODE_ATTEMPTS_MAX
            mock_resolve.assert_not_called()
            response.set_cookie.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_spent_code_is_rejected_without_charging_another_attempt(self, mock_db):
        """`code_hash IS NULL` is how "the attempts ran out" is spelled. Charging again
        would keep incrementing a counter nothing reads, and the row still carries a live
        link this must not touch.
        """
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.auth.register_failed_code_attempt", new_callable=AsyncMock) as mock_charge,
        ):
            mock_requests.get = AsyncMock(return_value=_code_request(code_hash=None))

            with pytest.raises(UnauthorizedException):
                await verify_email_code(_request(), self._body(), Mock(), mock_db)

            mock_charge.assert_not_called()
            mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"invalidated_at": datetime.now(UTC)}, id="invalidated"),
            pytest.param({"used_at": datetime.now(UTC)}, id="used"),
            pytest.param({"expires_at": datetime.now(UTC) - timedelta(minutes=1)}, id="expired"),
        ],
    )
    async def test_a_row_that_is_no_longer_live_is_rejected(self, mock_db, overrides):
        """Unlike the link, these collapse into one message rather than reporting which
        applies: the link's reasons steer a human looking at a page ("a newer link is
        waiting"), while a code is typed against a row the caller cannot see.
        """
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.auth.register_failed_code_attempt", new_callable=AsyncMock) as mock_charge,
        ):
            mock_requests.get = AsyncMock(return_value=_code_request(**overrides))

            with pytest.raises(UnauthorizedException, match="invalid or has expired"):
                await verify_email_code(_request(), self._body(), Mock(), mock_db)

            mock_charge.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_right_code_signs_an_existing_user_in_through_the_claim(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=_code_request())
            mock_users.get = AsyncMock(
                return_value={
                    "id": 1,
                    "uuid": USER_UUID,
                    "username": "existinguser",
                    "email": "existing@example.com",
                    "is_deleted": False,
                }
            )
            mock_providers.exists = AsyncMock(return_value=True)
            stub_claim(mock_db)

            response = Mock()
            outcome = await verify_email_code(_request(), self._body(), response, mock_db)

            assert outcome.status == "authenticated"
            assert outcome.access_token is not None
            response.set_cookie.assert_called_once()
            assert "used_at IS NULL" in claimed_used_at_sql(mock_db)

    @pytest.mark.asyncio
    async def test_a_code_typed_with_the_spacing_the_email_prints_still_works(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=_code_request())
            mock_users.get = AsyncMock(
                return_value={"id": 1, "uuid": USER_UUID, "email": "existing@example.com", "is_deleted": False}
            )
            mock_providers.exists = AsyncMock(return_value=True)
            stub_claim(mock_db)

            outcome = await verify_email_code(_request(), self._body(code="481 052"), Mock(), mock_db)

            assert outcome.status == "authenticated"

    @pytest.mark.asyncio
    async def test_losing_the_claim_to_the_link_in_the_same_email_raises_unauthorized(self, mock_db):
        """One row, two credentials, one session. Whichever of the link and the code
        reaches `claim_authentication_request` first is the one that signs in; the other
        is refused, and no second session is minted.
        """
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.api.v1.auth.resolve_identity", new_callable=AsyncMock) as mock_resolve,
        ):
            mock_requests.get = AsyncMock(return_value=_code_request())
            stub_claim(mock_db, won=False)

            response = Mock()
            with pytest.raises(UnauthorizedException, match="invalid or has expired"):
                await verify_email_code(_request(), self._body(), response, mock_db)

            mock_resolve.assert_not_called()
            response.set_cookie.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_new_user_gets_an_onboarding_session(self, mock_db):
        """The unified flow needs no carve-out for codes: a verified email with no account
        behind it goes to `/auth/complete`, exactly as it does from the link.
        """
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=_code_request(email="new@example.com"))
            mock_users.get = AsyncMock(return_value=None)
            stub_claim(mock_db)

            outcome = await verify_email_code(_request(), self._body(), Mock(), mock_db)

            assert outcome.status == "onboarding_required"
            assert outcome.email == "new@example.com"
            assert outcome.onboarding_token is not None


class TestEmailCodeVerifyRequestSchema:
    """The code arrives from a human keyboard, so the schema does the normalizing."""

    @pytest.mark.parametrize("typed", ["481052", "481 052", "481-052", " 481052 "])
    def test_separators_are_stripped(self, typed):
        assert EmailCodeVerifyRequest(request_id=REQUEST_UUID, code=typed).code == "481052"

    @pytest.mark.parametrize("typed", ["48105", "4810521", "", "abcdef"])
    def test_anything_that_is_not_six_digits_is_a_422_not_a_guess(self, typed):
        """Rejected here rather than counted against `code_attempts`: the cap bounds
        guesses at the secret, and a five-character string was never one.
        """
        with pytest.raises(ValidationError):
            EmailCodeVerifyRequest(request_id=REQUEST_UUID, code=typed)


class TestGoogleAuthRequestShape:
    """The body `POST /auth/google` accepts, now that the browser sends a code."""

    def test_the_old_credential_field_is_refused_rather_than_ignored(self):
        """`extra="forbid"`, so a client still sending a GIS ID token gets a 422 that names
        the field. A moved contract must not accept a body it will silently do nothing with.
        """
        with pytest.raises(ValidationError):
            GoogleAuthRequest(
                code="an-authorization-code",
                code_verifier=GOOGLE_CODE_VERIFIER,
                redirect_uri=FRONTEND_CALLBACK,
                credential="an-id-token",
            )

    def test_the_credential_only_body_the_old_client_sent_is_refused(self):
        with pytest.raises(ValidationError):
            GoogleAuthRequest(credential="an-id-token")

    @pytest.mark.parametrize("verifier", ["a" * 42, "a" * 129, ""])
    def test_a_verifier_outside_rfc_7636s_bounds_is_a_422(self, verifier: str):
        """43-128 characters, per RFC 7636 §4.1. Rejected here so a malformed verifier
        names the field rather than coming back as an `invalid_grant` from Google that
        names nothing.
        """
        with pytest.raises(ValidationError):
            google_auth_body(code_verifier=verifier)

    @pytest.mark.parametrize("verifier", ["a" * 42 + "+", "a" * 42 + "/", "a" * 42 + "="])
    def test_a_verifier_outside_rfc_7636s_alphabet_is_a_422(self, verifier: str):
        """The unreserved set only. This is the shape a verifier built with standard base64
        rather than base64url arrives in, and catching it here beats an `invalid_grant`.
        """
        with pytest.raises(ValidationError):
            google_auth_body(code_verifier=verifier)

    def test_the_alphabet_rfc_7636_does_allow_is_accepted(self):
        verifier = "AZaz09-._~" + "a" * 33

        assert google_auth_body(code_verifier=verifier).code_verifier == verifier

    @pytest.mark.parametrize("verifier", ["a" * 43, "a" * 128])
    def test_both_ends_of_that_range_are_accepted(self, verifier: str):
        assert google_auth_body(code_verifier=verifier).code_verifier == verifier

    def test_an_empty_code_is_a_422(self):
        with pytest.raises(ValidationError):
            google_auth_body(code="")


class TestGoogleRedirectUriIsChecked:
    """The `redirect_uri` the browser used has to be the one `FRONTEND_URL` derives.

    A diagnostic rather than a security control - Google binds the code to the URI it saw
    and will not accept an unregistered one - so what these pin is that a `FRONTEND_URL`
    disagreeing with the origin the visitor reached fails *here*, in this app's own error,
    rather than as a `redirect_uri_mismatch` that names neither setting.
    """

    def test_the_uri_is_derived_from_frontend_url(self):
        assert settings.google_redirect_uri.endswith("/auth/google/callback")

    @pytest.mark.parametrize("frontend_url", ["https://dive.example.com", "https://dive.example.com/"])
    def test_a_trailing_slash_on_frontend_url_does_not_double_up(self, frontend_url: str):
        """Rebuilt from the parse, exactly as `passkey_origin` is, because a trailing slash
        is the most ordinary way to write a URL variable and would otherwise produce a
        `//auth/google/callback` that matches nothing Google has registered.
        """
        derived = FrontendSettings(FRONTEND_URL=frontend_url).google_redirect_uri

        assert derived == "https://dive.example.com/auth/google/callback"

    @pytest.mark.asyncio
    async def test_a_different_redirect_uri_is_refused_before_google_is_called(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.exchange_google_code", new_callable=AsyncMock) as exchange,
        ):
            with pytest.raises(BadRequestException) as raised:
                await auth_with_google(
                    _request(), google_auth_body(redirect_uri="https://evil.example.com/callback"), Mock(), mock_db
                )

            # The whole point of refusing here rather than at Google: nothing was spent.
            exchange.assert_not_called()
            detail = str(raised.value.detail)
            assert "FRONTEND_URL" in detail
            assert "https://evil.example.com/callback" in detail
            assert FRONTEND_CALLBACK in detail


class TestAuthWithGoogle:
    """`POST /auth/google` - redeeming an authorization code."""

    @pytest.mark.asyncio
    async def test_a_code_google_refuses_raises_unauthorized(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.exchange_google_code", new_callable=AsyncMock) as exchange,
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
        ):
            exchange.return_value = None

            with pytest.raises(UnauthorizedException):
                await auth_with_google(_request(), google_auth_body(), Mock(), mock_db)

            # An unredeemed code has no token to verify - the second call must not happen.
            mock_verify.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_id_token_that_does_not_check_out_raises_unauthorized(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.exchange_google_code", new_callable=AsyncMock) as exchange,
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
        ):
            exchange.return_value = "an-id-token"
            mock_verify.return_value = None

            with pytest.raises(UnauthorizedException):
                await auth_with_google(_request(), google_auth_body(), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_google_being_unreachable_is_not_reported_as_a_bad_credential(self, mock_db):
        """A 503 from the exchange travels out as a 503. Reporting "invalid credential" for
        this server's own outage would send the visitor to re-try something that was never
        their problem.
        """
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.exchange_google_code", new_callable=AsyncMock) as exchange,
        ):
            exchange.side_effect = HTTPException(status_code=503, detail="unreachable")

            with pytest.raises(HTTPException) as raised:
                await auth_with_google(_request(), google_auth_body(), Mock(), mock_db)

            assert raised.value.status_code == 503
            assert not isinstance(raised.value, UnauthorizedException)

    @pytest.mark.asyncio
    async def test_existing_google_user_signs_in(self, mock_db):
        google_user = GoogleUserInfo(google_id="g-123", email="user@example.com", name="Jane Doe")
        db_user = {"id": 1, "uuid": USER_UUID, "username": "janedoe", "email": "user@example.com", "is_deleted": False}

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.exchange_google_code", new_callable=AsyncMock) as exchange,
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            exchange.return_value = "an-id-token"
            mock_verify.return_value = google_user
            mock_providers.get = AsyncMock(return_value={"user_id": 1})
            mock_users.get = AsyncMock(return_value=db_user)

            response = Mock()
            outcome = await auth_with_google(_request(), google_auth_body(), response, mock_db)

            assert outcome.status == "authenticated"
            response.set_cookie.assert_called_once()
            mock_providers.get.assert_called_once_with(db=mock_db, provider="google", provider_user_id="g-123")

    @pytest.mark.asyncio
    async def test_all_three_body_fields_reach_the_exchange(self, mock_db):
        """The verifier is what makes the code useless to anyone who intercepted it, and
        the redirect URI is what Google matches against its registration - so both have to
        travel, not just the code.
        """
        google_user = GoogleUserInfo(google_id="g-123", email="user@example.com", name="Jane Doe")

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.exchange_google_code", new_callable=AsyncMock) as exchange,
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            exchange.return_value = "an-id-token"
            mock_verify.return_value = google_user
            mock_providers.get = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value=None)

            await auth_with_google(_request(), google_auth_body(code="the-code"), Mock(), mock_db)

            exchange.assert_awaited_once_with(
                code="the-code", code_verifier=GOOGLE_CODE_VERIFIER, redirect_uri=FRONTEND_CALLBACK
            )
            mock_verify.assert_awaited_once_with("an-id-token")

    @pytest.mark.asyncio
    async def test_links_existing_email_account(self, mock_db):
        """A magic-link account with a matching, Google-verified email gets the
        `google` provider linked onto it rather than a duplicate account created."""
        google_user = GoogleUserInfo(google_id="g-123", email="user@example.com", name="Jane Doe")
        existing_user = {
            "id": 1,
            "uuid": USER_UUID,
            "username": "janedoe",
            "email": "user@example.com",
            "is_deleted": False,
        }

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.exchange_google_code", new_callable=AsyncMock) as exchange,
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            exchange.return_value = "an-id-token"
            mock_verify.return_value = google_user
            mock_providers.get = AsyncMock(return_value=None)
            mock_providers.exists = AsyncMock(return_value=False)
            mock_providers.create = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value=existing_user)

            outcome = await auth_with_google(_request(), google_auth_body(), Mock(), mock_db)

            assert outcome.status == "authenticated"
            mock_providers.create.assert_called_once()
            created_object = mock_providers.create.call_args.kwargs["object"]
            assert created_object.user_id == 1
            assert created_object.provider == "google"
            assert created_object.provider_user_id == "g-123"

    @pytest.mark.asyncio
    async def test_new_user_gets_onboarding_session_with_prefill(self, mock_db):
        google_user = GoogleUserInfo(
            google_id="g-999", email="newperson@example.com", name="New Person", avatar="https://example.com/a.png"
        )

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.exchange_google_code", new_callable=AsyncMock) as exchange,
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            exchange.return_value = "an-id-token"
            mock_verify.return_value = google_user
            mock_providers.get = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value=None)

            outcome = await auth_with_google(_request(), google_auth_body(), Mock(), mock_db)

            assert outcome.status == "onboarding_required"
            assert outcome.email == "newperson@example.com"
            assert outcome.name == "New Person"
            assert outcome.onboarding_token is not None

            # The picture is deliberately *not* on the response - nothing renders it - but
            # it has to survive inside the token, which is the channel `POST /auth/complete`
            # imports it from. Dropping it from both would silently sever that chain, and
            # the only symptom would be Google sign-ups quietly starting with initials.
            payload = jwt.decode(outcome.onboarding_token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
            assert payload["avatar"] == "https://example.com/a.png"
            assert outcome.model_dump().get("avatar") is None


class TestCompleteProfile:
    """`POST /auth/complete` - the only place a `User` row is ever created."""

    @pytest.mark.asyncio
    async def test_invalid_onboarding_token_raises_unauthorized(self, mock_db):
        with patch("src.app.api.v1.auth.verify_onboarding_token", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = None

            with pytest.raises(UnauthorizedException):
                await complete_profile(
                    _request(),
                    ProfileCompletionRequest(onboarding_token="bad", name="New Person", username="newperson"),
                    Mock(),
                    mock_db,
                )

    @pytest.mark.asyncio
    async def test_duplicate_username_raises(self, mock_db):
        token_data = OnboardingTokenData(email="new@example.com", provider="email")

        with (
            patch("src.app.api.v1.auth.verify_onboarding_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.crud_users") as mock_users,
        ):
            mock_verify.return_value = token_data
            mock_users.exists = AsyncMock(return_value=True)

            with pytest.raises(DuplicateValueException, match="Username not available"):
                await complete_profile(
                    _request(),
                    ProfileCompletionRequest(onboarding_token="good", name="New Person", username="taken"),
                    Mock(),
                    mock_db,
                )

    @pytest.mark.asyncio
    async def test_duplicate_email_raises(self, mock_db):
        token_data = OnboardingTokenData(email="new@example.com", provider="email")

        with (
            patch("src.app.api.v1.auth.verify_onboarding_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.crud_users") as mock_users,
        ):
            mock_verify.return_value = token_data
            mock_users.exists = AsyncMock(side_effect=[False, True])  # username free, email taken

            with pytest.raises(DuplicateValueException, match="email already exists"):
                await complete_profile(
                    _request(),
                    ProfileCompletionRequest(onboarding_token="good", name="New Person", username="newperson"),
                    Mock(),
                    mock_db,
                )

    @pytest.mark.asyncio
    async def test_successful_completion_creates_user_and_provider(self, mock_db):
        token_data = OnboardingTokenData(
            email="new@example.com", provider="google", provider_user_id="g-1", name="New Person", avatar=None
        )
        created_user = Mock(id=42, uuid=USER_UUID)

        with (
            patch("src.app.api.v1.auth.verify_onboarding_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.crud_users") as mock_users,
            patch("src.app.api.v1.auth.crud_authentication_providers") as mock_providers,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock) as mock_blacklist,
        ):
            mock_verify.return_value = token_data
            mock_users.exists = AsyncMock(return_value=False)
            mock_users.create = AsyncMock(return_value=created_user)
            mock_providers.create = AsyncMock(return_value=None)
            mock_db.commit = AsyncMock(return_value=None)

            response = Mock()
            outcome = await complete_profile(
                _request(),
                ProfileCompletionRequest(onboarding_token="good", name="New Person", username="newperson"),
                response,
                mock_db,
            )

            assert outcome.status == "authenticated"
            mock_users.create.assert_called_once()
            assert mock_users.create.call_args.kwargs["commit"] is False
            mock_providers.create.assert_called_once()
            provider_kwargs = mock_providers.create.call_args.kwargs
            assert provider_kwargs["object"].user_id == 42
            assert provider_kwargs["object"].provider == "google"
            assert provider_kwargs["object"].provider_user_id == "g-1"
            assert provider_kwargs["commit"] is False
            # Two commits, and which is which is the assertion: the account transaction -
            # user, provider link and the account-created audit event, all `commit=False`
            # above so they land together - and then the session `issue_tokens` mints. A
            # third would mean something inside the account transaction had started
            # committing on its own, which is exactly what the `commit=False` kwargs above
            # exist to prevent.
            assert mock_db.commit.await_count == 2
            mock_blacklist.assert_called_once_with("good", mock_db)
            response.set_cookie.assert_called_once()

    @staticmethod
    async def _complete_with_avatar(mock_db, imported, *, avatar="https://lh3.googleusercontent.com/a/photo"):
        """Run `POST /auth/complete` for a Google identity whose token carries `avatar`,
        with the import itself stubbed, and hand back what `crud_users.create` was given."""
        token_data = OnboardingTokenData(
            email="new@example.com", provider="google", provider_user_id="g-1", name="New Person", avatar=avatar
        )

        with (
            patch("src.app.api.v1.auth.verify_onboarding_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.crud_users") as mock_users,
            patch("src.app.api.v1.auth.crud_authentication_providers") as mock_providers,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.import_google_avatar", new_callable=AsyncMock) as mock_import,
        ):
            mock_verify.return_value = token_data
            mock_users.exists = AsyncMock(return_value=False)
            mock_users.create = AsyncMock(return_value=Mock(id=42, uuid=USER_UUID))
            mock_providers.create = AsyncMock(return_value=None)
            mock_import.return_value = imported
            mock_db.commit = AsyncMock(return_value=None)

            outcome = await complete_profile(
                _request(),
                ProfileCompletionRequest(onboarding_token="good", name="New Person", username="newperson"),
                Mock(),
                mock_db,
            )

            return outcome, mock_users.create.call_args.kwargs["object"], mock_import

    @pytest.mark.asyncio
    async def test_a_google_picture_becomes_the_new_accounts_avatar(self, mock_db):
        """The columns are set on the row being created, not written afterwards: the blob is
        already on the volume by this point, so the account's single commit is what makes it
        referenced - the write-file-then-commit-row ordering, unchanged."""
        imported = StoredAvatar(storage_key="user-avatars/ab/nonce_abc", sha256="ab" * 32)

        outcome, created, mock_import = await self._complete_with_avatar(mock_db, imported)

        assert outcome.status == "authenticated"
        mock_import.assert_awaited_once_with("https://lh3.googleusercontent.com/a/photo")
        assert created.avatar_storage_key == "user-avatars/ab/nonce_abc"
        assert created.avatar_sha256 == "ab" * 32

    @pytest.mark.asyncio
    async def test_an_import_that_fails_still_creates_the_account(self, mock_db):
        """`import_google_avatar` answers `None` for every failure it can have, and this is
        why: a sign-up must not hinge on a CDN. The diver gets initials, not an error."""
        outcome, created, _ = await self._complete_with_avatar(mock_db, None)

        assert outcome.status == "authenticated"
        assert created.avatar_storage_key is None
        assert created.avatar_sha256 is None

    @pytest.mark.asyncio
    async def test_an_email_signup_has_no_picture_to_import(self, mock_db):
        _, created, mock_import = await self._complete_with_avatar(mock_db, None, avatar=None)

        mock_import.assert_awaited_once_with(None)
        assert created.avatar_storage_key is None

    @pytest.mark.asyncio
    async def test_concurrent_onboarding_rolls_back_and_raises_duplicate(self, mock_db):
        """Two requests racing to complete the same onboarding token/email: whichever
        loses the DB's unique-constraint race must fail cleanly, not create a second
        user - the pre-check (`exists`) alone can't close this race, only the DB can."""
        token_data = OnboardingTokenData(email="race@example.com", provider="email")

        with (
            patch("src.app.api.v1.auth.verify_onboarding_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.crud_users") as mock_users,
        ):
            mock_verify.return_value = token_data
            mock_users.exists = AsyncMock(return_value=False)
            mock_users.create = AsyncMock(side_effect=IntegrityError("insert", {}, Exception("duplicate key")))
            mock_db.rollback = AsyncMock(return_value=None)

            with pytest.raises(DuplicateValueException):
                await complete_profile(
                    _request(),
                    ProfileCompletionRequest(onboarding_token="good", name="Race Person", username="raceperson"),
                    Mock(),
                    mock_db,
                )

            # Two rollbacks, and only the second is the failure: the first is
            # `release_read_transaction` ending the duplicate checks' transaction before the
            # Google-picture import, so the connection does not idle across a network fetch.
            assert mock_db.rollback.await_count == 2
