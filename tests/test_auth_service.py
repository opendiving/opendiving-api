"""Unit tests for `services.auth_service` - the identity-resolution and token-issuing
logic shared by the email and Google auth flows.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.app.services.auth_service import (
    AuthenticatedUser,
    OnboardingRequired,
    generate_unique_username,
    issue_tokens,
    resolve_identity,
)


class TestGenerateUniqueUsername:
    @pytest.mark.asyncio
    async def test_returns_sanitized_base_when_available(self, mock_db):
        with patch("src.app.services.auth_service.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=False)

            username = await generate_unique_username("Jane.Doe123", mock_db)

            assert username == "janedoe123"

    @pytest.mark.asyncio
    async def test_appends_suffix_on_collision(self, mock_db):
        with patch("src.app.services.auth_service.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(side_effect=[True, False])

            username = await generate_unique_username("janedoe", mock_db)

            assert username == "janedoe1"
            assert mock_crud.exists.call_count == 2

    @pytest.mark.asyncio
    async def test_pads_too_short_base(self, mock_db):
        with patch("src.app.services.auth_service.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=False)

            username = await generate_unique_username("a", mock_db)

            assert username == "auser"


class TestIssueTokens:
    @pytest.mark.asyncio
    async def test_sets_refresh_cookie_and_returns_access_token(self):
        response = Mock()

        tokens = await issue_tokens(response, "someuser")

        assert tokens["token_type"] == "bearer"
        assert "access_token" in tokens
        response.set_cookie.assert_called_once()
        kwargs = response.set_cookie.call_args.kwargs
        assert kwargs["key"] == "refresh_token"
        assert kwargs["httponly"] is True
        assert kwargs["samesite"] == "lax"


class TestResolveIdentity:
    @pytest.mark.asyncio
    async def test_existing_provider_link_signs_in_directly(self, mock_db):
        with (
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_providers.get = AsyncMock(return_value={"user_id": 7})
            mock_users.get = AsyncMock(return_value={"id": 7, "username": "someone"})

            outcome = await resolve_identity(
                mock_db, provider="google", email="someone@example.com", provider_user_id="g-1"
            )

            assert isinstance(outcome, AuthenticatedUser)
            assert outcome.user["id"] == 7
            mock_users.get.assert_called_once_with(db=mock_db, id=7, is_deleted=False)

    @pytest.mark.asyncio
    async def test_links_provider_onto_existing_user_found_by_email(self, mock_db):
        with (
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_providers.get = AsyncMock(return_value=None)
            mock_providers.exists = AsyncMock(return_value=False)
            mock_providers.create = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value={"id": 3, "username": "existing"})

            outcome = await resolve_identity(
                mock_db, provider="google", email="existing@example.com", provider_user_id="g-2"
            )

            assert isinstance(outcome, AuthenticatedUser)
            mock_providers.create.assert_called_once()
            created = mock_providers.create.call_args.kwargs["object"]
            assert created.user_id == 3
            assert created.provider == "google"
            assert created.provider_user_id == "g-2"

    @pytest.mark.asyncio
    async def test_does_not_relink_an_already_linked_provider(self, mock_db):
        with (
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_providers.get = AsyncMock(return_value=None)
            mock_providers.exists = AsyncMock(return_value=True)
            mock_providers.create = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value={"id": 3, "username": "existing"})

            outcome = await resolve_identity(mock_db, provider="email", email="existing@example.com")

            assert isinstance(outcome, AuthenticatedUser)
            mock_providers.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_matching_user_returns_onboarding_required(self, mock_db):
        with (
            patch("src.app.services.auth_service.crud_authentication_providers") as mock_providers,
            patch("src.app.services.auth_service.crud_users") as mock_users,
        ):
            mock_providers.get = AsyncMock(return_value=None)
            mock_users.get = AsyncMock(return_value=None)

            outcome = await resolve_identity(
                mock_db, provider="google", email="new@example.com", provider_user_id="g-3", name="New", avatar="a"
            )

            assert isinstance(outcome, OnboardingRequired)
            assert outcome.email == "new@example.com"
            assert outcome.provider == "google"
            assert outcome.provider_user_id == "g-3"
            assert outcome.name == "New"
            assert outcome.avatar == "a"
