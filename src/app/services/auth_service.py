"""Shared logic behind every "I've verified an identity, now what?" decision in the
unified auth flow - used by both `POST /auth/email/verify` and `POST /auth/google`
(see `resolve_identity`), and the token-issuing tail end shared by every endpoint that
signs a user in (`issue_tokens`).

The question has three answers, not two: an account exists, no account exists yet, or one
exists and is inside its deletion grace period (`DeletionPending`). The passkey flow
answers the same three from its own resolve site - `services.passkey_service.finish_sign_in`
- because an assertion carries no email for this module to look anything up by.
"""

import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.security import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token, create_refresh_token
from ..crud.crud_authentication_providers import crud_authentication_providers
from ..crud.crud_users import crud_users
from ..schemas.authentication_provider import AuthenticationProviderCreate


async def issue_tokens(response: Response, user_uuid: uuid_pkg.UUID) -> dict[str, str]:
    """Creates a fresh access/refresh token pair for the user with public id
    `user_uuid`, sets the refresh token as an httpOnly cookie on `response`, and
    returns the access token - the common tail end of every flow that signs a user in.

    The subject is the immutable `uuid` rather than the username these tokens used to
    name. A username is editable (`PATCH /user`) and is released for anyone to claim
    the instant it changes, with no cooldown - so a username subject is a session whose
    identity someone else can assume simply by taking the name, and `/auth/refresh`
    re-mints whatever subject it's handed, keeping such a token alive indefinitely - the
    liveness check that endpoint now makes does not close that, because the stolen name
    resolves perfectly well to whoever holds it. The same defect signed the *renaming* user
    out permanently. See DECISIONS.md.
    """
    subject = str(user_uuid)
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = await create_access_token(data={"sub": subject}, expires_delta=access_token_expires)

    refresh_token = await create_refresh_token(data={"sub": subject})
    max_age = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        # `AUTH_COOKIE_SECURE` (default true) rather than a literal: a LAN instance with no
        # certificate serves plain HTTP, where the browser drops a `Secure` cookie without
        # a word - the user is signed out on the next reload and nothing logs why.
        secure=settings.AUTH_COOKIE_SECURE,
        samesite="lax",
        max_age=max_age,
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


@dataclass
class DeletionPending:
    """The identity resolves to an account inside its deletion grace period.

    Not a sign-in and not onboarding: the row is still there and still deleted, and the
    caller is offered the account back rather than being given it. Signing in must not
    silently cancel a deletion somebody deliberately asked for, so a restore is its own
    explicit click on every path that can reach one.

    `purge_after` is `None` only in the state `purge_deleted_accounts` warns about - a row
    flagged with no `deleted_at` to count from, which nothing in the app writes. The screen
    then has no date to show, and the restore itself works exactly the same.
    """

    user: dict[str, Any]
    purge_after: datetime | None

    @classmethod
    def for_row(cls, user: dict[str, Any]) -> DeletionPending:
        """Wraps a soft-deleted row with the date its grace period runs out - the one place
        `deleted_at + ACCOUNT_DELETION_GRACE_DAYS` is computed for a resolve site, shared by
        both of `resolve_identity`'s lookups and by `passkey_service.finish_sign_in`.
        """
        deleted_at = user.get("deleted_at")
        purge_after = (
            deleted_at + timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS) if deleted_at is not None else None
        )
        return cls(user=user, purge_after=purge_after)


async def resolve_identity(
    db: AsyncSession,
    *,
    provider: str,
    email: str,
    provider_user_id: str | None = None,
    name: str | None = None,
    avatar: str | None = None,
) -> AuthenticatedUser | OnboardingRequired | DeletionPending:
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

    **Neither lookup filters `is_deleted` any more**, and both branches answer
    `DeletionPending` for a flagged row. Relaxing only the email one would leave a real
    hole rather than half a feature: somebody who signed up with Google and later changed
    their account email (`verify_email_change` rewrites the address on the row) is
    reachable *only* by provider link, so after deletion the first lookup would miss on
    the filter, the second would miss on the address, and they would fall through to
    `OnboardingRequired` - and `/auth/complete` would hand them a **second account** while
    the first sat waiting to be purged.
    """
    if provider_user_id is not None:
        existing_link = await crud_authentication_providers.get(
            db=db, provider=provider, provider_user_id=provider_user_id
        )
        if existing_link is not None:
            linked_user = await crud_users.get(db=db, id=existing_link["user_id"])
            if linked_user is not None:
                linked_user = cast(dict[str, Any], linked_user)
                if linked_user["is_deleted"]:
                    return DeletionPending.for_row(linked_user)
                return AuthenticatedUser(user=linked_user)

    user = await crud_users.get(db=db, email=email)
    if user is not None:
        user = cast(dict[str, Any], user)
        if user["is_deleted"]:
            # Before the provider link below, not after: an account pending deletion is not
            # a place to write to. Nothing is lost by waiting - the restore signs them in,
            # and the next sign-in through this provider links it against a live row.
            return DeletionPending.for_row(user)

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
