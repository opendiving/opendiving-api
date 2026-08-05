import re
from datetime import timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import UnauthorizedException
from ...core.schemas import GoogleAuthRequest, Token
from ...core.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    TokenType,
    authenticate_user,
    create_access_token,
    create_refresh_token,
    verify_google_id_token,
    verify_token,
)
from ...crud.crud_users import crud_users
from ...schemas.user import UserCreateInternal, UserReadInternal

router = APIRouter(tags=["login"])


async def _generate_unique_username(base: str, db: AsyncSession) -> str:
    """Derives a unique, schema-valid username (see `UserBase.username`) from `base`
    (typically the local part of a Google account's email), appending a numeric suffix
    if needed to avoid colliding with an existing user.
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


async def _issue_tokens(response: Response, username: str) -> dict[str, str]:
    """Creates a fresh access/refresh token pair for `username`, sets the refresh
    token as an httpOnly cookie on `response`, and returns the access token - the
    common tail end of both `/login` and `/login/google`.
    """
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = await create_access_token(data={"sub": username}, expires_delta=access_token_expires)

    refresh_token = await create_refresh_token(data={"sub": username})
    max_age = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

    response.set_cookie(
        key="refresh_token", value=refresh_token, httponly=True, secure=True, samesite="lax", max_age=max_age
    )

    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/login", response_model=Token)
async def login_for_access_token(
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    user = await authenticate_user(username_or_email=form_data.username, password=form_data.password, db=db)
    if not user:
        raise UnauthorizedException("Wrong username, email or password.")

    return await _issue_tokens(response, user["username"])


@router.post("/login/google", response_model=Token)
async def login_with_google(
    response: Response,
    body: GoogleAuthRequest,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Signs in with Google, transparently signing up (creating an account) on first
    use - the frontend's "Continue with Google" button on both the sign in and sign up
    pages hits this same endpoint, matching how Google Identity Services itself treats
    sign in/up as a single flow.
    """
    google_user = await verify_google_id_token(body.credential)
    if google_user is None:
        raise UnauthorizedException("Invalid Google credential.")

    db_user = await crud_users.get(db=db, google_id=google_user.google_id, is_deleted=False)

    if db_user is None:
        db_user = await crud_users.get(db=db, email=google_user.email, is_deleted=False)

        if db_user is not None:
            # An account with this (Google-verified) email already exists from a
            # regular sign-up - link the Google account to it rather than erroring
            # out or creating a duplicate account.
            await crud_users.update(db=db, object={"google_id": google_user.google_id}, uuid=db_user["uuid"])
        else:
            username = await _generate_unique_username(google_user.email.split("@")[0], db)
            user_internal = UserCreateInternal(
                name=google_user.name,
                username=username,
                email=google_user.email,
                google_id=google_user.google_id,
            )
            db_user_model = await crud_users.create(
                db=db, object=user_internal, schema_to_select=UserReadInternal, return_as_model=True
            )
            db_user = await crud_users.get(db=db, id=db_user_model.id, is_deleted=False)

    db_user = cast(dict[str, Any], db_user)
    return await _issue_tokens(response, db_user["username"])


@router.post("/refresh")
async def refresh_access_token(request: Request, db: AsyncSession = Depends(async_get_db)) -> dict[str, str]:
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise UnauthorizedException("Refresh token missing.")

    user_data = await verify_token(refresh_token, TokenType.REFRESH, db)
    if not user_data:
        raise UnauthorizedException("Invalid refresh token.")

    new_access_token = await create_access_token(data={"sub": user_data.username_or_email})
    return {"access_token": new_access_token, "token_type": "bearer"}
