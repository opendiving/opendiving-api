"""Another account's picture, served to somebody who is not its owner: a connected person's,
a row of *Added you*'s, a connect link's owner's.

Never stored by a browser or a proxy (`private, no-store`), the 404 included, which is returned
rather than raised: a raised one reaches `ClientCacheMiddleware` unlabelled, and it labels a
credential-free `GET` publicly cacheable. The owner's own routes answer `private, max-age=300`,
which here would keep serving a cut tie for five minutes. The `ETag` is the rendition's digest,
so a client that keeps the bytes asks again and is answered 304 while the tie stands.
"""

from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.binary_responses import not_modified, stored_bytes_response, uncacheable_not_found
from ..services.user_pictures import AVATAR_FRAME, get_rendition, read_picture_bytes

_NO_STORE = "private, no-store"


async def serve_account_avatar(
    request: Request, db: AsyncSession, *, account_id: int | None, not_found: str = "Not found"
) -> Response:
    """The avatar of `account_id`, or the one 404 - `not_found` - where it is `None` or has no
    picture. The caller decides who may see it, and answers `None` for anybody else."""
    stored = await get_rendition(db, user_id=account_id, frame=AVATAR_FRAME) if account_id is not None else None
    if stored is None:
        return uncacheable_not_found(not_found)
    etag = f'"{stored.sha256}"'
    if request.headers.get("if-none-match") == etag:
        return not_modified(etag, cache_control=_NO_STORE)
    return stored_bytes_response(
        await read_picture_bytes(stored),
        media_type=stored.content_type,
        etag=etag,
        cache_control=_NO_STORE,
        disposition="inline",
    )
