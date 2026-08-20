"""Full export of the caller's own logbook, in three shapes.

Four things are true of all three endpoints, and each is here rather than in the service
layer because each is an HTTP concern:

- **The caller's own data, and nothing else.** No `username` or `user_uuid` parameter: the
  bearer token names the only account there is to export, so there is no authorization
  decision to get wrong and no id to probe with. That is a stronger guarantee than the
  ownership check every other route makes, not a weaker one.
- **Never cached.** No `@cache` decorator and `Cache-Control: no-store` on the way out. An
  export is a whole logbook keyed by nothing but the user, so a Redis entry would be
  megabytes evicting everything the cache exists for - and *"`@cache` and per-request
  authorization don't mix directly"* (DECISIONS.md) makes a cached whole-account payload
  the worst possible thing to get wrong. `no-store` rather than `private` because the file
  includes c-card scans, which have no business sitting in a browser's disk cache.
- **Rate limited.** An archive walks every blob the caller owns; unthrottled it is a
  cheap way to make a shared instance do a lot of I/O. The bounds are generous - this is
  a button a diver presses once, not a polled endpoint.
- **Spooled, not streamed live.** Each response is built into a `SpooledTemporaryFile`
  while the request's database session is still open, then streamed from there. Handing a
  database-reading generator straight to `StreamingResponse` would run it *after* FastAPI
  closed the session. It also gives every response a real `Content-Length`, so a browser
  can draw a progress bar for the archive.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import IO, Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from ...api.dependencies import get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.utils.rate_limit import enforce_rate_limit
from ...services.export import load_export_bundle, spool, spool_text, write_archive, write_dives_csv, write_uddf
from ...services.export.naming import export_filename

router = APIRouter(tags=["export"])


async def _enforce_export_limit(user_id: int) -> None:
    await enforce_rate_limit(
        f"export:user:{user_id}",
        settings.EXPORT_RATE_LIMIT_PER_USER,
        settings.EXPORT_RATE_LIMIT_WINDOW_SECONDS,
    )


def _download(buffer: IO[bytes], *, filename: str, media_type: str) -> StreamingResponse:
    """Stream a spooled temp file as a download, and delete it when the response ends.

    A `SpooledTemporaryFile` deletes itself when closed, so the generator's `finally` is
    the whole lifecycle: whether it runs to the end, the client disconnects mid-download
    or the server unwinds, the spill file goes with it. No `BackgroundTask` is involved -
    Starlette closes the body iterator either way.
    """

    def chunks() -> Iterator[bytes]:
        try:
            while True:
                chunk = buffer.read(64 * 1024)
                if not chunk:
                    return
                yield chunk
        finally:
            buffer.close()

    size = buffer.seek(0, 2)
    buffer.seek(0)
    return StreamingResponse(
        chunks(),
        media_type=media_type,
        headers={
            # ASCII-only by construction (`export_filename` scrubs the username), so the
            # RFC 6266 two-parameter form `core/utils/uploads.py` needs for stored
            # filenames buys nothing here.
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
            # The same pair `read_dive_file` serves stored uploads with. These bodies are
            # `attachment` and diver-supplied besides (the archive carries their own dive
            # files; the UDDF is XML), so the browser must not be free to re-interpret one
            # as something scriptable at this origin.
            "X-Content-Type-Options": "nosniff",
            # `frame-ancestors` is spelled out because it does not fall back to
            # `default-src`: a response with its own policy opts out of
            # `SecurityHeadersMiddleware`'s default and would otherwise be framable
            # however strict the rest of this is.
            "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
        },
    )


@router.get("/export/uddf")
async def export_uddf(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> StreamingResponse:
    """Download the caller's whole logbook as a single UDDF 3.2.2 document.

    UDDF is the open interchange format Subsurface, divelogs.de and MacDive import, so
    this is the file to hand another program. It carries the dives, their sites, trips,
    gases, cylinders, gear and full sample profiles - but not the things the format has no
    slot for (gear sets, service history, c-cards, per-cylinder role, the deco ceiling).
    For a copy that holds everything, use `/export/archive`.
    """
    await _enforce_export_limit(current_user["id"])
    bundle = await load_export_bundle(db, user_id=current_user["id"])
    exported_at = datetime.now(UTC)
    # Named before the spool exists, so nothing can raise between creating the temp file
    # and handing it to the response that owns closing it.
    filename = export_filename(current_user["username"], exported_at.date(), "uddf")
    buffer = await spool(write_uddf(db, bundle, exported_at=exported_at))
    return _download(buffer, filename=filename, media_type="application/xml")


@router.get("/export/csv")
async def export_csv(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> StreamingResponse:
    """Download the caller's dives as a single spreadsheet-ready CSV.

    One row per dive with the related records flattened into readable cells - the file to
    open in Excel, Numbers or a notebook. The normalized set (cylinders, trips, sites,
    gear, service history, certifications) ships inside `/export/archive`.
    """
    await _enforce_export_limit(current_user["id"])
    bundle = await load_export_bundle(db, user_id=current_user["id"])
    exported_at = datetime.now(UTC)
    filename = export_filename(current_user["username"], exported_at.date(), "csv")
    # Off the event loop, unlike the other two: `write_dives_csv` is a plain generator
    # with no await anywhere, so draining it is one uninterrupted stretch of CPU inside an
    # `async def`. The UDDF and JSON writers avoid that incidentally, by awaiting
    # `load_profile` once per dive. Measured at 14 us per dive against the dev corpus - so
    # 7 ms for 500 dives and ~0.14 s for ten thousand, which is small but is a whole
    # worker stalling rather than one request being slow.
    #
    # **The constraint this buys:** the generator reads attributes off ORM instances that
    # are still attached to the request's `AsyncSession`, from a thread that is not the
    # event loop. Safe because every attribute `_dive_row` touches is an eagerly-loaded
    # column - add a `deferred` one or a relationship and it becomes lazy IO off-loop,
    # which fails with `MissingGreenlet` rather than blocking.
    buffer = await run_in_threadpool(spool_text, write_dives_csv(bundle))
    # `charset=utf-8` alongside the byte-order mark `tabular.py` writes: between them
    # every consumer that has an opinion about a CSV's encoding gets told the truth.
    return _download(buffer, filename=filename, media_type="text/csv; charset=utf-8")


@router.get("/export/archive")
async def export_archive(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> StreamingResponse:
    """Download everything: a zip holding the structured export, the UDDF document, the
    full CSV set, every stored dive-computer file and both sides of every c-card.

    This is the complete copy - nothing in the account is reachable only through the app
    after taking it. **It includes the certification card images**, which are personal
    documents, so treat the file accordingly.
    """
    await _enforce_export_limit(current_user["id"])
    bundle = await load_export_bundle(db, user_id=current_user["id"])
    exported_at = datetime.now(UTC)
    filename = export_filename(current_user["username"], exported_at.date(), "zip")
    buffer = await write_archive(db, bundle, exported_at=exported_at)
    return _download(buffer, filename=filename, media_type="application/zip")
