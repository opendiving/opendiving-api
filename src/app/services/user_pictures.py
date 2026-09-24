"""Storage for an account's two pictures: its avatar and its check-in portrait.

The **only** module that writes a `user_picture` row. It sits beside
`services/certification_files.py` and `services/dive_files.py` at the same layer - those
know *what* is stored against which row, `services/blob_store.py` knows *where and how* -
and it follows the same ordering rule for every key a row holds: write the files, then
commit the row; retire the row, then unlink after that commit.

**Each picture keeps its original, a crop, and a rendition.** The original is the upload
with its metadata stripped losslessly (`services/picture_originals.py`), so it decodes to
the diver's pixels and carries no location. The rendition - the one WebP every screen
shows - is always rendered from the two: decoded, oriented, cropped, bounded and
re-encoded, which is what a client cannot be trusted to do and why it happens here, where it
holds for the web app, iOS and anything else. "Adjust your photo" is a new crop over the
same original.

The one exception keeps no original: an avatar uploaded without a crop, and the one seeded
from Google at sign-up. Those render the centred square every avatar upload rendered before
originals were kept, from today's formats under today's cap. A portrait always has a crop.

Pillow parses untrusted bytes, so the decode is fenced on four sides: an explicit `formats`
allowlist so only a few battle-tested parsers are ever reachable, a byte cap on the read
itself, a cap on the pixel count the header *claims*, and a second cap on what will
actually be rasterized once the decoder has been asked to do it cheaply. See `_normalize`
for why those last two are not the same check, and `MAX_ORIGINAL_DECODE_PIXELS` for why a
cap in pixels is not a cap in bytes of memory.
"""

import hashlib
import io
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

import anyio.to_thread
import httpx
from fastapi import UploadFile
from PIL import Image, ImageOps
from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ..core.db.database import release_read_transaction
from ..core.utils.uploads import read_upload_within_limit, safe_filename
from ..models.user_picture import UserPicture
from ..schemas.user_picture import PictureCrop, PictureKind
from . import blob_store
from .picture_originals import DamagedImageError, sniff_original, strip_metadata

logger = logging.getLogger(__name__)

# The same ceiling the card upload uses. Generous for a phone photo, and it bounds what one
# request buffers: `read_upload_within_limit` holds the whole upload in memory and the
# decode below holds the raster on top of it.
MAX_PICTURE_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

# Pixels the file *claims*, read from the header before anything is decoded. This is the
# bomb check: a 10 MB upload can describe far more pixels than it costs bytes, and 50 MP is
# roughly a 7000x7000 photo - comfortably above any camera somebody points at their own
# face.
#
# It is deliberately **not** the memory bound. A cap in pixels says nothing about bytes of
# RAM: a uniform PNG describing 50 MP is a couple of hundred kilobytes on the wire and a
# 150 MB raster, and the alpha paths hold two or three of those at once. That is what the
# two decode caps below are for.
MAX_PICTURE_PIXELS = 50_000_000

# Pixels *after* the reduced decode of an original, which is a different question from the
# one above and the one that governs memory. Both caps end in the same 415 and they are not
# redundant:
#
# - `MAX_PICTURE_PIXELS` judges the declaration, before `draft` has had a chance to make a
#   dishonest one cheap. Take it away and a bomb is quietly downscaled instead of refused.
# - This one judges what will actually be rasterized. `draft` reduces only a JPEG - a 12 MP
#   phone photo arrives here as 1008x756 and a 48 MP one at about the same - so what it bounds in
#   practice is a PNG, which is decoded whole.
#
# Written as the product it is named for, 4032x3024 (a 12 MP phone photo, 12,192,768
# pixels), so that image passes whatever its orientation; a phone screenshot is a quarter of
# it. One cap for every mode, and originals are JPEG or PNG only: a WebP or GIF of the same
# size costs up to three times as much to decode.
#
# **Measure this per format, in a fresh process, rather than reasoning about it.** Each figure
# is an `ru_maxrss` delta over a process that has already read the upload, taken inside the
# API's own container (Linux, Pillow 12.3): that counter is a monotonic high-water mark, and
# macOS reads it higher. At the cap, through `_prepare_original` with the strip included and
# either frame: ~110 MB for a 5 MB RGBA PNG, the worst a 5 MB file can be - `resize`
# premultiplies a full-size copy on the alpha paths, see `_normalize` - ~60 MB for an RGB one,
# and ~25 MB for a 10 MB camera JPEG, which `draft` decodes at a quarter. `_DECODE_LIMITER`
# makes that one decode per worker, so the shipped image's four workers reach ~440 MB above
# their own resident sets only when four worst-case PNGs arrive at once, against a documented
# install minimum of 1 GB for the whole stack
# (https://github.com/opendiving/opendiving/blob/main/docs/install.md).
MAX_ORIGINAL_DECODE_PIXELS = 4032 * 3024

# The same question for the avatar that keeps no original - an upload without a crop, from
# a client that has already cropped the square itself, and the Google seed. Those keep the
# four formats they always accepted, WebP and GIF among them, and so the lower cap those two
# need: one RGBA WebP decode at 1536x1536 is ~50 MB, measured as above, because
# `WebPImageFile.load` materializes the whole frame as `bytes` and copies it again into a
# `BytesIO` before the raster is built. A JPEG still passes whatever its megapixels, `draft`
# reducing it first; what this refuses is a large PNG, WebP or GIF offered as an
# already-cropped square.
#
# One quirk it inherits from `draft`, worth knowing before someone reports it as a bug: JPEG
# only halves while *both* edges stay at or above the avatar dimension, so a JPEG whose short
# edge is under 1024 px is not reduced at all and is refused here like a PNG of the same
# size. Only a panorama wider than about 2.25:1 lands in that band, which is why the
# rejection says nothing about formats.
MAX_AVATAR_DECODE_PIXELS = 1536 * 1536

# One picture decoded at a time per worker process, which is what turns the per-decode
# figures above into a ceiling rather than a multiplier. Without it the ceiling is the app's
# own threadpool - `core/setup.set_threadpool_tokens` raises it to 100 per worker, and the
# shipped image runs four - and a hundred concurrent decodes is not a number a 1 GB install
# survives whatever the per-decode figure is.
#
# One rather than a handful because it costs nothing to be strict here: an upload is tens
# to hundreds of milliseconds through this, uploads are rare, and the input that would make
# the queue matter is precisely the hostile one. Passed to `run_sync` as its own limiter
# rather than shrinking the global one, which every other blocking hop in the app shares.
_DECODE_LIMITER = anyio.CapacityLimiter(1)

# The parsers each path may reach. Pillow ships dozens, several for formats whose decoders
# have a CVE history; `formats=` is what keeps them unreachable from an anonymous byte
# string.
ALLOWED_FORMATS = ["JPEG", "PNG", "WEBP", "GIF"]
ORIGINAL_FORMATS = ["JPEG", "PNG"]

# The avatar's square, and the size every decode asks `draft` for: 36-80 px on screen,
# doubled on a retina display.
AVATAR_DIMENSION = 512
RENDITION_CONTENT_TYPE = "image/webp"

# 85 puts a smooth face around 10 KB and a max-entropy worst case under 50. `method=6` is
# libwebp's slowest, most thorough search; it costs tens of milliseconds, once per write, on
# a thread - and every byte it saves is paid back on every render.
_WEBP_QUALITY = 85
_WEBP_METHOD = 6

# Guards on the one URL this server will fetch an image from (see `import_google_avatar`).
_GOOGLE_AVATAR_HOST = "googleusercontent.com"
_GOOGLE_AVATAR_TIMEOUT_SECONDS = 5.0

_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", RENDITION_CONTENT_TYPE: ".webp"}


@dataclass(frozen=True, slots=True)
class Frame:
    """What a picture is rendered into.

    `ratio` is width to height, and the crop has to match it. `max_height` bounds the
    rendition, which is never upscaled. `opaque` fills transparency with white, after the
    resize where it costs nothing: a portrait is looked at on a desk and printed, where a
    transparent corner would show whatever sits behind it, while an avatar is drawn on
    surfaces of several colours and keeps its alpha.
    """

    kind: PictureKind
    key_kind: str
    ratio: tuple[int, int]
    max_height: int
    opaque: bool


AVATAR_FRAME = Frame(PictureKind.AVATAR, "user-avatars", (1, 1), AVATAR_DIMENSION, opaque=False)
# 35x45 mm, the passport photo of the UK, Germany and ICAO Doc 9303, in proportion.
PORTRAIT_FRAME = Frame(PictureKind.PORTRAIT, "user-portraits", (7, 9), 900, opaque=True)


class UnsupportedPictureError(Exception):
    """The uploaded bytes are not an image this can keep or render. The routes' 415."""


class InvalidCropError(Exception):
    """The crop does not fit the image, or is off the picture's ratio. The routes' 422."""


class PictureChangedError(Exception):
    """The picture was replaced or removed while this request re-rendered it. A 409."""


@dataclass(frozen=True, slots=True)
class StoredAvatar:
    """A rendition the Google seed has put in the store, for the new account's row."""

    storage_key: str
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredPictureFile:
    """One file of a picture - its rendition or its original - and how to serve it. No
    bytes: the conditional-request path answers a 304 without reading any."""

    storage_key: str
    sha256: str
    content_type: str
    filename: str


@dataclass(frozen=True, slots=True)
class _Prepared:
    rendition: bytes
    original: bytes | None = None
    original_content_type: str | None = None


def picture_filename(kind: PictureKind, content_type: str) -> str:
    """`avatar.webp`, `portrait.jpg`: the name a picture's file goes by as a rendition's
    download and as an archive member. The kind and the stored type, never the diver's
    filename, which is metadata and never a path."""
    return f"{kind.value}{_EXTENSIONS[content_type]}"


def _upright_size(image: Image.Image, width: int, height: int) -> tuple[int, int]:
    """The declared size once the EXIF orientation is applied - the space a crop is in."""
    orientation = image.getexif().get(0x0112, 1)
    return (height, width) if orientation in (5, 6, 7, 8) else (width, height)


def _check_crop(crop: PictureCrop, size: tuple[int, int], frame: Frame) -> None:
    width, height = size
    if crop.x + crop.width > width or crop.y + crop.height > height:
        raise InvalidCropError(f"The crop reaches outside the {width}x{height} image.")
    ratio_width, ratio_height = frame.ratio
    # Within a pixel on either edge, which is what a client rounding a fractional rectangle
    # can produce.
    if abs(crop.width * ratio_height - crop.height * ratio_width) > max(frame.ratio):
        raise InvalidCropError(f"The crop has to be {ratio_width}:{ratio_height}.")


def _normalize(data: bytes, frame: Frame = AVATAR_FRAME, crop: PictureCrop | None = None) -> bytes:
    """Decode, orient, crop, bound and re-encode as WebP. Runs in a worker thread.

    With a crop, `data` is a stripped original and the crop frames it. Without one it is an
    avatar that keeps no original, rendered as the centred square it always was.

    Every rejection of the image raises `UnsupportedPictureError`, which the routes map to
    one 415; a crop that does not fit raises `InvalidCropError`, their 422. Three of the
    415s are worth naming, because two look like one check and are two, and the third
    looks like a duplicate of the first and is not:

    - **Above `Image.MAX_IMAGE_PIXELS * 2`** (178,956,970 by default) Pillow raises
      `DecompressionBombError` from inside `Image.open` itself, before any line of this
      function's own runs. That is why the open sits inside this `try` rather than above
      it: left out, the largest inputs of all would be the ones that 500.
    - **Above `MAX_PICTURE_PIXELS`** the app rejects what the header *claims*, from
      `Image.size`, which costs no pixel decode.
    - **Above the decode cap, after `draft`**, it rejects what will actually be rasterized.
      The two caps answer different questions and neither substitutes for the other; the
      constants say which is which.

    Between the second and the third, `draft` asks the decoder for the smallest raster
    still no smaller than the avatar. Only JPEG can honour it - it decodes at a fraction of
    the DCT scale, which is what `Image.thumbnail` uses it for - and that is exactly the
    format cameras produce. It halves only while the result stays at or above the requested
    size, so the no-upscaling rule survives it; it also bounds what a rendition can reach,
    since a crop is drawn from the reduced raster.

    `exif_transpose` next, so a portrait-orientation phone photo comes out upright - the
    orientation is the one thing in the metadata the rendition applies. The WebP encoder
    writes only what it is handed in `save()`, and nothing here hands it the source's EXIF,
    ICC profile or XMP. An animated input contributes its first frame.

    The rest is written to hold as few full-resolution rasters as it can: `exif_transpose`
    runs `in_place` because it copies otherwise, the mode conversion is guarded because
    `convert` to the mode an image already has copies too, and the crop and the resize are
    one `resize(..., box=...)`. **It cannot get to one on the alpha paths**, and that is
    Pillow rather than this code: `resize` premultiplies an `LA` or `RGBA` source into a
    full-size `La`/`RGBa` copy before the crop box shrinks anything, which is intrinsic to
    resampling alpha correctly. Hence the decode caps, which are what make the ceiling hold
    whatever the mode.
    """
    if crop is None and frame is not AVATAR_FRAME:
        raise TypeError("only an avatar is rendered without a crop")
    formats, decode_cap = (
        (ALLOWED_FORMATS, MAX_AVATAR_DECODE_PIXELS) if crop is None else (ORIGINAL_FORMATS, MAX_ORIGINAL_DECODE_PIXELS)
    )
    try:
        with Image.open(io.BytesIO(data), formats=formats) as image:
            width, height = image.size
            if width * height > MAX_PICTURE_PIXELS:
                raise UnsupportedPictureError(
                    f"That image is too large to process ({width}x{height}). Please use a smaller photo."
                )

            # After the claimed-size cap, never before: that cap has to judge the
            # declaration, which is what makes it a bomb check rather than a resizing hint.
            image.draft(None, (AVATAR_DIMENSION, AVATAR_DIMENSION))

            # And before the first line here that decodes.
            if image.width * image.height > decode_cap:
                raise UnsupportedPictureError(
                    f"That image is too large to process ({width}x{height}). Please use a smaller photo."
                )

            if crop is not None:
                upright = _upright_size(image, width, height)
                _check_crop(crop, upright, frame)

            ImageOps.exif_transpose(image, in_place=True)

            has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
            target_mode = "RGBA" if has_alpha else "RGB"
            oriented = image if image.mode == target_mode else image.convert(target_mode)

            if crop is None:
                # Never larger than the source's shorter edge, so nothing is upscaled -
                # `draft` cannot have taken it below the avatar dimension, since it only
                # reduces while the result stays at least that large.
                edge = min(oriented.width, oriented.height, AVATAR_DIMENSION)
                rendered = ImageOps.fit(oriented, (edge, edge), method=Image.Resampling.LANCZOS)
            else:
                # The crop is in the upright original's pixels and the raster may have been
                # reduced, so it is scaled onto what was actually decoded.
                scale_x = oriented.width / upright[0]
                scale_y = oriented.height / upright[1]
                box = (
                    crop.x * scale_x,
                    crop.y * scale_y,
                    (crop.x + crop.width) * scale_x,
                    (crop.y + crop.height) * scale_y,
                )
                ratio_width, ratio_height = frame.ratio
                out_height = max(1, min(frame.max_height, math.floor(crop.height * scale_y)))
                out_width = max(1, round(out_height * ratio_width / ratio_height))
                rendered = oriented.resize((out_width, out_height), Image.Resampling.LANCZOS, box=box)

            if frame.opaque and rendered.mode == "RGBA":
                backdrop = Image.new("RGB", rendered.size, "white")
                backdrop.paste(rendered, mask=rendered.getchannel("A"))
                rendered = backdrop

            out = io.BytesIO()
            rendered.save(out, format="WEBP", quality=_WEBP_QUALITY, method=_WEBP_METHOD)
            return out.getvalue()
    except Image.DecompressionBombError as exc:
        raise UnsupportedPictureError("That image describes far too many pixels to process.") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        accepted = "a JPEG, PNG, WEBP or GIF" if crop is None else "a JPEG or PNG"
        raise UnsupportedPictureError(f"Unsupported image. Upload {accepted}.") from exc


def _prepare_original(data: bytes, frame: Frame, crop: PictureCrop) -> _Prepared:
    """Strip an original and render it through `crop`. Runs in a worker thread."""
    content_type = sniff_original(data)
    if content_type is None:
        raise UnsupportedPictureError("Unsupported image. Upload a JPEG or PNG.")
    try:
        original = strip_metadata(data, content_type)
    except DamagedImageError as exc:
        raise UnsupportedPictureError(f"That image is damaged: {exc}.") from exc
    return _Prepared(rendition=_normalize(original, frame, crop), original=original, original_content_type=content_type)


async def process_avatar(data: bytes) -> bytes:
    """The rendition of an avatar that keeps no original: `_normalize`, off the event loop
    and behind `_DECODE_LIMITER`.

    A decode plus a WebP encode is tens to hundreds of milliseconds of pure CPU, and the
    same thread hop every other blocking hop in this app takes. Unlike those, it is also the
    one hop whose *memory* is set by what the caller uploaded rather than by what the app
    allocates - hence its own limiter. Every decode below takes the same hop.
    """
    if not data:
        raise UnsupportedPictureError("The uploaded file is empty.")
    return await anyio.to_thread.run_sync(_normalize, data, limiter=_DECODE_LIMITER)


async def process_original(data: bytes, frame: Frame, crop: PictureCrop) -> _Prepared:
    """The stripped original and its rendition through `crop`."""
    if not data:
        raise UnsupportedPictureError("The uploaded file is empty.")
    return await anyio.to_thread.run_sync(_prepare_original, data, frame, crop, limiter=_DECODE_LIMITER)


async def store_picture(
    db: AsyncSession, *, user_id: int, frame: Frame, upload: UploadFile, crop: PictureCrop | None
) -> str:
    """Store (or replace) one picture from an upload. Returns the rendition's hex digest.

    Raises `HTTPException(413)` via `read_upload_within_limit` if the upload is oversized,
    `UnsupportedPictureError` if the bytes are not an image it accepts, and
    `InvalidCropError` if the crop does not frame it. `crop` is `None` only for an avatar,
    which then keeps no original.
    """
    data = await read_upload_within_limit(upload, MAX_PICTURE_UPLOAD_SIZE)
    prepared = (
        _Prepared(rendition=await process_avatar(data)) if crop is None else await process_original(data, frame, crop)
    )
    return await _write(
        db,
        user_id=user_id,
        frame=frame,
        prepared=prepared,
        crop=crop,
        filename=safe_filename(upload.filename, default=frame.kind.value),
    )


async def copy_avatar_to_portrait(db: AsyncSession, *, user_id: int, crop: PictureCrop) -> str | None:
    """Make the avatar's original the portrait's too, framed by `crop`. Returns the new
    rendition's digest, or `None` while the avatar holds no original.

    A copy under keys of its own, never a shared key: each picture's replace and remove
    unlink their keys after the commit, so a shared one would let either destroy the
    other's bytes. The store has no copy operation, so the copy is a read and a put. The
    filename and the type come with it; the digest does too, the stripped original being
    stripped already.
    """
    source = (
        await db.execute(
            select(UserPicture.original_storage_key, UserPicture.original_filename).where(
                UserPicture.user_id == user_id, UserPicture.kind == PictureKind.AVATAR.value
            )
        )
    ).one_or_none()
    if source is None or source.original_storage_key is None or source.original_filename is None:
        return None

    await release_read_transaction(db)
    data = await blob_store.get(source.original_storage_key)
    prepared = await process_original(data, PORTRAIT_FRAME, crop)
    return await _write(
        db, user_id=user_id, frame=PORTRAIT_FRAME, prepared=prepared, crop=crop, filename=source.original_filename
    )


async def _write(
    db: AsyncSession, *, user_id: int, frame: Frame, prepared: _Prepared, crop: PictureCrop | None, filename: str
) -> str:
    """Put the files, upsert the row, and retire whatever it held.

    **The files are written before the row commits, and the replaced ones are unlinked
    after it**, as for a card file: a crash between the two strands unreferenced files,
    which the sweeper reclaims, where the reverse order would leave a committed row naming
    bytes that do not exist.

    The keys being replaced are read here, in a **fresh narrow select**, rather than taken
    from the caller's `current_user`, which is a snapshot from the start of the request: a
    second upload arriving while the first is in flight would name keys already retired,
    and unlinking them would destroy the other request's committed blobs. Reading them here
    narrows that to two writes genuinely overlapping this function, which costs an orphan
    and never a live file, since `blob_store.new_key` mints a fresh nonce per write.
    """
    existing = (
        await db.execute(
            select(UserPicture.original_storage_key, UserPicture.rendition_storage_key).where(
                UserPicture.user_id == user_id, UserPicture.kind == frame.kind.value
            )
        )
    ).one_or_none()

    rendition_sha256 = hashlib.sha256(prepared.rendition).hexdigest()
    rendition_key = blob_store.new_key(frame.key_kind, sha256=rendition_sha256)
    original: dict[str, object] = dict.fromkeys(
        ("original_storage_key", "original_sha256", "original_byte_size", "original_content_type", "original_filename")
    )
    if prepared.original is not None:
        original_sha256 = hashlib.sha256(prepared.original).hexdigest()
        original = {
            "original_storage_key": blob_store.new_key(frame.key_kind, sha256=original_sha256),
            "original_sha256": original_sha256,
            "original_byte_size": len(prepared.original),
            "original_content_type": prepared.original_content_type,
            "original_filename": filename,
        }

    # The select above autobegan a transaction that would otherwise sit idle across the
    # threadpool writes. What it returned is plain values, not an ORM entity - the
    # precondition `release_read_transaction` documents.
    await release_read_transaction(db)
    if prepared.original is not None:
        await blob_store.put(cast(str, original["original_storage_key"]), prepared.original)
    await blob_store.put(rendition_key, prepared.rendition)

    now = datetime.now(UTC)
    values = {
        # Minted with every replacement: a replaced picture is a different file.
        "uuid": uuid7(),
        "rendition_storage_key": rendition_key,
        "rendition_sha256": rendition_sha256,
        **original,
        **_crop_columns(crop),
    }
    await db.execute(
        pg_insert(UserPicture)
        .values(user_id=user_id, kind=frame.kind.value, created_at=now, **values)
        .on_conflict_do_update(
            index_elements=[UserPicture.user_id, UserPicture.kind], set_={**values, "updated_at": now}
        )
    )
    if existing is not None:
        blob_store.delete_after_commit(
            db, [key for key in (existing.original_storage_key, existing.rendition_storage_key) if key]
        )
    await db.commit()
    return rendition_sha256


async def recrop_picture(db: AsyncSession, *, user_id: int, frame: Frame, crop: PictureCrop) -> str | None:
    """Re-render a picture from the original it holds, through a new crop. Returns the new
    rendition's digest, or `None` when no original is held. The original is not touched.

    The `UPDATE` repeats both keys it read, so a replacement or a removal that lands while
    this renders matches no row: the rendition just drawn is from an original that is no
    longer the picture's, and it is unlinked rather than committed.
    """
    held = (
        await db.execute(
            select(
                UserPicture.original_storage_key,
                UserPicture.original_content_type,
                UserPicture.rendition_storage_key,
            ).where(UserPicture.user_id == user_id, UserPicture.kind == frame.kind.value)
        )
    ).one_or_none()
    if held is None or held.original_storage_key is None:
        return None

    await release_read_transaction(db)
    original = await blob_store.get(held.original_storage_key)
    rendition = await anyio.to_thread.run_sync(_normalize, original, frame, crop, limiter=_DECODE_LIMITER)
    rendition_sha256 = hashlib.sha256(rendition).hexdigest()
    rendition_key = blob_store.new_key(frame.key_kind, sha256=rendition_sha256)
    await blob_store.put(rendition_key, rendition)

    result = cast(
        CursorResult,
        await db.execute(
            update(UserPicture)
            .where(
                UserPicture.user_id == user_id,
                UserPicture.kind == frame.kind.value,
                UserPicture.original_storage_key == held.original_storage_key,
                UserPicture.rendition_storage_key == held.rendition_storage_key,
            )
            .values(
                rendition_storage_key=rendition_key,
                rendition_sha256=rendition_sha256,
                updated_at=datetime.now(UTC),
                **_crop_columns(crop),
            )
        ),
    )
    if result.rowcount == 0:
        await db.rollback()
        await blob_store.delete(rendition_key)
        raise PictureChangedError("The picture changed while it was being adjusted. Reload it and try again.")

    blob_store.delete_after_commit(db, held.rendition_storage_key)
    await db.commit()
    return rendition_sha256


async def delete_picture(db: AsyncSession, *, user_id: int, frame: Frame) -> bool:
    """Remove a picture and both its files. Returns whether there was one to remove.

    Row first, files after the commit - the mirror of `_write`'s ordering. The `DELETE`
    repeats the key the select just read, which is what makes a remove racing a replacement
    safe: the replacement has already written new keys, this matches no row, and the caller
    gets the same "nothing to remove" answer as an account that never had one.
    """
    held = (
        await db.execute(
            select(UserPicture.original_storage_key, UserPicture.rendition_storage_key).where(
                UserPicture.user_id == user_id, UserPicture.kind == frame.kind.value
            )
        )
    ).one_or_none()
    if held is None:
        return False

    result = cast(
        CursorResult,
        await db.execute(
            delete(UserPicture).where(
                UserPicture.user_id == user_id,
                UserPicture.kind == frame.kind.value,
                UserPicture.rendition_storage_key == held.rendition_storage_key,
            )
        ),
    )
    if result.rowcount == 0:
        return False

    blob_store.delete_after_commit(db, [key for key in (held.original_storage_key, held.rendition_storage_key) if key])
    await db.commit()
    return True


async def get_rendition(db: AsyncSession, *, user_id: int, frame: Frame) -> StoredPictureFile | None:
    """Where a picture's rendition is and which version it is, or `None` without one.

    One narrow indexed read, and it answers both questions the download route asks: the
    digest settles `If-None-Match` (a 304 stops here, having touched no bytes) and the key
    is what the bytes are then read from.
    """
    row = (
        await db.execute(
            select(UserPicture.rendition_storage_key, UserPicture.rendition_sha256).where(
                UserPicture.user_id == user_id, UserPicture.kind == frame.kind.value
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return StoredPictureFile(
        storage_key=row.rendition_storage_key,
        sha256=row.rendition_sha256,
        content_type=RENDITION_CONTENT_TYPE,
        filename=picture_filename(frame.kind, RENDITION_CONTENT_TYPE),
    )


async def get_original(db: AsyncSession, *, user_id: int, frame: Frame) -> StoredPictureFile | None:
    """The same for a picture's original, or `None` when none is held."""
    row = (
        await db.execute(
            select(
                UserPicture.original_storage_key,
                UserPicture.original_sha256,
                UserPicture.original_content_type,
                UserPicture.original_filename,
            ).where(UserPicture.user_id == user_id, UserPicture.kind == frame.kind.value)
        )
    ).one_or_none()
    if row is None or row.original_storage_key is None:
        return None
    return StoredPictureFile(
        storage_key=row.original_storage_key,
        sha256=row.original_sha256,
        content_type=row.original_content_type,
        filename=row.original_filename,
    )


async def read_picture_bytes(stored: StoredPictureFile) -> bytes:
    """The bytes `stored` names. Raises `blob_store.BlobMissingError` if they are gone.

    Deliberately not folded into the lookups above: the conditional-request path must be
    able to answer without reading a file, and a row naming absent bytes is data loss
    rather than a 404 - each caller decides how loudly to fail (the routes 500, the export
    archive skips the member).
    """
    return await blob_store.get(stored.storage_key)


async def seed_google_avatar(db: AsyncSession, *, user_id: int, stored: StoredAvatar) -> None:
    """Give a new account the avatar `import_google_avatar` stored, in the caller's
    transaction. A rendition alone: the picture URL names no file, so there is no original."""
    await db.execute(
        pg_insert(UserPicture).values(
            uuid=uuid7(),
            user_id=user_id,
            kind=PictureKind.AVATAR.value,
            rendition_storage_key=stored.storage_key,
            rendition_sha256=stored.sha256,
            created_at=datetime.now(UTC),
        )
    )


def _crop_columns(crop: PictureCrop | None) -> dict[str, int | None]:
    return {
        "crop_x": crop.x if crop else None,
        "crop_y": crop.y if crop else None,
        "crop_width": crop.width if crop else None,
        "crop_height": crop.height if crop else None,
    }


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
    """Fetch a Google profile picture, normalize it, and put it in the store.

    Returns what `seed_google_avatar` writes for the new account, or `None` if there is
    nothing to import - and **`None` is the answer to every failure**, deliberately. This
    runs inside `POST /auth/complete`, which is the one place an account is created; a
    diver's sign-up must not hinge on a CDN being reachable, and an account with initials
    is a complete account. The request that got here has just proved Google reachable by
    verifying the token, so the common case is not the one being defended against.

    Redirects are not followed, the read is capped at the upload limit, and the bytes go
    through the same `_normalize` as an avatar upload without a crop - Google's is a JPEG or
    PNG served to browsers, not a trusted image. Nothing seeds the portrait: a Google
    profile picture is the decorative kind that picture is not.

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
                    if total > MAX_PICTURE_UPLOAD_SIZE:
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
    key = blob_store.new_key(AVATAR_FRAME.key_kind, sha256=digest)
    try:
        await blob_store.put(key, processed)
    except OSError:
        logger.warning("Could not store the imported Google profile picture", exc_info=True)
        return None

    return StoredAvatar(storage_key=key, sha256=digest)
