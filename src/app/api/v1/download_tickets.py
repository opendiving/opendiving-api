"""Tickets that let a browser fetch one download by navigating to it.

The download routes want an `Authorization` header, which a navigation cannot send, so the
web app fetches them and saves the bytes from an object URL. Firefox on iOS ignores the name
such a save is given and uses the object URL's uuid instead; a navigation to the route
itself is named by its `Content-Disposition`. A ticket is what makes that navigation
authenticated, and `get_downloading_user` is the only thing that accepts one.
"""

import uuid as uuid_pkg
from typing import Annotated

from fastapi import APIRouter, Depends

from ...api.dependencies import current_session_uuid, get_current_user, refuse_api_keys
from ...core.exceptions.http_exceptions import UnauthorizedException
from ...core.security import DOWNLOAD_TOKEN_EXPIRE_SECONDS, create_download_token
from ...schemas.download_ticket import DownloadTicketCreate, DownloadTicketMinted

router = APIRouter(tags=["downloads"])


@router.post(
    "/user/download-ticket",
    response_model=DownloadTicketMinted,
    status_code=201,
    dependencies=[Depends(refuse_api_keys)],
)
async def write_download_ticket(
    body: DownloadTicketCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    session_uuid: Annotated[uuid_pkg.UUID | None, Depends(current_session_uuid)],
) -> DownloadTicketMinted:
    """Mint a ticket that authenticates one download URL without an `Authorization` header.

    From a signed-in session, never an API key. The ticket is appended to `path` as
    `?ticket=` and accepted by the routes serving a download - an export, a dive's stored
    file, a certification card's file - on that path alone. It lives `expires_in` seconds,
    may be presented more than once within them, and stops working as soon as the session
    that minted it is signed out or revoked. Minting checks nothing about the download
    itself: the request to `path` answers exactly as it would with a bearer token.
    """
    # `get_current_user` has already refused a token naming no session.
    if session_uuid is None:
        raise UnauthorizedException("User not authenticated.")

    ticket = create_download_token(user_uuid=current_user["uuid"], session_uuid=session_uuid, path=body.path)
    return DownloadTicketMinted(ticket=ticket, expires_in=DOWNLOAD_TOKEN_EXPIRE_SECONDS)
