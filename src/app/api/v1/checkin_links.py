"""The check-in link: three routes for the diver who mints it, and three for whoever holds it.

The anonymous half is authorized by the token in the path, and answers every refusal - an
unknown, expired or revoked token, a diver who has asked for deletion, a card that is not the
diver's, a PDF front, a missing portrait - with one identical 404. Each of its responses sets
`Cache-Control: private, no-store` itself, the 404s included, which is why they are returned
rather than raised: a raised one reaches `ClientCacheMiddleware` unlabelled, and it labels a
credential-free `GET` publicly cacheable. The token is the credential here, and a revoked link
has to stop answering at the next request rather than when a shared cache lets go.

Nothing here is `@cache`d: the link is resolved on every request, in the route.
"""

import uuid as uuid_pkg
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...schemas.checkin_link import (
    CheckinLinkCreate,
    CheckinLinkMinted,
    CheckinLinkRead,
    CheckinLinkRevokedResponse,
    CheckinSummary,
)
from ...services.checkin_links import (
    checkin_summary,
    find_card_front,
    is_servable_front,
    live_checkin_link_expiry,
    load_card_front,
    mint_checkin_link,
    resolve_checkin_link,
    revoke_checkin_links,
)
from ...services.user_pictures import PORTRAIT_FRAME, get_rendition, read_picture_bytes

router = APIRouter(tags=["checkin"])

_NO_STORE = "private, no-store"
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "Unknown, expired or revoked, and every other refusal: one answer"}
}


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Not found"}, headers={"Cache-Control": _NO_STORE})


def _not_modified(sha256: str) -> Response:
    return Response(status_code=304, headers={"ETag": f'"{sha256}"', "Cache-Control": _NO_STORE})


def _image(*, sha256: str, content_type: str, data: bytes) -> Response:
    """An image the page shows in an `<img>`: `inline`, where the owner routes say `attachment`
    because their clients fetch through the API client. The rest is theirs - `nosniff`, a
    sandboxing CSP, and an `ETag` of the digest."""
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            # `frame-ancestors` does not fall back to `default-src`; see the owner routes.
            "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
            "Cache-Control": _NO_STORE,
            "ETag": f'"{sha256}"',
        },
    )


@router.post("/user/checkin-link", response_model=CheckinLinkMinted, status_code=201)
async def write_checkin_link(
    body: CheckinLinkCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CheckinLinkMinted:
    """Share the caller's check-in page as a link that lives for 24 hours.

    Revokes every other link of the caller's first, so one is live at a time. The body is the
    diving figures as the page shows them, which the link shows for its whole life whatever is
    logged afterwards. The token is in this response and nowhere else: only its hash is kept.
    """
    token, expires_at = await mint_checkin_link(db, user_id=current_user["id"], figures=body)
    return CheckinLinkMinted(token=token, expires_at=expires_at)


@router.get("/user/checkin-link", response_model=CheckinLinkRead)
async def read_checkin_link(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CheckinLinkRead:
    """When the caller's live link expires, or 404 without one. Never the token, which was
    shown once and is not kept."""
    expires_at = await live_checkin_link_expiry(db, user_id=current_user["id"])
    if expires_at is None:
        raise NotFoundException("No live check-in link")
    return CheckinLinkRead(expires_at=expires_at)


@router.delete("/user/checkin-link", response_model=CheckinLinkRevokedResponse)
async def erase_checkin_link(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CheckinLinkRevokedResponse:
    """Revoke the caller's link: it stops answering at its next request. Succeeds with nothing
    to revoke too, as a session revoke does."""
    await revoke_checkin_links(db, user_id=current_user["id"])
    await db.commit()
    return CheckinLinkRevokedResponse()


@router.get("/checkin/{token}", response_model=CheckinSummary, responses=_NOT_FOUND)
async def read_checkin_summary(
    token: str, response: Response, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> CheckinSummary | Response:
    """A shared check-in page, **without authentication**: the token is the credential.

    Everything the signed-in check-in page prints, with the diving figures the link was minted
    with, and when the link stops working.
    """
    link = await resolve_checkin_link(db, token=token)
    summary = await checkin_summary(db, link) if link is not None else None
    if summary is None:
        return _not_found()
    response.headers["Cache-Control"] = _NO_STORE
    return summary


@router.get("/checkin/{token}/portrait", responses=_NOT_FOUND)
async def read_checkin_portrait(
    request: Request, token: str, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> Response:
    """The shared page's portrait: the 7:9 rendition `GET /user/portrait` serves, and never the
    avatar or the original. 404 for a diver with no portrait."""
    link = await resolve_checkin_link(db, token=token)
    stored = await get_rendition(db, user_id=link.user_id, frame=PORTRAIT_FRAME) if link is not None else None
    if stored is None:
        return _not_found()
    if request.headers.get("if-none-match") == f'"{stored.sha256}"':
        return _not_modified(stored.sha256)
    return _image(sha256=stored.sha256, content_type=stored.content_type, data=await read_picture_bytes(stored))


@router.get("/checkin/{token}/certification/{uuid}/front", responses=_NOT_FOUND)
async def read_checkin_card_front(
    request: Request,
    token: str,
    # A string, so a malformed uuid is the same 404 rather than a 422 the middleware would
    # label publicly cacheable.
    uuid: Annotated[str, Path(description="The certification's uuid")],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> Response:
    """The front of one of the shared page's cards, when it is an image; the bytes
    `GET /certification/{uuid}/file/front` serves. A PDF front, a card with no front and a
    card that is not the diver's are the same 404, and no route serves a back."""
    try:
        certification_uuid = uuid_pkg.UUID(uuid)
    except ValueError:
        return _not_found()
    link = await resolve_checkin_link(db, token=token)
    front = await find_card_front(db, link, certification_uuid=certification_uuid) if link is not None else None
    if front is None or not is_servable_front(front.content_type):
        return _not_found()
    if request.headers.get("if-none-match") == f'"{front.sha256}"':
        return _not_modified(front.sha256)
    loaded = await load_card_front(db, front)
    if loaded is None:
        return _not_found()
    return _image(sha256=loaded.sha256, content_type=loaded.content_type, data=loaded.data)
