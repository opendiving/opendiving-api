"""Unit tests for the unified auth flow (`api.v1.auth`): email magic link, Google, and
profile completion. See `tests/test_auth_service.py` for the shared identity-resolution
logic, and `tests/test_security.py` for the underlying token helpers.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from src.app.api.v1.auth import (
    auth_with_google,
    check_email_link,
    complete_profile,
    request_email_link,
    verify_email_link,
)
from src.app.core.exceptions.http_exceptions import DuplicateValueException, RateLimitException, UnauthorizedException
from src.app.core.schemas import GoogleUserInfo, OnboardingTokenData
from src.app.schemas.auth import EmailAuthRequest, EmailVerifyRequest, GoogleAuthRequest, ProfileCompletionRequest


def _request(ip: str = "1.2.3.4") -> Mock:
    request = Mock()
    request.client = Mock(host=ip)
    return request


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
            mock_crud.create = AsyncMock(return_value=None)

            result = await request_email_link(_request(), EmailAuthRequest(email="new@example.com"), mock_db)

            assert result.message == "Check your email for the next step."
            mock_send.assert_called_once()
            assert mock_send.call_args.kwargs["email"] == "new@example.com"
            mock_crud.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_never_queries_whether_the_user_exists(self, mock_db):
        """Email enumeration protection: the response (and the code path leading to
        it) must be identical whether or not an account exists for this email - this
        endpoint doesn't even look at `crud_users`."""
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.auth.crud_users") as mock_users,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
        ):
            mock_crud.count = AsyncMock(return_value=0)
            mock_crud.update = AsyncMock(return_value=None)
            mock_crud.create = AsyncMock(return_value=None)

            result = await request_email_link(_request(), EmailAuthRequest(email="anyone@example.com"), mock_db)

            assert result.message == "Check your email for the next step."
            mock_users.exists.assert_not_called()
            mock_users.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalidates_previous_pending_tokens_for_the_same_email(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
        ):
            mock_crud.count = AsyncMock(return_value=1)
            mock_crud.update = AsyncMock(return_value=None)
            mock_crud.create = AsyncMock(return_value=None)

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
            mock_crud.create = AsyncMock(return_value=None)

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
                await check_email_link(_request(), "any", mock_db)

    @pytest.mark.asyncio
    async def test_not_found_is_invalid(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_crud,
        ):
            mock_crud.get = AsyncMock(return_value=None)

            result = await check_email_link(_request(), "bad", mock_db)

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

            result = await check_email_link(_request(), "stale", mock_db)

            assert result.valid is False

    @pytest.mark.asyncio
    async def test_already_used_token_is_invalid(self, mock_db):
        """This is what actually keeps a human from re-triggering sign-in after
        already following the link once (e.g. via the browser's back button) -
        unlike `verify_email_link`'s idempotent-reuse leniency, this precheck must
        treat an already-used token as invalid so no button is even shown."""
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

            result = await check_email_link(_request(), "already-used", mock_db)

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

            result = await check_email_link(_request(), "expired", mock_db)

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
        ):
            mock_crud.get = AsyncMock(return_value=auth_request)

            result = await check_email_link(_request(), "good", mock_db)

            assert result.valid is True
            assert result.email == "a@example.com"


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
        """Superseded by a newer request (see `request_email_link`) - a hard reject,
        unlike a merely-already-used-but-still-live token (see the idempotent-reuse
        test below)."""
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
    async def test_already_used_but_live_token_succeeds_again(self, mock_db):
        """Re-opening the same link (e.g. a mail client's link-preview/security-
        scanning feature having already "detonated" it, or the user clicking twice)
        must not error - it just re-confirms the same outcome, and shouldn't
        re-touch `used_at` a second time."""
        auth_request = {
            "id": 1,
            "email": "existing@example.com",
            "used_at": datetime.now(UTC),
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        db_user = {"id": 1, "username": "existinguser", "email": "existing@example.com"}

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_requests.update = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value=db_user)
            mock_providers.exists = AsyncMock(return_value=True)

            outcome = await verify_email_link(_request(), EmailVerifyRequest(token="already-used"), Mock(), mock_db)

            assert outcome.status == "authenticated"
            mock_requests.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_user_is_authenticated_and_token_marked_used(self, mock_db):
        auth_request = {
            "id": 1,
            "email": "existing@example.com",
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        db_user = {"id": 1, "username": "existinguser", "email": "existing@example.com"}

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as mock_requests,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_requests.get = AsyncMock(return_value=auth_request)
            mock_requests.update = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value=db_user)
            mock_providers.exists = AsyncMock(return_value=True)

            response = Mock()
            outcome = await verify_email_link(_request(), EmailVerifyRequest(token="good"), response, mock_db)

            assert outcome.status == "authenticated"
            assert outcome.access_token is not None
            response.set_cookie.assert_called_once()
            mock_requests.update.assert_called_once()
            update_kwargs = mock_requests.update.call_args.kwargs
            assert update_kwargs["id"] == 1
            assert update_kwargs["object"].used_at is not None

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
            mock_requests.update = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value=None)

            outcome = await verify_email_link(_request(), EmailVerifyRequest(token="good"), Mock(), mock_db)

            assert outcome.status == "onboarding_required"
            assert outcome.onboarding_token is not None
            assert outcome.email == "new@example.com"


class TestAuthWithGoogle:
    """`POST /auth/google`."""

    @pytest.mark.asyncio
    async def test_invalid_credential_raises_unauthorized(self, mock_db):
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
        ):
            mock_verify.return_value = None

            with pytest.raises(UnauthorizedException):
                await auth_with_google(_request(), GoogleAuthRequest(credential="bad"), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_existing_google_user_signs_in(self, mock_db):
        google_user = GoogleUserInfo(google_id="g-123", email="user@example.com", name="Jane Doe")
        db_user = {"id": 1, "username": "janedoe", "email": "user@example.com"}

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_verify.return_value = google_user
            mock_providers.get = AsyncMock(return_value={"user_id": 1})
            mock_users.get = AsyncMock(return_value=db_user)

            response = Mock()
            outcome = await auth_with_google(_request(), GoogleAuthRequest(credential="good"), response, mock_db)

            assert outcome.status == "authenticated"
            response.set_cookie.assert_called_once()
            mock_providers.get.assert_called_once_with(db=mock_db, provider="google", provider_user_id="g-123")

    @pytest.mark.asyncio
    async def test_links_existing_email_account(self, mock_db):
        """A magic-link account with a matching, Google-verified email gets the
        `google` provider linked onto it rather than a duplicate account created."""
        google_user = GoogleUserInfo(google_id="g-123", email="user@example.com", name="Jane Doe")
        existing_user = {"id": 1, "username": "janedoe", "email": "user@example.com"}

        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_verify.return_value = google_user
            mock_providers.get = AsyncMock(return_value=None)
            mock_providers.exists = AsyncMock(return_value=False)
            mock_providers.create = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value=existing_user)

            outcome = await auth_with_google(_request(), GoogleAuthRequest(credential="good"), Mock(), mock_db)

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
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_verify.return_value = google_user
            mock_providers.get = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value=None)

            outcome = await auth_with_google(_request(), GoogleAuthRequest(credential="good"), Mock(), mock_db)

            assert outcome.status == "onboarding_required"
            assert outcome.email == "newperson@example.com"
            assert outcome.name == "New Person"
            assert outcome.avatar == "https://example.com/a.png"
            assert outcome.onboarding_token is not None


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
        created_user = Mock(id=42)

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
            mock_db.commit.assert_called_once()
            mock_blacklist.assert_called_once_with("good", mock_db)
            response.set_cookie.assert_called_once()

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

            mock_db.rollback.assert_called_once()
