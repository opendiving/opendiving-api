"""The connect link: three routes for the member who owns it, two anonymous ones for whoever
holds it, and one for a holder who is signed in.

It exists in every registration mode. On an invite-only instance it also admits a newcomer
under the owner's invitation quota, which `services.registration_gate` decides; nothing here
reads the mode.

**The token is the credential** for the anonymous half, and every refusal - an unknown, reset
or removed token, an owner who has asked to be deleted - is one identical 404. Each response
sets `Cache-Control: private, no-store` itself, the 404s included, which is why they are
returned rather than raised (`api.account_avatar`): a reset has to stop the old link answering
at the next request. No rate limiter: the token has 128 bits, which no guessing reaches.

The anonymous read answers the owner's name and picture and never the username, which only a
caller with a session is told: a forwarded QR code then yields a username to nobody who asks
the API without an account.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.account_avatar import serve_account_avatar
from ...api.dependencies import get_current_user, refuse_api_keys
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.utils.binary_responses import uncacheable_not_found
from ...core.utils.request_context import RequestContext
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_people import person_connected_to_uuid
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.connect_link import (
    ConnectLinkOwner,
    ConnectLinkOwnerForYou,
    ConnectLinkRead,
    ConnectLinkRemovedResponse,
)
from ...services.connect_links import live_connect_link, read_connect_link, remove_connect_link, replace_connect_link

router = APIRouter(tags=["people"])

_NO_STORE = "private, no-store"
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "Unknown, reset or removed, or an owner asking to be deleted: one answer"}
}


_NO_SUCH_LINK = "This connect link is not active."


def _no_such_link() -> JSONResponse:
    return uncacheable_not_found(_NO_SUCH_LINK)


@router.get("/user/connect-link", response_model=ConnectLinkRead, dependencies=[Depends(refuse_api_keys)])
async def read_own_connect_link(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> ConnectLinkRead:
    """The caller's connect link, or 404 when they have none.

    Needs a signed-in session; an API key is refused with a 403. The `token` is in the
    answer every time, so a client can show the same link and QR code on every visit.
    """
    link = await read_connect_link(db, user_id=current_user["id"])
    if link is None:
        raise NotFoundException("No connect link")
    return ConnectLinkRead(token=link.token, created_at=link.created_at)


@router.post(
    "/user/connect-link", response_model=ConnectLinkRead, status_code=201, dependencies=[Depends(refuse_api_keys)]
)
async def make_connect_link(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> ConnectLinkRead:
    """Make the caller's connect link, or reset it: a fresh token either way.

    Whoever follows `/connect/<token>` sees the caller's name and picture and may put the
    caller in their people, connected, which shows the caller under their *Added you*; on an
    invite-only instance a newcomer may create an account through it, counted as the caller's
    invitation and charged to their quota. A reset replaces the token in place, so the
    previous one stops resolving at once; the ties it made stay. Needs a signed-in session;
    an API key is refused with a 403.
    """
    # No rate limiter: minting is authenticated, and a member pressing it repeatedly churns
    # one row of their own.
    token, created_at = await replace_connect_link(db, user_id=current_user["id"])
    await record_auth_event(
        db,
        event_type=AuthEventType.CONNECT_LINK_CREATED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
        commit=False,
    )
    await db.commit()
    return ConnectLinkRead(token=token, created_at=created_at)


@router.delete("/user/connect-link", response_model=ConnectLinkRemovedResponse, dependencies=[Depends(refuse_api_keys)])
async def erase_connect_link(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> ConnectLinkRemovedResponse:
    """Remove the caller's connect link: it stops resolving at once.

    Succeeds with nothing to remove too. The ties it made and the accounts it admitted stay.
    Needs a signed-in session; an API key is refused with a 403.
    """
    if await remove_connect_link(db, user_id=current_user["id"]):
        await record_auth_event(
            db,
            event_type=AuthEventType.CONNECT_LINK_REMOVED,
            context=RequestContext.from_request(request),
            user_id=current_user["id"],
            commit=False,
        )
        await db.commit()
    return ConnectLinkRemovedResponse()


@router.get("/connect-link/{token}", response_model=ConnectLinkOwner, responses=_NOT_FOUND)
async def resolve_connect_link(
    token: str, response: Response, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> ConnectLinkOwner | JSONResponse:
    """Whose connect link this is, **without authentication**: the owner's name and picture
    version, which the connect page shows before its sign-in form.

    The token is the credential. An unknown token, a reset or removed one, and an owner who
    has asked for their account to be deleted are one identical 404, in every registration
    mode. Both answers carry `Cache-Control: private, no-store`. Answers for one token and
    lists nothing.
    """
    link = await live_connect_link(db, token=token)
    if link is None:
        return _no_such_link()
    response.headers["Cache-Control"] = _NO_STORE
    return ConnectLinkOwner(name=link.owner_name, avatar_sha256=link.owner_avatar_sha256)


@router.get("/connect-link/{token}/avatar", responses=_NOT_FOUND)
async def read_connect_link_avatar(
    request: Request,
    token: str,
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[str | None, Query(description="Opaque cache-busting version token; ignored by the server")] = None,
) -> Response:
    """The connect link owner's picture, **without authentication**: its rendition, one WebP.

    The token is the credential. An owner with no picture is the same 404 as a dead token.
    Never stored by a browser or a proxy (`Cache-Control: private, no-store`), with an `ETag`;
    `If-None-Match` answers 304.
    """
    link = await live_connect_link(db, token=token)
    return await serve_account_avatar(
        request, db, account_id=None if link is None else link.owner_id, not_found=_NO_SUCH_LINK
    )


@router.get("/connect-link/{token}/owner", response_model=ConnectLinkOwnerForYou, responses=_NOT_FOUND)
async def read_connect_link_owner(
    token: str,
    response: Response,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> ConnectLinkOwnerForYou | JSONResponse:
    """Whose connect link this is, to a signed-in caller: the owner's name, username and
    picture version, whether the link is the caller's own, and the caller's person already
    connected to the owner, if any.

    The same identical 404 as the anonymous read, for the same tokens. Both answers carry
    `Cache-Control: private, no-store`.
    """
    link = await live_connect_link(db, token=token)
    if link is None:
        return _no_such_link()
    response.headers["Cache-Control"] = _NO_STORE
    return ConnectLinkOwnerForYou(
        name=link.owner_name,
        username=link.owner_username,
        avatar_sha256=link.owner_avatar_sha256,
        is_yours=link.owner_id == current_user["id"],
        person_uuid=await person_connected_to_uuid(db, user_id=current_user["id"], connected_user_id=link.owner_id),
    )
