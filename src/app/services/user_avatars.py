"""Storage for one account's avatar: normalize on the way in, serve one WebP on the way out.

The **only** module that knows a `User` row has a picture at all. It sits beside
`services/certification_files.py` and `services/dive_files.py` at the same layer - those
know *what* is stored against which row, `services/blob_store.py` knows *where and how* -
and it follows the same ordering rule: write the file, then commit the row; clear the row,
then unlink after that commit.

**The bytes that arrive are never the bytes that are stored.** Every upload is decoded,
oriented, squared, bounded to 512 px and re-encoded as WebP, and that is a privacy feature
before it is a performance one: re-encoding is what strips EXIF, and a phone photo's EXIF
carries GPS. A client cannot be trusted to do it - the web app is one caller of an API that
also serves iOS and anything else somebody writes - so the guarantee lives here, where it
holds for all of them. The original is not kept: an avatar is a derived display artifact,
and fidelity to what was uploaded is worth nothing. That is the opposite of the card files
next door, which are archival documents and are stored byte for byte.

Pillow parses untrusted bytes, so the decode is fenced in three ways: an explicit `formats`
allowlist so only four battle-tested parsers are ever reachable, a pixel cap checked from
the header before any pixel is decoded, and a byte cap on the read itself. See
`_normalize` for the two oversized bands and why both end in the same rejection.
"""

import hashlib
import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

import anyio.to_thread
import httpx
from fastapi import UploadFile
from PIL import Image, ImageOps
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db.database import release_read_transaction
from ..core.utils.uploads import read_upload_within_limit
from ..models.user import User
from . import blob_store

logger = logging.getLogger(__name__)

# The key prefix every avatar is stored under - the third kind on the volume, after
# `dive-files` and `certification-files`. See `blob_store.new_key`.
KEY_KIND = "user-avatars"

# The same ceiling the card upload uses. Generous for a portrait, and it bounds what one
# request buffers: `read_upload_within_limit` holds the whole upload in memory and the
# decode below holds the raster on top of it.
MAX_AVATAR_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

# Pixels, checked against the *header* before anything is decoded. A 10 MB upload can
# describe far more pixels than it costs bytes, which is the whole shape of a
# decompression bomb: 50 MP is roughly a 7000x7000 photo, comfortably above any camera
# somebody points at their own face.
#
# It is **not** low enough to make the raster free, and that is worth stating rather than
# implying. A 50 MP RGB raster is 150 MB, and a uniform PNG that describes one costs a
# couple of hundred kilobytes on the wire - a four-figure amplification that the byte cap
# above cannot see. So the cap is one of three things holding memory down, the other two
# being `_normalize`'s reduced decode and `_DECODE_LIMITER`'s bound on how many of these
# can be in flight at once. Lowering it further would start rejecting real phone photos -
# 48 and 50 MP sensors ship today - which is a worse trade than bounding the concurrency.
MAX_AVATAR_PIXELS = 50_000_000

# One avatar decoded at a time per worker process, which is what turns "150 MB per 50 MP
# raster" into a bound rather than a multiplier. Without it the ceiling is the app's own
# threadpool - `core/setup.set_threadpool_tokens` raises it to 100 per worker, and the
# shipped image runs four - so a hundred concurrent uploads of a cap-passing uniform PNG
# is not a number a single-node install survives. Measured on this pipeline: 206 MB for a
# 7000x7000 PNG, 10 MB for the same picture as a JPEG (see `_normalize` on why they
# differ), against 568 MB for both before the reduced decode landed.
#
# One rather than a handful because it costs nothing to be strict here. A realistic phone
# photo is tens of milliseconds through this, uploads are rare - once per account,
# occasionally again - and the input that would make the queue matter is precisely the
# hostile one. Passed to `run_sync` as its own limiter rather than shrinking the global
# one, which every other blocking hop in the app shares.
_DECODE_LIMITER = anyio.CapacityLimiter(1)

# Only these four parsers are ever invoked. Pillow ships dozens, several for formats whose
# decoders have a CVE history and none of which anyone uploads as an avatar; `formats=`
# is what keeps them unreachable from an anonymous byte string.
ALLOWED_FORMATS = ["JPEG", "PNG", "WEBP", "GIF"]

# One square, one format, for every mount the clients have: 36-80 px on screen, doubled on
# a retina display. Smaller sources are left alone rather than upscaled - blowing up a
# 200 px picture to 512 makes a bigger file and a worse image.
AVATAR_DIMENSION = 512
AVATAR_CONTENT_TYPE = "image/webp"
AVATAR_FILENAME = "avatar.webp"

# 85 puts a smooth portrait around 10 KB and a max-entropy worst case under 50. `method=6`
# is libwebp's slowest, most thorough search; it costs tens of milliseconds on a 512 px
# square, once per upload, on a thread - and every byte it saves is paid back on every
# render for the life of the account.
_WEBP_QUALITY = 85
_WEBP_METHOD = 6

# Guards on the one URL this server will fetch an image from (see `import_google_avatar`).
_GOOGLE_AVATAR_HOST = "googleusercontent.com"
_GOOGLE_AVATAR_TIMEOUT_SECONDS = 5.0


class UnsupportedAvatarImageError(Exception):
    """The uploaded bytes are not an image this can turn into an avatar."""


@dataclass(frozen=True, slots=True)
class StoredAvatar:
    """Where an account's avatar is and which version it is. No bytes - see
    `read_avatar_bytes`, which the 304 path deliberately never reaches."""

    storage_key: str
    sha256: str


def _normalize(data: bytes) -> bytes:
    """Decode, orient, square, bound and re-encode as WebP. Runs in a worker thread.

    Every rejection raises `UnsupportedAvatarImageError`, which the routes map to one 415.
    Two of them are worth naming because they look like one check and are two:

    - **Above `MAX_AVATAR_PIXELS`** the app rejects, from `Image.size` - which the plugin
      fills in from the header, so this costs no pixel decode.
    - **Above `Image.MAX_IMAGE_PIXELS * 2`** (178,956,970 by default) Pillow raises
      `DecompressionBombError` from inside `Image.open` itself, *before* the line above
      ever runs. That is why the open sits inside this `try` rather than above it: left
      out, the largest inputs of all would be the ones that 500.

    `exif_transpose` first, so a portrait phone photo comes out upright - orientation is
    the one thing in the metadata that has to be applied before the rest of it is dropped.
    Dropping is structural rather than an erasure step: the WebP encoder writes only what
    it is handed in `save()`, and nothing here hands it the source's EXIF, ICC profile or
    XMP. An animated input contributes its first frame, which is the one `Image.open`
    leaves selected.

    **Every step after the cap is written to hold one full-resolution raster, not three**,
    because at the cap one of those is 150 MB. `draft` asks the decoder for the smallest
    raster still no smaller than the avatar - JPEG does this natively by decoding at a
    fraction of the DCT scale, which is what `Image.thumbnail` uses it for, and it turns
    the common phone-photo case into a couple of megabytes. `exif_transpose` runs
    `in_place` because it copies otherwise, and the mode conversion is guarded because
    `convert` to the mode an image already has copies too. `ImageOps.fit` crops and
    resizes in a single `resize(..., box=...)`, so the square is never materialised at
    full size either.
    """
    try:
        with Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS) as image:
            width, height = image.size
            if width * height > MAX_AVATAR_PIXELS:
                raise UnsupportedAvatarImageError(
                    f"That image is too large to process ({width}x{height}). Please use a smaller photo."
                )

            # After the cap, never before: the cap has to judge what the file *claims*,
            # which is what makes it a bomb check rather than a resizing hint.
            image.draft(None, (AVATAR_DIMENSION, AVATAR_DIMENSION))
            ImageOps.exif_transpose(image, in_place=True)

            # Alpha survives into the WebP rather than being composited onto an invented
            # background: a logo with a transparent corner should keep it, and the clients
            # draw avatars on surfaces of several different colours.
            has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
            target_mode = "RGBA" if has_alpha else "RGB"
            oriented = image if image.mode == target_mode else image.convert(target_mode)

            # Never larger than the source's shorter edge, so nothing is upscaled - `draft`
            # cannot have taken it below the avatar dimension, since it only reduces while
            # the result stays at least that large.
            edge = min(oriented.width, oriented.height, AVATAR_DIMENSION)
            square = ImageOps.fit(oriented, (edge, edge), method=Image.Resampling.LANCZOS)

            out = io.BytesIO()
            square.save(out, format="WEBP", quality=_WEBP_QUALITY, method=_WEBP_METHOD)
            return out.getvalue()
    except Image.DecompressionBombError as exc:
        raise UnsupportedAvatarImageError("That image describes far too many pixels to be a profile picture.") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise UnsupportedAvatarImageError("Unsupported image. Upload a JPEG, PNG, WEBP or GIF.") from exc


async def process_avatar(data: bytes) -> bytes:
    """`_normalize`, off the event loop and behind `_DECODE_LIMITER`.

    A decode plus a WebP encode is tens to hundreds of milliseconds of pure CPU, and the
    same thread hop every other blocking hop in this app takes (`blob_store.put`, the
    dive-export parse, the Google key fetch). Unlike those, it is also the one hop whose
    *memory* is set by what the caller uploaded rather than by what the app allocates -
    hence its own limiter. See `_DECODE_LIMITER`.
    """
    if not data:
        raise UnsupportedAvatarImageError("The uploaded file is empty.")
    return await anyio.to_thread.run_sync(_normalize, data, limiter=_DECODE_LIMITER)


async def store_user_avatar(db: AsyncSession, *, user_id: int, upload: UploadFile) -> str:
    """Store (or replace) this account's avatar. Returns the stored image's hex digest.

    Raises `HTTPException(413)` via `read_upload_within_limit` if the upload is oversized,
    and `UnsupportedAvatarImageError` if the bytes are not a decodable, accepted image.

    **The file is written before the row commits, and the replaced file is unlinked after
    it.** Same ordering rule, and for the same reason, as `store_certification_file`: a
    crash between the two strands an unreferenced file, which the sweeper reclaims, where
    the reverse order would leave a committed row naming bytes that do not exist.

    The key being replaced is read here, in a **fresh narrow select**, rather than taken
    from the caller's `current_user` dict. That dict is resolved once per request by
    `get_current_user`, so on a second upload arriving while the first is still in flight
    it names a key that has already been retired - and scheduling an unlink for it would
    destroy the *other* request's freshly committed blob. Reading it here narrows that to
    two uploads genuinely overlapping this function, which costs an orphan and never a
    live file, since `blob_store.new_key` mints a fresh nonce per write.
    """
    data = await read_upload_within_limit(upload, MAX_AVATAR_UPLOAD_SIZE)
    processed = await process_avatar(data)
    digest = hashlib.sha256(processed).hexdigest()

    existing_key = (await db.execute(select(User.avatar_storage_key).where(User.id == user_id))).scalar_one_or_none()

    key = blob_store.new_key(KEY_KIND, sha256=digest)
    # The select above autobegan a transaction that would otherwise sit idle across a
    # threadpool write with an `fsync` in it. What it returned is a bare `str | None`, so
    # there is no live ORM entity for the rollback to expire - the precondition
    # `release_read_transaction` documents.
    await release_read_transaction(db)
    await blob_store.put(key, processed)

    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(avatar_storage_key=key, avatar_sha256=digest, updated_at=datetime.now(UTC))
    )
    if existing_key is not None:
        blob_store.delete_after_commit(db, existing_key)
    await db.commit()

    return digest


async def get_stored_avatar(db: AsyncSession, *, user_id: int) -> StoredAvatar | None:
    """Where this account's avatar is and which version it is, or `None` if it has none.

    One narrow indexed read, and it answers both questions the download route asks: the
    digest settles `If-None-Match` (a 304 stops here, having touched no bytes) and the key
    is what the bytes are then read from.
    """
    row = (
        await db.execute(select(User.avatar_storage_key, User.avatar_sha256).where(User.id == user_id))
    ).one_or_none()
    if row is None or row.avatar_storage_key is None or row.avatar_sha256 is None:
        return None
    return StoredAvatar(storage_key=row.avatar_storage_key, sha256=row.avatar_sha256)


async def read_avatar_bytes(stored: StoredAvatar) -> bytes:
    """The bytes `stored` names. Raises `blob_store.BlobMissingError` if they are gone.

    Deliberately not folded into `get_stored_avatar`: the conditional-request path must be
    able to answer without reading a file, and a row naming absent bytes is data loss
    rather than a 404 - each caller decides how loudly to fail (the route 500s, the export
    archive skips the member).
    """
    return await blob_store.get(stored.storage_key)


async def delete_user_avatar(db: AsyncSession, *, user_id: int) -> bool:
    """Clear this account's avatar. Returns whether there was one to clear.

    Row first, file after the commit - the mirror of `store_user_avatar`'s ordering.

    The `UPDATE` repeats the key the select just read rather than naming the user alone,
    which is what makes a delete racing a replacement safe: the replacement has already
    written a new key, this matches no row, and the caller gets the same "nothing to
    remove" answer as an account that never had one. Without the condition this would
    clear columns pointing at bytes it is not the one that wrote, and unlink a live blob.
    """
    key = (await db.execute(select(User.avatar_storage_key).where(User.id == user_id))).scalar_one_or_none()
    if key is None:
        return False

    result = cast(
        CursorResult,
        await db.execute(
            update(User)
            .where(User.id == user_id, User.avatar_storage_key == key)
            .values(avatar_storage_key=None, avatar_sha256=None, updated_at=datetime.now(UTC))
        ),
    )
    if result.rowcount == 0:
        return False

    blob_store.delete_after_commit(db, key)
    await db.commit()
    return True


def _is_google_avatar_url(url: str) -> bool:
    """Whether this is a URL the server is willing to fetch an image from.

    The URL already comes out of a Google ID token this server verified against Google's
    published keys, so it is not attacker-supplied in the ordinary sense. This is the
    second fence anyway, because the first one's failure mode is server-side request
    forgery: a token claim that could name `http://169.254.169.254/…` and be fetched from
    inside the network is the entire class of bug.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == _GOOGLE_AVATAR_HOST or host.endswith(f".{_GOOGLE_AVATAR_HOST}"))


async def import_google_avatar(url: str | None) -> StoredAvatar | None:
    """Fetch a Google profile picture, normalize it, and put it on the volume.

    Returns what to write on the new `User` row, or `None` if there is nothing to import -
    and **`None` is the answer to every failure**, deliberately. This runs inside
    `POST /auth/complete`, which is the one place an account is created; a diver's sign-up
    must not hinge on a CDN being reachable, and an account with initials is a complete
    account. The request that got here has just proved Google reachable by verifying the
    token, so the common case is not the one being defended against.

    Redirects are not followed, the read is capped at the upload limit, and the bytes go
    through the same `_normalize` pipeline as an upload - Google's is a JPEG or PNG served
    to browsers, not a trusted image.

    The blob is written before the caller's commit, per the ordering rule. If that commit
    never happens the file is a nonce-keyed orphan inside the sweeper's grace window,
    which is the harmless direction.
    """
    if not url:
        return None
    if not _is_google_avatar_url(url):
        logger.warning("Refusing to import a profile picture from an unexpected host")
        return None

    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=_GOOGLE_AVATAR_TIMEOUT_SECONDS) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    logger.info(
                        "Google profile picture fetch answered %s; creating the account without one",
                        response.status_code,
                    )
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_AVATAR_UPLOAD_SIZE:
                        logger.info(
                            "Google profile picture exceeded the upload limit; creating the account without one"
                        )
                        return None
                    chunks.append(chunk)

        processed = await process_avatar(b"".join(chunks))
    except Exception:
        logger.warning("Could not import the Google profile picture; creating the account without one", exc_info=True)
        return None

    digest = hashlib.sha256(processed).hexdigest()
    key = blob_store.new_key(KEY_KIND, sha256=digest)
    try:
        await blob_store.put(key, processed)
    except OSError:
        logger.warning("Could not store the imported Google profile picture", exc_info=True)
        return None

    return StoredAvatar(storage_key=key, sha256=digest)
