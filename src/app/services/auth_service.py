"""Shared logic behind every "I've verified an identity, now what?" decision in the
unified auth flow - used by both `POST /auth/email/verify` and `POST /auth/google`
(see `resolve_identity`), and the token-issuing tail end shared by every endpoint that
signs a user in (`issue_tokens`).
"""

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.security import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token, create_refresh_token
from ..crud.crud_authentication_providers import crud_authentication_providers
from ..crud.crud_users import crud_users
from ..schemas.authentication_provider import AuthenticationProviderCreate


async def generate_unique_username(base: str, db: AsyncSession) -> str:
    """Derives a unique, schema-valid username (see `UserBase.username`) from `base`
    (typically the local part of an email address), appending a numeric suffix if
    needed to avoid colliding with an existing user.
    """
    sanitized = re.sub(r"[^a-z0-9]", "", base.lower())[:20]
    if len(sanitized) < 2:
        sanitized = (sanitized + "user")[:20]

    candidate = sanitized
    suffix = 0
    while await crud_users.exists(db=db, username=candidate):
        suffix += 1
        suffix_str = str(suffix)
        candidate = f"{sanitized[: 20 - len(suffix_str)]}{suffix_str}"

    return candidate


async def issue_tokens(response: Response, username: str) -> dict[str, str]:
    """Creates a fresh access/refresh token pair for `username`, sets the refresh
    token as an httpOnly cookie on `response`, and returns the access token - the
    common tail end of every flow that signs a user in.
    """
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = await create_access_token(data={"sub": username}, expires_delta=access_token_expires)

    refresh_token = await create_refresh_token(data={"sub": username})
    max_age = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

    response.set_cookie(
        key="refresh_token", value=refresh_token, httponly=True, secure=True, samesite="lax", max_age=max_age
    )

    return {"access_token": access_token, "token_type": "bearer"}


@dataclass
class AuthenticatedUser:
    """An existing account was found for the verified identity - ready to sign in."""

    user: dict[str, Any]


@dataclass
class OnboardingRequired:
    """No account exists yet for the verified identity - the caller should mint an
    onboarding token (`create_onboarding_token`) and send the user to profile
    completion instead of signing them in.
    """

    email: str
    provider: str
    provider_user_id: str | None
    name: str | None
    avatar: str | None


async def resolve_identity(
    db: AsyncSession,
    *,
    provider: str,
    email: str,
    provider_user_id: str | None = None,
    name: str | None = None,
    avatar: str | None = None,
) -> AuthenticatedUser | OnboardingRequired:
    """Resolves a verified identity - an email address (and, for Google, a stable
    provider subject id) the caller has already proven ownership of - to either an
    existing account or a signal that onboarding should start.

    This is the one place both `/auth/email/verify` and `/auth/google` funnel through,
    so "does an account already exist, and if so is this provider linked to it yet" is
    answered identically for both:

    1. If `provider_user_id` is given (Google) and already linked to *some* account,
       that account owns this identity outright - sign in as them.
    2. Otherwise, look up by `email`. If found, link `provider` onto that account (if
       it isn't already) - this is what lets an email-created account later sign in
       with Google (or vice versa) without creating a duplicate user.
    3. Otherwise, no account exists yet - the caller should start onboarding.
    """
    if provider_user_id is not None:
        existing_link = await crud_authentication_providers.get(
            db=db, provider=provider, provider_user_id=provider_user_id
        )
        if existing_link is not None:
            linked_user = await crud_users.get(db=db, id=existing_link["user_id"], is_deleted=False)
            if linked_user is not None:
                return AuthenticatedUser(user=cast(dict[str, Any], linked_user))

    user = await crud_users.get(db=db, email=email, is_deleted=False)
    if user is not None:
        user = cast(dict[str, Any], user)
        already_linked = await crud_authentication_providers.exists(db=db, user_id=user["id"], provider=provider)
        if not already_linked:
            await crud_authentication_providers.create(
                db=db,
                object=AuthenticationProviderCreate(
                    user_id=user["id"], provider=provider, provider_user_id=provider_user_id
                ),
            )
        return AuthenticatedUser(user=user)

    return OnboardingRequired(
        email=email, provider=provider, provider_user_id=provider_user_id, name=name, avatar=avatar
    )
