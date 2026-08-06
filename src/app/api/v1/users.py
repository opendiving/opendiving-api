import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Cookie, Depends, Request, Response
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    BadRequestException,
    DuplicateValueException,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
)
from ...core.security import blacklist_token, blacklist_tokens, generate_secure_token, hash_token, oauth2_scheme
from ...core.utils.rate_limit import enforce_rate_limit
from ...crud.crud_authentication_requests import crud_authentication_requests
from ...crud.crud_user_dive_stats import crud_user_dive_stats
from ...crud.crud_users import crud_users
from ...schemas.authentication_request import AuthenticationRequestCreate, AuthenticationRequestUpdate
from ...schemas.email_change import (
    EmailChangeRequest,
    EmailChangeRequestResponse,
    EmailChangeVerifyRequest,
    EmailChangeVerifyResponse,
)
from ...schemas.user import UserRead, UserReadInternal, UserUpdate
from ...schemas.user_dive_stats import UserDiveStatsRead, UserDiveStatsReadInternal
from ...services.email_service import send_email_change_confirmation_email, send_email_changed_notification

router = APIRouter(tags=["users"])

# Note: there is no `POST /user` here - account creation only ever happens via
# `POST /auth/complete` (see `api.v1.auth`), after an identity (email or Google) has
# already been verified. There is no separate signup flow.

_EMAIL_CHANGE_REQUEST_RESPONSE = EmailChangeRequestResponse()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/users", response_model=PaginatedListResponse[UserRead], dependencies=[Depends(get_current_user)])
async def read_users(
    request: Request, db: Annotated[AsyncSession, Depends(async_get_db)], page: int = 1, items_per_page: int = 10
) -> dict:
    users_data = await crud_users.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        is_deleted=False,
    )

    response: dict[str, Any] = paginated_response(crud_data=users_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/user/me", response_model=UserRead)
async def read_users_me(request: Request, current_user: Annotated[dict, Depends(get_current_user)]) -> dict:
    return current_user


@router.get("/user/{uuid}", response_model=UserRead, dependencies=[Depends(get_current_user)])
async def read_user(
    request: Request, uuid: uuid_pkg.UUID, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> UserRead:
    db_user = await crud_users.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=UserRead, return_as_model=True
    )
    if db_user is None:
        raise NotFoundException("User not found")

    return cast(UserRead, db_user)


@router.patch("/user/{uuid}")
async def patch_user(
    request: Request,
    values: UserUpdate,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_user = await crud_users.get(db=db, uuid=uuid)
    if db_user is None:
        raise NotFoundException("User not found")

    db_username = db_user["username"] if isinstance(db_user, dict) else db_user.username

    if current_user["uuid"] != uuid:
        raise ForbiddenException()

    # Note: `email` is deliberately not part of `UserUpdate` - see
    # `POST /user/email-change/request` for how email changes work instead.
    if values.username is not None and values.username != db_username:
        if await crud_users.exists(db=db, username=values.username):
            raise DuplicateValueException("Username not available")

    await crud_users.update(db=db, object=values, uuid=uuid)
    return {"message": "User updated"}


@router.post("/user/email-change/request", response_model=EmailChangeRequestResponse)
async def request_email_change(
    request: Request,
    body: EmailChangeRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> EmailChangeRequestResponse:
    """Step 1 of changing an account's email: generates a confirmation link and
    emails it to the *new* address - the change only takes effect once that link is
    opened (`POST /user/email-change/verify`), proving the caller actually controls
    it.

    Always operates on the caller's own account (from the access token), not a path
    parameter - there is no other account to target.

    Always returns the same generic message, whether or not `new_email` already
    belongs to another account.
    """
    new_email = body.new_email.lower()
    if new_email == current_user["email"].lower():
        raise BadRequestException("That's already your email address.")

    await enforce_rate_limit(
        f"email-change:user:{current_user['id']}",
        settings.EMAIL_CHANGE_REQUEST_RATE_LIMIT_PER_USER,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"email-change:ip:{_client_ip(request)}",
        settings.EMAIL_CHANGE_REQUEST_RATE_LIMIT_PER_USER * 5,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    # Only one *live* change request per user at a time, regardless of which target
    # address a previous one was for.
    pending_count = await crud_authentication_requests.count(
        db, user_id=current_user["id"], purpose="email_change", invalidated_at=None
    )
    if pending_count > 0:
        await crud_authentication_requests.update(
            db=db,
            object=AuthenticationRequestUpdate(invalidated_at=datetime.now(UTC)),
            allow_multiple=True,
            user_id=current_user["id"],
            purpose="email_change",
            invalidated_at=None,
        )

    raw_token = generate_secure_token()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES)
    await crud_authentication_requests.create(
        db=db,
        object=AuthenticationRequestCreate(
            email=new_email,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
            purpose="email_change",
            user_id=current_user["id"],
        ),
    )

    confirm_url = f"{settings.FRONTEND_URL}/settings/confirm-email?token={raw_token}"
    await send_email_change_confirmation_email(new_email=new_email, confirm_url=confirm_url)

    return _EMAIL_CHANGE_REQUEST_RESPONSE


@router.post("/user/email-change/verify", response_model=EmailChangeVerifyResponse)
async def verify_email_change(
    request: Request,
    body: EmailChangeVerifyRequest,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> EmailChangeVerifyResponse:
    """Step 2: validates the confirmation link and, if it checks out, applies the
    email change. Deliberately doesn't require the caller to be signed in - the link
    may well be opened on a different device/browser than the one the change was
    requested from - the token itself, tied to a specific `user_id`, is what
    authorizes this.

    Re-opening/re-verifying the *same* link again (e.g. a mail client's
    link-preview/security-scanning feature "detonating" it before a human clicks, or
    the user clicking twice) is deliberately not an error - it's a no-op that just
    reports the change as already applied. Only an *expired* link, or one superseded
    by a newer request, is rejected - see `AuthenticationRequest.invalidated_at`.
    """
    await enforce_rate_limit(
        f"email-change-verify:ip:{_client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(
        db=db, token_hash=hash_token(body.token), purpose="email_change"
    )
    if auth_request is None:
        raise UnauthorizedException("This confirmation link is invalid.")

    if auth_request["invalidated_at"] is not None:
        raise UnauthorizedException("This confirmation link is no longer valid - a newer request was made.")

    new_email = auth_request["email"]
    user_id = auth_request["user_id"]

    # Already applied - a harmless repeat (see docstring above). Skip straight to
    # the same success response without re-touching the DB or re-notifying anyone.
    if auth_request["used_at"] is not None:
        return EmailChangeVerifyResponse(email=new_email)

    expires_at = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise UnauthorizedException("This confirmation link has expired.")

    db_user = await crud_users.get(db=db, id=user_id, is_deleted=False)
    if db_user is None:
        raise NotFoundException("User not found")

    current_email = db_user["email"] if isinstance(db_user, dict) else db_user.email

    if await crud_users.exists(db=db, email=new_email):
        raise DuplicateValueException("Email is already registered to another account")

    try:
        await crud_users.update(db=db, object={"email": new_email}, id=user_id)
        await crud_authentication_requests.update(
            db=db, object=AuthenticationRequestUpdate(used_at=datetime.now(UTC)), id=auth_request["id"]
        )
    except IntegrityError:
        await db.rollback()
        raise DuplicateValueException("Email is already registered to another account") from None

    await send_email_changed_notification(old_email=current_email, new_email=new_email)

    return EmailChangeVerifyResponse(email=new_email)


@router.get("/user/{uuid}/dive-stats", response_model=UserDiveStatsRead)
async def read_dive_stats(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> UserDiveStatsRead:
    if current_user["uuid"] != uuid:
        raise ForbiddenException()

    stats = await crud_user_dive_stats.get(
        db=db, user_id=current_user["id"], schema_to_select=UserDiveStatsReadInternal, return_as_model=True
    )
    if stats is None:
        # No dives logged yet - return zeroed-out stats rather than 404, since
        # every user conceptually has stats, they just haven't been created yet.
        # total_dives/max_depth/total_time/species_seen have Pydantic defaults, but mypy's
        # pydantic plugin doesn't recognize defaults declared via `Annotated[..., Field(default=...)]`.
        return UserDiveStatsRead(user_uuid=uuid, created_at=datetime.now(UTC))  # type: ignore[call-arg]

    stats = cast(UserDiveStatsReadInternal, stats)
    return UserDiveStatsRead(**{k: v for k, v in stats.model_dump().items() if k != "user_id"}, user_uuid=uuid)


@router.delete("/user/{uuid}")
async def erase_user(
    request: Request,
    response: Response,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    access_token: str = Depends(oauth2_scheme),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
) -> dict[str, str]:
    db_user = await crud_users.get(db=db, uuid=uuid, schema_to_select=UserReadInternal)
    if not db_user:
        raise NotFoundException("User not found")

    if current_user["uuid"] != uuid:
        raise ForbiddenException()

    await crud_users.delete(db=db, uuid=uuid)

    if refresh_token:
        await blacklist_tokens(access_token=access_token, refresh_token=refresh_token, db=db)
        response.delete_cookie(key="refresh_token")
    else:
        await blacklist_token(token=access_token, db=db)

    return {"message": "User deleted"}
