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
from ...crud.crud_users import crud_users
from ...schemas.authentication_request import AuthenticationRequestCreate, AuthenticationRequestUpdate
from ...schemas.email_change import (
    EmailChangeRequest,
    EmailChangeRequestResponse,
    EmailChangeVerifyRequest,
    EmailChangeVerifyResponse,
)
from ...schemas.user import UserRead, UserReadInternal, UserUpdate
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
    # `POST /user/{uuid}/email-change/request` for how email changes work instead.
    if values.username is not None and values.username != db_username:
        if await crud_users.exists(db=db, username=values.username):
            raise DuplicateValueException("Username not available")

    await crud_users.update(db=db, object=values, uuid=uuid)
    return {"message": "User updated"}


@router.post("/user/{uuid}/email-change/request", response_model=EmailChangeRequestResponse)
async def request_email_change(
    request: Request,
    uuid: uuid_pkg.UUID,
    body: EmailChangeRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> EmailChangeRequestResponse:
    """Step 1 of changing an account's email: generates a single-use confirmation
    link and emails it to the *new* address - the change only takes effect once that
    link is clicked (`POST /user/email-change/verify`), proving the caller actually
    controls it.

    Always returns the same generic message, whether or not `new_email` already
    belongs to another account.
    """
    if current_user["uuid"] != uuid:
        raise ForbiddenException()

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

    # Only one active change request per user at a time, regardless of which target
    # address a previous (still-pending) request was for.
    pending_count = await crud_authentication_requests.count(
        db, user_id=current_user["id"], purpose="email_change", used_at=None
    )
    if pending_count > 0:
        await crud_authentication_requests.update(
            db=db,
            object=AuthenticationRequestUpdate(used_at=datetime.now(UTC)),
            allow_multiple=True,
            user_id=current_user["id"],
            purpose="email_change",
            used_at=None,
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

    new_email = auth_request["email"]
    user_id = auth_request["user_id"]

    db_user = await crud_users.get(db=db, id=user_id, is_deleted=False)
    if db_user is None:
        raise NotFoundException("User not found")

    current_email = db_user["email"] if isinstance(db_user, dict) else db_user.email

    if auth_request["used_at"] is not None:
        # Already used - most likely a mail client's link-preview/security-scanning
        # feature (many run a real JS-executing browser to "detonate" links before a
        # human ever clicks) rather than a genuine reuse attempt. If the account's
        # email already matches what this token would have set, the change this
        # token represents has already gone through - report success instead of a
        # confusing "invalid link" error for something that, in fact, already
        # worked. Only a token that's used *and* doesn't match the current state is
        # treated as a real (rejected) reuse.
        if current_email == new_email:
            return EmailChangeVerifyResponse(email=new_email)
        raise UnauthorizedException("This confirmation link has already been used.")

    expires_at = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise UnauthorizedException("This confirmation link has expired.")

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
