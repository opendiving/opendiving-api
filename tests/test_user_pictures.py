"""The two pictures: `services/user_pictures.py`, `services/picture_originals.py`, and the
routes under `/user/avatar` and `/user/portrait`.

**The pipeline is a security boundary**, not a resizing convenience: Pillow is handed bytes
an anonymous caller chose, and what is kept has to be free of the location a phone photo
carries whatever went in. So the branches tested first are the rejections and the strippings:
the lossless strip of an original, which keeps the pixels and the orientation and drops every
other byte of metadata, and the rendition, which is cropped and bounded from it.

Three of the rejections are about size, and they look like one check. An image between the
app's claimed-size cap and Pillow's own limit is rejected by `_normalize`'s explicit test; an
image past 178,956,970 px never reaches that line, because `Image.open` raises
`DecompressionBombError` first; and an image that passes both can still be refused for what it
would *rasterize* to, which is the only one that governs memory.

The store/delete group is about ordering (files before row, unlink after commit) and about
*where the retired keys come from*: reading them from the request-start `current_user`
snapshot rather than from the database would let one upload unlink another's committed blobs.
"""

import base64
import hashlib
import importlib.util
import io
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import anyio
import httpx
import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.db.migrations import MIGRATIONS_PATH
from src.app.core.setup import create_application
from src.app.crud.crud_users import read_account
from src.app.models.user import USER_AVATAR_SHA256, USER_AVATAR_STORAGE_KEY, User, user_table
from src.app.models.user_picture import UserPicture
from src.app.schemas.user import UserRead
from src.app.schemas.user_picture import PictureCrop
from src.app.services import blob_store, user_pictures
from src.app.services.picture_originals import strip_metadata
from src.app.services.user_pictures import (
    AVATAR_DIMENSION,
    AVATAR_FRAME,
    MAX_AVATAR_DECODE_PIXELS,
    MAX_ORIGINAL_DECODE_PIXELS,
    MAX_PICTURE_PIXELS,
    MAX_PICTURE_UPLOAD_SIZE,
    PORTRAIT_FRAME,
    InvalidCropError,
    PictureChangedError,
    StoredAvatar,
    StoredPictureFile,
    UnsupportedPictureError,
    _is_google_avatar_url,
    _normalize,
    _prepare_import,
    _prepare_original,
    copy_avatar_to_portrait,
    delete_picture,
    get_original,
    get_rendition,
    import_google_avatar,
    preview_data_url,
    recrop_picture,
    seed_google_avatar,
    store_picture,
)
from tests.conftest import db_available
from tests.helpers.generators import create_user, set_avatar_columns
from tests.helpers.images import (
    C2PA_PAYLOAD,
    COMMENT_PAYLOAD,
    MARKED_JPEG_SIZE,
    MOTION_PHOTO_VIDEO,
    XMP_PAYLOAD,
    animated_gif,
    bmp,
    gif,
    jpeg_segment,
    jpeg_with_exif,
    large_jpeg,
    phone_jpeg,
    plain_jpeg,
    plain_png,
    png_declaring,
    png_with_alpha,
    screenshot_png,
    solid_png,
    webp_with_alpha,
    with_jpeg_segments,
)

USER_UUID = uuid7()
CURRENT_USER = {"id": 1, "uuid": USER_UUID, "username": "ada", "is_superuser": False}

# Copied verbatim from `read_certification_file`'s response. `frame-ancestors` is the
# load-bearing clause: a response that sets its own policy opts out of
# `SecurityHeadersMiddleware`'s default, so a shorter CSP here would silently make a picture
# framable however strict the rest of the app is.
EXPECTED_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"

AVATAR_PATH = "/api/v1/user/avatar"
PORTRAIT_PATH = "/api/v1/user/portrait"

# The largest centred 7:9 crop of an upright 3024x4032 phone photo: full width, 3888 high.
PORTRAIT_CROP_OF_A_TURNED_PHONE_PHOTO = PictureCrop(x=0, y=72, width=3024, height=3888)
# The same of an upright 4032x3024: full height, 2352 wide.
PORTRAIT_CROP_OF_A_LANDSCAPE_PHOTO = PictureCrop(x=840, y=0, width=2352, height=3024)


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


def _upload(content: bytes, filename: str = "me.jpg") -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def _square(size: tuple[int, int]) -> PictureCrop:
    edge = min(size)
    return PictureCrop(x=(size[0] - edge) // 2, y=(size[1] - edge) // 2, width=edge, height=edge)


def _pixels(data: bytes) -> bytes:
    with _open(data) as image:
        return image.tobytes()


class TestNormalization:
    def test_the_output_is_always_one_webp(self) -> None:
        result = _open(_normalize(jpeg_with_exif()))

        assert result.format == "WEBP"

    def test_orientation_is_applied_and_the_metadata_it_came_from_is_gone(self) -> None:
        """The two halves of the same fixture, and they fail in opposite directions.

        A pipeline that never read the EXIF leaves the picture on its side; one that
        re-encoded without stripping leaves the GPS block that phone photos carry sitting
        in an avatar the account will hand out for years.
        """
        stored = _normalize(jpeg_with_exif(orientation=6))
        result = _open(stored).convert("RGB")

        edge = MARKED_JPEG_SIZE
        # Orientation 6 rotates the source 90 degrees, which carries the green top-left
        # quadrant round to the top right.
        top_left = result.getpixel((edge // 4, edge // 4))
        top_right = result.getpixel((edge * 3 // 4, edge // 4))
        assert isinstance(top_left, tuple) and isinstance(top_right, tuple)
        assert top_right[1] > top_right[0], "the orientation tag was not applied"
        assert top_left[0] > top_left[1], "the green quadrant is still where it started"

        assert dict(_open(stored).getexif()) == {}
        assert b"Exif" not in stored
        assert "exif" not in _open(stored).info

    def test_an_untagged_image_of_the_same_scene_comes_out_the_other_way_round(self) -> None:
        """The control for the test above: without the tag nothing rotates, so the two
        outputs must disagree. Otherwise a `_normalize` that ignored orientation entirely
        would satisfy half the assertions above by luck of which quadrant was sampled."""
        result = _open(_normalize(jpeg_with_exif(orientation=1))).convert("RGB")

        top_left = result.getpixel((MARKED_JPEG_SIZE // 4, MARKED_JPEG_SIZE // 4))
        assert isinstance(top_left, tuple)
        assert top_left[1] > top_left[0]

    def test_transparency_survives_the_re_encode(self) -> None:
        """WebP carries alpha, so there is no reason to composite it onto an invented
        background - and a diver whose picture has a transparent corner would see whatever
        colour we picked, on every surface the clients draw avatars on."""
        result = _open(_normalize(png_with_alpha()))

        assert result.mode == "RGBA"
        assert result.convert("RGBA").getpixel((8, 8)) == (0, 0, 0, 0)

    def test_an_animated_gif_contributes_its_first_frame(self) -> None:
        result = _open(_normalize(animated_gif())).convert("RGB")

        assert getattr(result, "n_frames", 1) == 1
        pixel = result.getpixel((16, 16))
        assert isinstance(pixel, tuple)
        assert pixel[0] > pixel[2], "the second (blue) frame won"

    def test_a_large_image_is_bounded_to_the_avatar_dimension(self) -> None:
        result = _open(_normalize(plain_png(size=(900, 700))))

        assert result.size == (AVATAR_DIMENSION, AVATAR_DIMENSION)

    def test_a_small_image_is_squared_but_never_upscaled(self) -> None:
        result = _open(_normalize(plain_png(size=(120, 80))))

        assert result.size == (80, 80)

    def test_a_large_jpeg_is_never_decoded_at_full_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The memory half, and it does not show up in any output the other tests read.

        The pixel cap bounds what is *accepted*, not what is allocated: a cap-passing
        50 MP source is a 150 MB raster, and the route has no rate limit in front of it.
        `draft` is what makes the common case cheap - JPEG decodes at a fraction of the DCT
        scale, so nothing near full resolution is ever materialised. Asserted on the opened
        image's own size, which `draft` rewrites in place before a pixel is read.
        """
        opened: list[Image.Image] = []
        real_open = Image.open

        def spy(fp: Any, **kwargs: Any) -> Image.Image:
            image = real_open(fp, **kwargs)
            opened.append(image)
            return image

        monkeypatch.setattr(user_pictures.Image, "open", spy)

        result = _open(_normalize(large_jpeg(size=(4000, 4000))))

        # A quarter of each edge: `draft` halves while the result stays at or above the
        # requested size, and an eighth (500) would fall under the 512 px avatar. So a
        # 48 MB raster becomes 3 MB, and the assertion is on the size the decoder was left
        # holding rather than on anything the output could tell you.
        assert opened[0].size == (1000, 1000), "the full-resolution raster was decoded anyway"
        assert result.size == (AVATAR_DIMENSION, AVATAR_DIMENSION)

    def test_a_jpeg_already_near_the_avatar_size_is_not_reduced_below_it(self) -> None:
        """The other side of `draft`: it only reduces while the result stays at least the
        avatar dimension, so the no-upscaling rule survives it."""
        result = _open(_normalize(large_jpeg(size=(700, 700))))

        assert result.size == (AVATAR_DIMENSION, AVATAR_DIMENSION)

    @pytest.mark.asyncio
    async def test_decodes_run_one_at_a_time_per_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bound that makes the raster size a ceiling rather than a multiplier.

        Asserted on the wiring rather than on memory: a resident-set measurement is both
        noisy and monotonic, while "this hop carries its own limiter, and it is not the
        global one every other blocking call shares" is exactly the property that would be
        lost if someone simplified the `run_sync` call.
        """
        captured: dict[str, Any] = {}
        real_run_sync = user_pictures.anyio.to_thread.run_sync

        async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return await real_run_sync(func, *args, **kwargs)

        monkeypatch.setattr(user_pictures.anyio.to_thread, "run_sync", spy)

        await user_pictures.process_avatar(jpeg_with_exif())

        assert captured["limiter"] is user_pictures._DECODE_LIMITER
        assert user_pictures._DECODE_LIMITER.total_tokens == 1
        assert user_pictures._DECODE_LIMITER is not anyio.to_thread.current_default_thread_limiter()

    def test_an_image_over_the_pixel_cap_is_refused_from_its_header_alone(self) -> None:
        """The app's own band. The fixture declares 56 MP in 74 bytes, which is the point:
        the check reads `Image.size` and never decodes, so a real raster would be 168 MB of
        test memory spent proving nothing extra."""
        oversized = png_declaring(8000, 7000)
        assert 8000 * 7000 > MAX_PICTURE_PIXELS

        with pytest.raises(UnsupportedPictureError):
            _normalize(oversized)

    def test_a_decompression_bomb_is_refused_before_the_cap_can_run(self) -> None:
        """Pillow's band, above 178,956,970 px, where `Image.open` raises on its own.

        Uncaught this is a 500 rather than a 415 - and the test above would never notice,
        because any fixture under that threshold exercises only the app's own check.
        """
        with pytest.raises(UnsupportedPictureError):
            _normalize(png_declaring(20000, 10000))

    def test_a_webp_upload_round_trips_with_its_transparency(self) -> None:
        """WebP is an accepted *input* as well as the output format, and it is the one that
        costs the most to decode per byte - `WebPImageFile.load` materializes the frame
        twice before the raster exists. It had no fixture for two review rounds, which is
        how the documented memory ceiling came to be measured from PNGs alone."""
        result = _open(_normalize(webp_with_alpha(size=(600, 600))))

        assert result.format == "WEBP"
        assert result.mode == "RGBA"
        pixel = result.convert("RGBA").getpixel((8, 8))
        assert isinstance(pixel, tuple)
        assert pixel[3] == 0

    def test_a_large_png_is_refused_for_what_it_would_rasterize_to(self) -> None:
        """The second cap, and it is not a duplicate of the first.

        9 MP is well under the claimed-size cap that catches bombs, and still far more than
        this will hold in memory: `draft` cannot reduce a PNG, so the whole raster would be
        materialised - and on the alpha paths two or three copies of it, against a
        documented install minimum of 1 GB for the entire stack.
        """
        assert 3000 * 3000 < MAX_PICTURE_PIXELS
        assert 3000 * 3000 > MAX_AVATAR_DECODE_PIXELS

        with pytest.raises(UnsupportedPictureError) as exc_info:
            _normalize(png_declaring(3000, 3000))

        # No format advice in the message, deliberately: a JPEG can reach this branch too
        # (`draft` does not reduce one whose short edge is under 1024), and "save it as a
        # JPEG" would be unactionable for exactly that caller.
        assert "JPEG" not in str(exc_info.value)

    def test_the_same_dimensions_as_a_jpeg_are_accepted(self) -> None:
        """The pair to the test above, and the reason the decode cap does not read as
        "no photos above 2.4 MP". A camera produces JPEG, `draft` reduces JPEG before the
        cap is asked, and the same 9 MP that is refused as a PNG arrives here as 0.6 MP.
        """
        result = _open(_normalize(large_jpeg(size=(3000, 3000))))

        assert result.size == (AVATAR_DIMENSION, AVATAR_DIMENSION)

    def test_the_bomb_guard_is_the_catch_and_not_the_fixture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The same claim with the threshold moved under an ordinary image, so the `except`
        clause is *executed* rather than inferred from a fixture nobody can shrink."""
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 16)

        with pytest.raises(UnsupportedPictureError):
            _normalize(plain_png(size=(64, 64)))

    def test_a_format_outside_the_allowlist_is_refused(self) -> None:
        """A valid BMP, and refused for being a BMP. `formats=` is what keeps the exotic
        decoders Pillow ships unreachable from an anonymous byte string."""
        with pytest.raises(UnsupportedPictureError):
            _normalize(bmp())

    def test_bytes_that_are_not_an_image_at_all_are_refused(self) -> None:
        with pytest.raises(UnsupportedPictureError):
            _normalize(b"not an image, just some text")

    @pytest.mark.asyncio
    async def test_an_empty_upload_is_refused_rather_than_decoded(self) -> None:
        with pytest.raises(UnsupportedPictureError):
            await user_pictures.process_avatar(b"")


class TestTheOriginalIsStrippedLosslessly:
    """What is kept is the file the diver picked, minus everything but the image."""

    @pytest.mark.parametrize("source", [phone_jpeg(), screenshot_png()])
    def test_it_decodes_to_the_uploads_pixels_and_keeps_only_the_orientation(self, source: bytes) -> None:
        stripped = _prepare_original(source, AVATAR_FRAME, PictureCrop(x=0, y=0, width=30, height=30)).original
        assert stripped is not None

        assert _pixels(stripped) == _pixels(source)
        with _open(stripped) as image:
            image.load()
            assert dict(image.getexif()) == {0x0112: _open(source).getexif()[0x0112]}
            assert not {"xmp", "comment", "Comment", "XML:com.adobe.xmp"} & set(image.info)
        for leaked in (b"GPS", b"PhoneMaker", XMP_PAYLOAD, b"Dahab", C2PA_PAYLOAD, COMMENT_PAYLOAD, b"taken at home"):
            assert leaked not in stripped

    @pytest.mark.parametrize(
        ("source", "content_type"), [(phone_jpeg(), "image/jpeg"), (screenshot_png(), "image/png")]
    )
    def test_stripping_a_stripped_original_changes_no_byte(self, source: bytes, content_type: str) -> None:
        """An archive this app wrote restores with the digest it was stored under."""
        once = strip_metadata(source, content_type)

        assert strip_metadata(once, content_type) == once

    def test_a_motion_photos_video_after_the_image_is_dropped(self) -> None:
        stripped = strip_metadata(phone_jpeg(), "image/jpeg")

        assert stripped.endswith(b"\xff\xd9")
        assert MOTION_PHOTO_VIDEO not in stripped

    def test_c2pa_credentials_and_comments_go(self) -> None:
        source = with_jpeg_segments(
            jpeg_with_exif(), jpeg_segment(0xEB, C2PA_PAYLOAD), jpeg_segment(0xFE, COMMENT_PAYLOAD)
        )

        stripped = strip_metadata(source, "image/jpeg")

        assert C2PA_PAYLOAD not in stripped and COMMENT_PAYLOAD not in stripped
        assert _pixels(stripped) == _pixels(source)

    def test_an_unknown_ancillary_png_chunk_goes(self) -> None:
        stripped = strip_metadata(screenshot_png(), "image/png")

        assert b"prVt" not in stripped
        assert b"trailing bytes" not in stripped

    def test_an_image_without_an_orientation_keeps_no_exif_at_all(self) -> None:
        stripped = strip_metadata(phone_jpeg(orientation=None), "image/jpeg")

        assert b"Exif" not in stripped

    def test_a_jpeg_segment_running_past_the_end_is_a_415_not_a_500(self) -> None:
        source = jpeg_with_exif()
        # A COM segment declaring far more bytes than the file has left.
        truncated = source[:2] + b"\xff\xfe\xff\xf0" + b"short"

        with pytest.raises(UnsupportedPictureError):
            _prepare_original(truncated, AVATAR_FRAME, _square((8, 8)))

    def test_a_png_chunk_running_past_the_end_is_a_415_not_a_500(self) -> None:
        source = plain_png(size=(8, 8))
        truncated = source[:8] + b"\x7f\xff\xff\xffIHDR" + b"short"

        with pytest.raises(UnsupportedPictureError):
            _prepare_original(truncated, AVATAR_FRAME, _square((8, 8)))

    def test_a_jpeg_that_never_ends_is_a_415(self) -> None:
        source = jpeg_with_exif()

        with pytest.raises(UnsupportedPictureError):
            _prepare_original(source[: len(source) // 2], AVATAR_FRAME, _square((MARKED_JPEG_SIZE,) * 2))

    def test_a_jpeg_that_starts_twice_is_a_415(self) -> None:
        source = jpeg_with_exif()

        with pytest.raises(UnsupportedPictureError):
            _prepare_original(source[:2] + source, AVATAR_FRAME, _square((MARKED_JPEG_SIZE,) * 2))

    def test_a_png_that_never_ends_is_a_415(self) -> None:
        source = plain_png(size=(8, 8))

        with pytest.raises(UnsupportedPictureError):
            _prepare_original(source[:-12], AVATAR_FRAME, _square((8, 8)))

    def test_an_exif_block_that_does_not_parse_keeps_nothing(self) -> None:
        """The pixels are what matter: a block Pillow cannot read carries no orientation worth
        keeping, and nothing else of it is kept anyway."""
        source = with_jpeg_segments(plain_jpeg(), jpeg_segment(0xE1, b"Exif\x00\x00not a tiff header"))

        stripped = strip_metadata(source, "image/jpeg")

        assert b"Exif" not in stripped
        assert _pixels(stripped) == _pixels(source)

    @pytest.mark.parametrize("source", [webp_with_alpha(size=(16, 16)), gif(), bmp()])
    def test_an_original_is_a_jpeg_or_a_png_and_nothing_else(self, source: bytes) -> None:
        """A WebP or GIF original costs up to three times a PNG's decode, and the cap was
        measured for these two."""
        with pytest.raises(UnsupportedPictureError) as exc_info:
            _prepare_original(source, AVATAR_FRAME, _square((8, 8)))

        assert "JPEG or PNG" in str(exc_info.value)


class TestRenderingThroughACrop:
    def test_a_12_mp_phone_photo_turned_by_its_exif_renders_upright_at_700x900(self) -> None:
        """Stored landscape with orientation 6: the crop is in the upright 3024x4032 image,
        and `draft` decodes it at 756x1008, whose 7:9 is 756x972 - bounded to 900 high."""
        rendition = _prepare_original(
            phone_jpeg(size=(4032, 3024), orientation=6), PORTRAIT_FRAME, PORTRAIT_CROP_OF_A_TURNED_PHONE_PHOTO
        ).rendition

        with _open(rendition) as image:
            assert image.format == "WEBP"
            assert image.size == (700, 900)
            top, bottom = image.convert("RGB").getpixel((350, 100)), image.convert("RGB").getpixel((350, 800))
        assert isinstance(top, tuple) and isinstance(bottom, tuple)
        assert top[1] > top[0], "the orientation tag was not applied"
        assert bottom[0] > bottom[1]

    def test_the_same_photo_without_the_tag_renders_landscape_derived_at_588x756(self) -> None:
        rendition = _prepare_original(
            phone_jpeg(size=(4032, 3024), orientation=None), PORTRAIT_FRAME, PORTRAIT_CROP_OF_A_LANDSCAPE_PHOTO
        ).rendition

        assert _open(rendition).size == (588, 756)

    def test_a_24_mp_phone_photo_is_accepted(self) -> None:
        crop = PictureCrop(x=1190, y=0, width=3332, height=4284)

        rendition = _prepare_original(phone_jpeg(size=(5712, 4284), orientation=None), PORTRAIT_FRAME, crop).rendition

        assert _open(rendition).format == "WEBP"

    @pytest.mark.parametrize("mode", ["RGB", "RGBA"])
    def test_a_4032x3024_png_is_accepted_whatever_its_mode(self, mode: str) -> None:
        """The cap is written as the product it is named for, so the image passes."""
        assert 4032 * 3024 == MAX_ORIGINAL_DECODE_PIXELS

        rendition = _prepare_original(
            solid_png(size=(4032, 3024), mode=mode), PORTRAIT_FRAME, PORTRAIT_CROP_OF_A_LANDSCAPE_PHOTO
        ).rendition

        assert _open(rendition).size == (700, 900)

    def test_a_png_over_the_cap_is_refused_for_what_it_would_rasterize_to(self) -> None:
        assert 4033 * 3024 > MAX_ORIGINAL_DECODE_PIXELS

        with pytest.raises(UnsupportedPictureError):
            _normalize(png_declaring(4033, 3024), PORTRAIT_FRAME, PictureCrop(x=0, y=0, width=7, height=9))

    def test_a_transparent_portrait_renders_opaque_on_white(self) -> None:
        source = screenshot_png(size=(70, 90), alpha=True, orientation=None)

        with _open(
            _prepare_original(source, PORTRAIT_FRAME, PictureCrop(x=0, y=0, width=70, height=90)).rendition
        ) as image:
            assert image.mode == "RGB"
            assert image.getpixel((60, 80)) == pytest.approx((255, 255, 255), abs=4)

    def test_a_transparent_avatar_keeps_its_alpha(self) -> None:
        source = screenshot_png(size=(64, 64), alpha=True, orientation=None)

        with _open(_prepare_original(source, AVATAR_FRAME, _square((64, 64))).rendition) as image:
            assert image.mode == "RGBA"
            pixel = image.getpixel((56, 56))
            assert isinstance(pixel, tuple) and pixel[3] == 0

    def test_a_small_crop_is_never_upscaled(self) -> None:
        rendition = _prepare_original(
            plain_png(size=(200, 200)), PORTRAIT_FRAME, PictureCrop(x=10, y=10, width=70, height=90)
        ).rendition

        assert _open(rendition).size == (70, 90)

    @pytest.mark.parametrize(
        "crop",
        [
            PictureCrop(x=0, y=0, width=70, height=80),
            PictureCrop(x=0, y=0, width=72, height=90),
            PictureCrop(x=0, y=0, width=90, height=90),
        ],
    )
    def test_a_crop_off_the_ratio_is_refused(self, crop: PictureCrop) -> None:
        with pytest.raises(InvalidCropError):
            _prepare_original(plain_png(size=(200, 200)), PORTRAIT_FRAME, crop)

    @pytest.mark.parametrize(
        "crop", [PictureCrop(x=0, y=0, width=70, height=89), PictureCrop(x=0, y=0, width=69, height=90)]
    )
    def test_a_crop_within_a_pixel_of_the_ratio_is_accepted(self, crop: PictureCrop) -> None:
        _prepare_original(plain_png(size=(200, 200)), PORTRAIT_FRAME, crop)

    def test_a_crop_outside_the_image_is_refused(self) -> None:
        with pytest.raises(InvalidCropError):
            _prepare_original(plain_png(size=(200, 200)), AVATAR_FRAME, PictureCrop(x=150, y=0, width=100, height=100))

    def test_a_crop_is_judged_against_the_upright_image(self) -> None:
        """Orientation 6 turns a 64x48 landscape into a 48x64 portrait, so a crop 60 high
        fits, and one 60 wide would not."""
        _prepare_original(
            phone_jpeg(size=(64, 48), orientation=6), PORTRAIT_FRAME, PictureCrop(x=0, y=0, width=46, height=60)
        )

        with pytest.raises(InvalidCropError):
            _prepare_original(
                phone_jpeg(size=(64, 48), orientation=6), AVATAR_FRAME, PictureCrop(x=0, y=0, width=60, height=60)
            )

    def test_only_an_avatar_renders_without_a_crop(self) -> None:
        with pytest.raises(TypeError):
            _normalize(plain_png(size=(8, 8)), PORTRAIT_FRAME)

    def test_the_crop_less_avatar_keeps_its_formats(self) -> None:
        """What the build before the crop sends: an already-cropped square in any of the
        four formats, rendered as it always was."""
        assert _open(_normalize(webp_with_alpha(size=(64, 64)))).size == (64, 64)
        assert MAX_AVATAR_DECODE_PIXELS < MAX_ORIGINAL_DECODE_PIXELS


class TestAnImportedOriginal:
    """An archive's portrait keeps the crop it carried where that fits, and gets the
    centred one otherwise - the only crop a document from any other producer can have."""

    def test_a_carried_crop_that_fits_frames_it(self) -> None:
        imported = _prepare_import(
            phone_jpeg(size=(4032, 3024), orientation=6), PORTRAIT_FRAME, PORTRAIT_CROP_OF_A_TURNED_PHONE_PHOTO
        )

        assert imported.crop == PORTRAIT_CROP_OF_A_TURNED_PHONE_PHOTO
        assert _open(imported.rendition).size == (700, 900)

    @pytest.mark.parametrize(
        ("orientation", "centred"),
        [(6, PORTRAIT_CROP_OF_A_TURNED_PHONE_PHOTO), (None, PORTRAIT_CROP_OF_A_LANDSCAPE_PHOTO)],
    )
    def test_without_one_the_largest_centred_crop_of_the_upright_image_frames_it(
        self, orientation: int | None, centred: PictureCrop
    ) -> None:
        imported = _prepare_import(phone_jpeg(size=(4032, 3024), orientation=orientation), PORTRAIT_FRAME, None)

        assert imported.crop == centred

    @pytest.mark.parametrize(
        "carried",
        [PictureCrop(x=900, y=0, width=3024, height=3888), PictureCrop(x=0, y=0, width=3024, height=3024)],
        ids=["outside the image", "off the ratio"],
    )
    def test_a_carried_crop_that_does_not_fit_gives_way_to_the_centred_one(self, carried: PictureCrop) -> None:
        imported = _prepare_import(phone_jpeg(size=(4032, 3024), orientation=6), PORTRAIT_FRAME, carried)

        assert imported.crop == PORTRAIT_CROP_OF_A_TURNED_PHONE_PHOTO

    def test_a_sliver_still_gets_a_crop(self) -> None:
        """A picture narrower than the ratio's smallest step rounds to a pixel, not to none."""
        imported = _prepare_import(plain_png(size=(40, 1)), PORTRAIT_FRAME, None)

        assert imported.crop == PictureCrop(x=19, y=0, width=1, height=1)

    def test_the_original_is_stripped_and_its_digest_is_of_what_is_kept(self) -> None:
        source = phone_jpeg(size=(64, 48), orientation=6)

        imported = _prepare_import(source, PORTRAIT_FRAME, None)

        assert imported.original == strip_metadata(source, "image/jpeg")
        assert imported.original_sha256 == hashlib.sha256(imported.original).hexdigest()
        assert imported.original_content_type == "image/jpeg"

    def test_stripping_what_this_app_stored_gives_back_its_digest(self) -> None:
        """What makes this app's own archive restore as the same original."""
        stored = _prepare_import(phone_jpeg(size=(64, 48), orientation=6), PORTRAIT_FRAME, None).original

        assert _prepare_import(stored, PORTRAIT_FRAME, None).original_sha256 == hashlib.sha256(stored).hexdigest()

    @pytest.mark.parametrize("source", [gif(), webp_with_alpha(size=(70, 90)), b"not a picture"])
    def test_anything_but_a_jpeg_or_a_png_is_refused(self, source: bytes) -> None:
        with pytest.raises(UnsupportedPictureError):
            _prepare_import(source, PORTRAIT_FRAME, None)

    @pytest.mark.asyncio
    async def test_the_preview_copy_is_a_small_webp_inline(self) -> None:
        rendition = _prepare_import(
            phone_jpeg(size=(4032, 3024), orientation=6), PORTRAIT_FRAME, PORTRAIT_CROP_OF_A_TURNED_PHONE_PHOTO
        ).rendition

        url = await preview_data_url(rendition)

        prefix = "data:image/webp;base64,"
        assert url.startswith(prefix)
        with _open(base64.b64decode(url.removeprefix(prefix))) as image:
            assert (image.format, image.size) == ("WEBP", (280, 360))


def _session(*, held: SimpleNamespace | None = None, rowcount: int = 1) -> AsyncMock:
    """A mocked session whose narrow lookups answer `held`.

    Mocked deliberately, the split `tests/test_file_storage_integration.py` documents: a
    session that never commits never fires `delete_after_commit`'s hook, so these tests can
    assert what was *scheduled* and in what order. The hook runs for real against Postgres
    below.
    """
    result = MagicMock()
    result.one_or_none.return_value = held
    result.rowcount = rowcount

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.info = {}
    return db


def _held(
    *, original: str | None = "user-avatars/ff/original", rendition: str = "user-avatars/ff/rendition"
) -> SimpleNamespace:
    return SimpleNamespace(
        original_storage_key=original,
        rendition_storage_key=rendition,
        original_content_type="image/jpeg",
        original_filename="me.jpg",
    )


class TestStoreOrdering:
    @pytest.mark.asyncio
    async def test_the_files_are_in_the_store_before_the_commit(self, volume: Path) -> None:
        """The ordering rule. A crash after this point strands unreferenced files, which the
        sweeper reclaims; the reverse order commits a row naming bytes that never existed."""
        db = _session()
        when_committed: list[int] = []
        db.commit = AsyncMock(side_effect=lambda: when_committed.append(len(list(volume.rglob("user-portraits/*/*")))))

        await store_picture(
            db,
            user_id=1,
            frame=PORTRAIT_FRAME,
            upload=_upload(plain_png(size=(70, 90))),
            crop=PictureCrop(x=0, y=0, width=70, height=90),
        )

        assert when_committed == [2]

    @pytest.mark.asyncio
    async def test_the_original_is_kept_and_the_rendition_is_derived(self, volume: Path) -> None:
        source = phone_jpeg()
        db = _session()

        digest = await store_picture(db, user_id=1, frame=AVATAR_FRAME, upload=_upload(source), crop=_square((48, 64)))

        stored = {key.split("_")[-1]: (volume / key).read_bytes() for key in blob_store.iter_keys()}
        assert set(stored) == {digest, hashlib.sha256(strip_metadata(source, "image/jpeg")).hexdigest()}
        assert _open(stored[digest]).format == "WEBP"

    @pytest.mark.asyncio
    async def test_an_avatar_without_a_crop_keeps_no_original(self, volume: Path) -> None:
        db = _session()

        digest = await store_picture(db, user_id=1, frame=AVATAR_FRAME, upload=_upload(jpeg_with_exif()), crop=None)

        assert [key.rsplit("_", 1)[-1] for key in blob_store.iter_keys()] == [digest]

    @pytest.mark.asyncio
    async def test_the_replaced_keys_come_from_the_database_not_from_the_caller(self, volume: Path) -> None:
        """The stale-snapshot trap. What is scheduled for unlinking is exactly what the
        function's own narrow select returned - both files of the picture it replaces."""
        db = _session(held=_held())

        await store_picture(db, user_id=1, frame=AVATAR_FRAME, upload=_upload(jpeg_with_exif()), crop=None)

        assert db.info["blob_store_pending_deletes"] == ["user-avatars/ff/original", "user-avatars/ff/rendition"]

    @pytest.mark.asyncio
    async def test_a_first_upload_retires_nothing(self, volume: Path) -> None:
        db = _session(held=None)

        await store_picture(db, user_id=1, frame=AVATAR_FRAME, upload=_upload(jpeg_with_exif()), crop=None)

        assert "blob_store_pending_deletes" not in db.info

    @pytest.mark.asyncio
    async def test_the_read_transaction_is_released_before_the_write(self, volume: Path) -> None:
        db = _session()

        await store_picture(db, user_id=1, frame=AVATAR_FRAME, upload=_upload(jpeg_with_exif()), crop=None)

        db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_oversized_upload_is_a_413_before_anything_is_decoded(self, volume: Path) -> None:
        db = _session()

        with pytest.raises(HTTPException) as exc_info:
            await store_picture(
                db,
                user_id=1,
                frame=AVATAR_FRAME,
                upload=_upload(b"\xff\xd8\xff" + b"\x00" * MAX_PICTURE_UPLOAD_SIZE),
                crop=None,
            )

        assert exc_info.value.status_code == 413
        assert list(blob_store.iter_keys()) == []

    @pytest.mark.asyncio
    async def test_an_undecodable_upload_writes_nothing(self, volume: Path) -> None:
        db = _session()

        with pytest.raises(UnsupportedPictureError):
            await store_picture(db, user_id=1, frame=AVATAR_FRAME, upload=_upload(bmp()), crop=None)

        assert list(blob_store.iter_keys()) == []
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_crop_that_does_not_fit_writes_nothing(self, volume: Path) -> None:
        db = _session()

        with pytest.raises(InvalidCropError):
            await store_picture(
                db,
                user_id=1,
                frame=AVATAR_FRAME,
                upload=_upload(plain_png(size=(8, 8))),
                crop=PictureCrop(x=4, y=4, width=8, height=8),
            )

        assert list(blob_store.iter_keys()) == []


class TestRecrop:
    @pytest.mark.asyncio
    async def test_a_picture_without_an_original_has_nothing_to_adjust(self) -> None:
        db = _session(held=_held(original=None))

        assert await recrop_picture(db, user_id=1, frame=AVATAR_FRAME, crop=_square((8, 8))) is None
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_that_lost_to_a_replacement_unlinks_what_it_drew_and_says_so(
        self, volume: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The conditional `UPDATE` matched no row: the original it drew from is no longer
        the picture's. Committing would pair a new original with a rendition of the old one."""
        db = _session(held=_held(), rowcount=0)
        monkeypatch.setattr(user_pictures.blob_store, "get", AsyncMock(return_value=plain_png(size=(16, 16))))

        with pytest.raises(PictureChangedError):
            await recrop_picture(db, user_id=1, frame=AVATAR_FRAME, crop=_square((16, 16)))

        assert list(blob_store.iter_keys()) == []
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()


class TestDelete:
    @pytest.mark.asyncio
    async def test_it_schedules_both_unlinks_and_reports_that_there_was_one(self) -> None:
        db = _session(held=_held())

        assert await delete_picture(db, user_id=1, frame=AVATAR_FRAME) is True
        assert db.info["blob_store_pending_deletes"] == ["user-avatars/ff/original", "user-avatars/ff/rendition"]
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_account_with_no_picture_reports_nothing_to_remove(self) -> None:
        db = _session(held=None)

        assert await delete_picture(db, user_id=1, frame=PORTRAIT_FRAME) is False
        assert db.info == {}
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_delete_that_lost_to_a_replacement_unlinks_nothing(self) -> None:
        """The conditional `DELETE` matched no row, which means the keys it read are no
        longer the picture's. Unlinking here would destroy the replacement's live blobs."""
        db = _session(held=_held(), rowcount=0)

        assert await delete_picture(db, user_id=1, frame=AVATAR_FRAME) is False
        assert db.info == {}
        db.commit.assert_not_awaited()


class TestGoogleAvatarUrlGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "https://lh3.googleusercontent.com/a/photo",
            "https://googleusercontent.com/a/photo",
        ],
    )
    def test_a_google_cdn_url_is_accepted(self, url: str) -> None:
        assert _is_google_avatar_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            # Plain http, so the response could be rewritten in flight.
            "http://lh3.googleusercontent.com/a/photo",
            # The classic SSRF target, and the reason this guard exists at all.
            "http://169.254.169.254/latest/meta-data/",
            "https://169.254.169.254/latest/meta-data/",
            # A suffix that only looks like the allowlisted one.
            "https://evilgoogleusercontent.com/a/photo",
            # The host in the userinfo rather than in the authority.
            "https://lh3.googleusercontent.com@evil.example/a",
            "file:///etc/passwd",
        ],
    )
    def test_anything_else_is_refused(self, url: str) -> None:
        assert _is_google_avatar_url(url) is False


class TestGoogleImport:
    """Every failure has to end in `None`, because the caller is account creation.

    `POST /auth/complete` is the only place a `User` row is ever made, and a sign-up that
    fails because a CDN was slow is a worse outcome than an account that starts with
    initials.
    """

    @staticmethod
    def _transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
        """Point `import_google_avatar`'s client at a mock transport.

        Patching the class rather than the module's `httpx` attribute, so the call still
        goes through a real `AsyncClient` - the streaming read and the `follow_redirects`
        argument are part of what is under test.
        """
        real_client = httpx.AsyncClient

        def build(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            return real_client(*args, **{**kwargs, "transport": httpx.MockTransport(handler)})

        monkeypatch.setattr(user_pictures.httpx, "AsyncClient", build)

    @pytest.mark.asyncio
    async def test_the_happy_path_stores_a_normalized_avatar(
        self, volume: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._transport(monkeypatch, lambda request: httpx.Response(200, content=jpeg_with_exif()))

        stored = await import_google_avatar("https://lh3.googleusercontent.com/a/photo")

        assert stored is not None
        assert stored.storage_key.startswith(f"{user_pictures.AVATAR_FRAME.key_kind}/")
        data = (volume / stored.storage_key).read_bytes()
        assert hashlib.sha256(data).hexdigest() == stored.sha256
        assert _open(data).format == "WEBP"

    @pytest.mark.asyncio
    async def test_no_url_is_not_an_error(self, volume: Path) -> None:
        assert await import_google_avatar(None) is None

    @pytest.mark.asyncio
    async def test_a_url_outside_the_allowlist_is_never_fetched(
        self, volume: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            fetched.append(str(request.url))
            return httpx.Response(200, content=jpeg_with_exif())

        self._transport(monkeypatch, handler)

        assert await import_google_avatar("http://169.254.169.254/latest/meta-data/") is None
        assert fetched == []

    @pytest.mark.asyncio
    async def test_a_redirect_is_not_followed(self, volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 302 off the allowlisted host is the way a host check gets walked around."""
        self._transport(
            monkeypatch,
            lambda request: httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"}),
        )

        assert await import_google_avatar("https://lh3.googleusercontent.com/a/photo") is None
        assert list(blob_store.iter_keys()) == []

    @pytest.mark.asyncio
    async def test_a_timeout_degrades_to_no_avatar(self, volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow", request=request)

        self._transport(monkeypatch, handler)

        assert await import_google_avatar("https://lh3.googleusercontent.com/a/photo") is None

    @pytest.mark.asyncio
    async def test_a_response_over_the_size_cap_degrades_to_no_avatar(
        self, volume: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._transport(
            monkeypatch,
            lambda request: httpx.Response(200, content=b"\xff\xd8\xff" + b"\x00" * MAX_PICTURE_UPLOAD_SIZE),
        )

        assert await import_google_avatar("https://lh3.googleusercontent.com/a/photo") is None
        assert list(blob_store.iter_keys()) == []

    @pytest.mark.asyncio
    async def test_bytes_that_do_not_decode_degrade_to_no_avatar(
        self, volume: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._transport(monkeypatch, lambda request: httpx.Response(200, content=b"<html>sign in</html>"))

        assert await import_google_avatar("https://lh3.googleusercontent.com/a/photo") is None
        assert list(blob_store.iter_keys()) == []

    @pytest.mark.asyncio
    async def test_a_404_degrades_to_no_avatar(self, volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._transport(monkeypatch, lambda request: httpx.Response(404))

        assert await import_google_avatar("https://lh3.googleusercontent.com/a/photo") is None


class _FakePictureStore:
    """The service layer, in a dict, so the route tests exercise the handlers end to end.

    What the routes own is the status mapping, the crop's parsing, the `ETag` comparison and
    the response headers; storage itself is covered against a real volume and a real
    session elsewhere in this file.
    """

    def __init__(self) -> None:
        self.renditions: dict[str, StoredPictureFile] = {}
        self.originals: dict[str, StoredPictureFile] = {}
        self.blobs: dict[str, bytes] = {}

    def _put(self, data: bytes, content_type: str, filename: str) -> StoredPictureFile:
        digest = hashlib.sha256(data).hexdigest()
        key = f"{digest}-{uuid7()}"
        self.blobs[key] = data
        return StoredPictureFile(storage_key=key, sha256=digest, content_type=content_type, filename=filename)

    async def store(self, db: Any, *, user_id: int, frame: Any, upload: UploadFile, crop: PictureCrop | None) -> str:
        data = await upload.read()
        kind = frame.kind.value
        if crop is None:
            rendition = await user_pictures.process_avatar(data)
            self.originals.pop(kind, None)
        else:
            prepared = await user_pictures.process_original(data, frame, crop)
            assert prepared.original is not None and prepared.original_content_type is not None
            rendition = prepared.rendition
            self.originals[kind] = self._put(prepared.original, prepared.original_content_type, upload.filename or "")
        self.renditions[kind] = self._put(rendition, "image/webp", f"{kind}.webp")
        return self.renditions[kind].sha256

    async def recrop(self, db: Any, *, user_id: int, frame: Any, crop: PictureCrop) -> str | None:
        original = self.originals.get(frame.kind.value)
        if original is None:
            return None
        rendition = _normalize(self.blobs[original.storage_key], frame, crop)
        self.renditions[frame.kind.value] = self._put(rendition, "image/webp", f"{frame.kind.value}.webp")
        return self.renditions[frame.kind.value].sha256

    async def copy(self, db: Any, *, user_id: int, crop: PictureCrop) -> str | None:
        original = self.originals.get("avatar")
        if original is None:
            return None
        rendition = _normalize(self.blobs[original.storage_key], PORTRAIT_FRAME, crop)
        self.originals["portrait"] = original
        self.renditions["portrait"] = self._put(rendition, "image/webp", "portrait.webp")
        return self.renditions["portrait"].sha256

    async def get_rendition(self, db: Any, *, user_id: int, frame: Any) -> StoredPictureFile | None:
        return self.renditions.get(frame.kind.value)

    async def get_original(self, db: Any, *, user_id: int, frame: Any) -> StoredPictureFile | None:
        return self.originals.get(frame.kind.value)

    async def read(self, stored: StoredPictureFile) -> bytes:
        return self.blobs[stored.storage_key]

    async def delete(self, db: Any, *, user_id: int, frame: Any) -> bool:
        self.originals.pop(frame.kind.value, None)
        return self.renditions.pop(frame.kind.value, None) is not None


@pytest.fixture(scope="module")
def picture_app() -> Any:
    """The **real** application, not a bare `FastAPI()` with the router bolted on.

    That distinction is the point of these tests. `SecurityHeadersMiddleware` and
    `ClientCacheMiddleware` both rewrite headers on the way out, and a picture download's
    whole header story is about opting out of the first one's default without losing
    `frame-ancestors` - which a hand-built app would not exercise at all.
    """
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def picture_client(
    picture_app: Any, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[TestClient, _FakePictureStore]]:
    store = _FakePictureStore()
    for name, fake in (
        ("store_picture", store.store),
        ("recrop_picture", store.recrop),
        ("copy_avatar_to_portrait", store.copy),
        ("get_rendition", store.get_rendition),
        ("get_original", store.get_original),
        ("read_picture_bytes", store.read),
        ("delete_picture", store.delete),
    ):
        monkeypatch.setattr(f"src.app.api.v1.users.{name}", fake)

    picture_app.dependency_overrides[get_current_user] = lambda: CURRENT_USER
    picture_app.dependency_overrides[async_get_db] = lambda: AsyncMock()
    with TestClient(picture_app) as client:
        yield client, store
    picture_app.dependency_overrides = {}


def _crop_field(crop: PictureCrop) -> dict[str, str]:
    return {"crop": crop.model_dump_json()}


class TestPictureRoutes:
    def test_put_then_get_serves_back_exactly_what_was_stored(
        self, picture_client: tuple[TestClient, _FakePictureStore]
    ) -> None:
        client, _ = picture_client

        put = client.put(AVATAR_PATH, files={"file": ("me.jpg", jpeg_with_exif(), "image/jpeg")})
        assert put.status_code == 200
        digest = put.json()["sha256"]

        got = client.get(AVATAR_PATH, params={"v": digest})

        assert got.status_code == 200
        assert hashlib.sha256(got.content).hexdigest() == digest
        assert got.headers["content-type"] == "image/webp"

    @pytest.mark.parametrize(
        "path", [AVATAR_PATH, PORTRAIT_PATH, f"{AVATAR_PATH}/original", f"{PORTRAIT_PATH}/original"]
    )
    def test_every_picture_file_carries_the_card_routes_headers_verbatim(
        self, picture_client: tuple[TestClient, _FakePictureStore], path: str
    ) -> None:
        """`frame-ancestors` above all: a response with its own CSP opts out of
        `SecurityHeadersMiddleware`, so a shorter policy here is a silent downgrade."""
        client, _ = picture_client
        client.put(
            AVATAR_PATH, files={"file": ("me.jpg", phone_jpeg(), "image/jpeg")}, data=_crop_field(_square((48, 64)))
        )
        client.put(
            PORTRAIT_PATH,
            files={"file": ("me.jpg", phone_jpeg(), "image/jpeg")},
            data=_crop_field(PictureCrop(x=0, y=0, width=42, height=54)),
        )

        got = client.get(path)

        assert got.status_code == 200
        assert got.headers["content-security-policy"] == EXPECTED_CSP
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["cache-control"] == "private, max-age=300"
        assert got.headers["content-disposition"].startswith("attachment; ")
        conditional = client.get(path, headers={"If-None-Match": got.headers["etag"]})
        assert conditional.status_code == 304
        assert conditional.content == b""

    def test_the_original_is_served_under_its_own_name_and_type(
        self, picture_client: tuple[TestClient, _FakePictureStore]
    ) -> None:
        client, _ = picture_client
        client.put(
            AVATAR_PATH,
            files={"file": ("Tauchen in Dahab.png", screenshot_png(), "image/png")},
            data=_crop_field(PictureCrop(x=0, y=0, width=30, height=30)),
        )

        got = client.get(f"{AVATAR_PATH}/original")

        assert got.headers["content-type"] == "image/png"
        assert got.headers["content-disposition"].startswith('attachment; filename="Tauchen in Dahab.png"')
        assert b"GPS" not in got.content

    def test_a_stale_if_none_match_gets_the_new_bytes(
        self, picture_client: tuple[TestClient, _FakePictureStore]
    ) -> None:
        client, _ = picture_client
        client.put(AVATAR_PATH, files={"file": ("me.jpg", jpeg_with_exif(), "image/jpeg")})
        first = client.get(AVATAR_PATH).headers["etag"]

        client.put(AVATAR_PATH, files={"file": ("me.png", png_with_alpha(), "image/png")})
        second = client.get(AVATAR_PATH, headers={"If-None-Match": first})

        assert second.status_code == 200
        assert second.headers["etag"] != first

    @pytest.mark.parametrize(
        "path", [AVATAR_PATH, PORTRAIT_PATH, f"{AVATAR_PATH}/original", f"{PORTRAIT_PATH}/original"]
    )
    def test_a_file_that_is_not_there_is_404(
        self, picture_client: tuple[TestClient, _FakePictureStore], path: str
    ) -> None:
        client, _ = picture_client
        client.put(AVATAR_PATH, files={"file": ("me.jpg", jpeg_with_exif(), "image/jpeg")})

        assert client.get(path).status_code == (200 if path == AVATAR_PATH else 404)

    @pytest.mark.parametrize("path", [AVATAR_PATH, PORTRAIT_PATH])
    def test_delete_removes_it_and_a_second_delete_is_404(
        self, picture_client: tuple[TestClient, _FakePictureStore], path: str
    ) -> None:
        client, _ = picture_client
        client.put(
            path,
            files={"file": ("me.png", plain_png(size=(70, 90)), "image/png")},
            data=_crop_field(
                PictureCrop(x=0, y=0, width=70, height=90)
                if path == PORTRAIT_PATH
                else PictureCrop(x=0, y=0, width=70, height=70)
            ),
        )

        assert client.delete(path).status_code == 200
        assert client.get(path).status_code == 404
        assert client.delete(path).status_code == 404

    def test_a_portrait_without_a_crop_is_422(self, picture_client: tuple[TestClient, _FakePictureStore]) -> None:
        """Every portrait holds an original, and an original is only ever kept with the crop
        a client showed the diver."""
        client, store = picture_client

        response = client.put(PORTRAIT_PATH, files={"file": ("me.png", plain_png(size=(70, 90)), "image/png")})

        assert response.status_code == 422
        assert store.renditions == {}

    @pytest.mark.parametrize("raw", ["not json", '{"x": 0, "y": 0, "width": 0, "height": 9}', '{"x": 0, "y": 0}'])
    def test_a_malformed_crop_is_422_naming_the_field(
        self, picture_client: tuple[TestClient, _FakePictureStore], raw: str
    ) -> None:
        client, _ = picture_client

        response = client.put(
            PORTRAIT_PATH, files={"file": ("me.png", plain_png(size=(70, 90)), "image/png")}, data={"crop": raw}
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"][:2] == ["body", "crop"]

    @pytest.mark.parametrize(
        "crop", [PictureCrop(x=0, y=0, width=70, height=70), PictureCrop(x=10, y=10, width=70, height=90)]
    )
    def test_a_crop_off_the_ratio_or_outside_the_image_is_422(
        self, picture_client: tuple[TestClient, _FakePictureStore], crop: PictureCrop
    ) -> None:
        client, _ = picture_client

        response = client.put(
            PORTRAIT_PATH, files={"file": ("me.png", plain_png(size=(70, 90)), "image/png")}, data=_crop_field(crop)
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "crop"]

    @pytest.mark.parametrize(
        ("source", "crop"),
        [(webp_with_alpha(size=(16, 16)), True), (gif(), True), (bmp(), False)],
    )
    def test_an_image_it_will_not_keep_or_render_is_415(
        self, picture_client: tuple[TestClient, _FakePictureStore], source: bytes, crop: bool
    ) -> None:
        """A WebP or GIF is refused as an original and still accepted as a crop-less avatar."""
        client, _ = picture_client

        response = client.put(
            AVATAR_PATH,
            files={"file": ("me", source, "application/octet-stream")},
            data=_crop_field(_square((8, 8))) if crop else None,
        )

        assert response.status_code == 415
        assert "JPEG" in response.json()["detail"] and "PNG" in response.json()["detail"]

    def test_an_oversized_upload_is_413(
        self, picture_client: tuple[TestClient, _FakePictureStore], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`read_upload_within_limit` raises this from inside the service, so the route's
        job is to let it through rather than to catch it as an unsupported image."""
        client, _ = picture_client

        async def oversized(db: Any, **kwargs: Any) -> str:
            raise HTTPException(status_code=413, detail="File too large. Maximum allowed size is 10 MB.")

        monkeypatch.setattr("src.app.api.v1.users.store_picture", oversized)

        assert client.put(AVATAR_PATH, files={"file": ("huge.jpg", b"\xff\xd8\xff", "image/jpeg")}).status_code == 413

    @pytest.mark.parametrize("path", [AVATAR_PATH, PORTRAIT_PATH])
    def test_adjusting_re_renders_from_the_original_it_holds(
        self, picture_client: tuple[TestClient, _FakePictureStore], path: str
    ) -> None:
        client, store = picture_client
        whole = (
            PictureCrop(x=0, y=0, width=140, height=180)
            if path == PORTRAIT_PATH
            else PictureCrop(x=0, y=0, width=140, height=140)
        )
        first = client.put(
            path, files={"file": ("me.png", plain_png(size=(140, 180)), "image/png")}, data=_crop_field(whole)
        ).json()["sha256"]
        kind = path.rsplit("/", 1)[-1]
        original = store.originals[kind]

        smaller = (
            PictureCrop(x=0, y=0, width=70, height=90)
            if path == PORTRAIT_PATH
            else PictureCrop(x=0, y=0, width=70, height=70)
        )
        adjusted = client.patch(path, json={"crop": smaller.model_dump()})

        assert adjusted.status_code == 200
        assert adjusted.json()["sha256"] != first
        assert store.originals[kind] == original

    def test_adjusting_a_picture_that_kept_no_original_is_404(
        self, picture_client: tuple[TestClient, _FakePictureStore]
    ) -> None:
        client, _ = picture_client
        client.put(AVATAR_PATH, files={"file": ("me.jpg", jpeg_with_exif(), "image/jpeg")})

        assert client.patch(AVATAR_PATH, json={"crop": _square((8, 8)).model_dump()}).status_code == 404

    def test_an_adjustment_that_lost_a_race_is_409(
        self, picture_client: tuple[TestClient, _FakePictureStore], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = picture_client

        async def changed(db: Any, **kwargs: Any) -> str:
            raise PictureChangedError("The picture changed while it was being adjusted.")

        monkeypatch.setattr("src.app.api.v1.users.recrop_picture", changed)

        assert client.patch(PORTRAIT_PATH, json={"crop": _square((9, 9)).model_dump()}).status_code == 409

    def test_the_copy_is_404_while_the_avatar_holds_no_original(
        self, picture_client: tuple[TestClient, _FakePictureStore]
    ) -> None:
        """What an avatar seeded from Google, or stored before originals were kept, looks
        like - there is no file to copy, and the diver uploads instead."""
        client, _ = picture_client
        client.put(AVATAR_PATH, files={"file": ("me.jpg", jpeg_with_exif(), "image/jpeg")})

        response = client.post(f"{PORTRAIT_PATH}/from-avatar", json={"crop": {"x": 0, "y": 0, "width": 7, "height": 9}})

        assert response.status_code == 404

    def test_the_copy_frames_the_avatars_original_at_7_9(
        self, picture_client: tuple[TestClient, _FakePictureStore]
    ) -> None:
        client, _ = picture_client
        client.put(
            AVATAR_PATH,
            files={"file": ("me.png", plain_png(size=(140, 180)), "image/png")},
            data=_crop_field(PictureCrop(x=0, y=0, width=140, height=140)),
        )

        copied = client.post(
            f"{PORTRAIT_PATH}/from-avatar", json={"crop": {"x": 0, "y": 0, "width": 140, "height": 180}}
        )

        assert copied.status_code == 200
        assert _open(client.get(PORTRAIT_PATH).content).size == (140, 180)

    def test_a_crop_body_takes_nothing_else(self, picture_client: tuple[TestClient, _FakePictureStore]) -> None:
        client, _ = picture_client

        response = client.patch(AVATAR_PATH, json={"crop": _square((8, 8)).model_dump(), "original": "x"})

        assert response.status_code == 422


def test_neither_avatar_column_is_mapped() -> None:
    """No request's query can select them: FastCRUD selects every mapped column, so a mapped
    one would be selected on every signed-in request and break the day it is dropped."""
    mapped = {attribute.key for attribute in inspect(User).column_attrs}

    assert {"avatar_storage_key", "avatar_sha256"} & mapped == set()
    assert {"avatar_storage_key", "avatar_sha256"} <= set(user_table.c.keys())


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestAgainstPostgres:
    """The round trips with a real session, real rows and a real volume.

    What only a real database can settle: the row and the `user` columns actually land, the
    `after_commit` hook actually unlinks (the mocked sessions above never commit, by design),
    and a replacement retires exactly the files it replaced.
    """

    @staticmethod
    async def _columns(async_db: AsyncSession, user_id: int) -> tuple[str | None, str | None]:
        row = (
            await async_db.execute(
                select(USER_AVATAR_STORAGE_KEY, USER_AVATAR_SHA256).where(user_table.c.id == user_id)
            )
        ).one()
        return row[0], row[1]

    @pytest.mark.asyncio
    async def test_an_avatar_with_a_crop_keeps_its_original_and_writes_the_columns(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        diver = create_user(db)
        source = phone_jpeg()

        digest = await store_picture(
            async_db,
            user_id=diver.id,
            frame=AVATAR_FRAME,
            upload=_upload(source, "Me at Dahab.jpg"),
            crop=_square((48, 64)),
        )

        original = await get_original(async_db, user_id=diver.id, frame=AVATAR_FRAME)
        rendition = await get_rendition(async_db, user_id=diver.id, frame=AVATAR_FRAME)
        assert original is not None and rendition is not None
        assert rendition.sha256 == digest
        assert original.filename == "Me at Dahab.jpg"
        assert original.content_type == "image/jpeg"
        assert await user_pictures.read_picture_bytes(original) == strip_metadata(source, "image/jpeg")
        assert await self._columns(async_db, diver.id) == (rendition.storage_key, digest)

    @pytest.mark.asyncio
    async def test_the_account_read_carries_each_pictures_fields(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        """`GET /user` returns `get_current_user`'s dict as it is, so this is its body."""
        diver = create_user(db)
        crop = PictureCrop(x=0, y=0, width=42, height=54)
        portrait = await store_picture(
            async_db, user_id=diver.id, frame=PORTRAIT_FRAME, upload=_upload(phone_jpeg()), crop=crop
        )
        avatar = await store_picture(
            async_db, user_id=diver.id, frame=AVATAR_FRAME, upload=_upload(jpeg_with_exif()), crop=None
        )

        account = await read_account(async_db, uuid=diver.uuid)

        assert account is not None
        read = UserRead.model_validate(account)
        assert (read.avatar_sha256, read.avatar_original_sha256, read.avatar_crop) == (avatar, None, None)
        assert read.portrait_sha256 == portrait
        assert read.portrait_original_sha256 == hashlib.sha256(strip_metadata(phone_jpeg(), "image/jpeg")).hexdigest()
        assert read.portrait_crop == crop
        assert account["id"] == diver.id

    @pytest.mark.asyncio
    async def test_an_account_without_pictures_reads_as_nulls(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)

        account = await read_account(async_db, uuid=diver.uuid)

        assert account is not None
        assert {
            key: account[key] for key in ("avatar_sha256", "avatar_crop", "portrait_sha256", "portrait_crop")
        } == dict.fromkeys(("avatar_sha256", "avatar_crop", "portrait_sha256", "portrait_crop"))

    @pytest.mark.asyncio
    async def test_a_replacement_retires_both_files_it_replaced(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        diver = create_user(db)
        crop = PictureCrop(x=0, y=0, width=70, height=90)
        await store_picture(
            async_db, user_id=diver.id, frame=PORTRAIT_FRAME, upload=_upload(plain_png(size=(70, 90))), crop=crop
        )
        first = [
            await get_original(async_db, user_id=diver.id, frame=PORTRAIT_FRAME),
            await get_rendition(async_db, user_id=diver.id, frame=PORTRAIT_FRAME),
        ]

        await store_picture(
            async_db,
            user_id=diver.id,
            frame=PORTRAIT_FRAME,
            upload=_upload(phone_jpeg()),
            crop=PictureCrop(x=0, y=0, width=42, height=54),
        )
        await blob_store._await_pending_removals()

        assert [file for file in first if file is not None and blob_store.exists(file.storage_key)] == []
        assert len(list(blob_store.iter_keys())) == 2

    @pytest.mark.asyncio
    async def test_an_adjustment_re_renders_without_touching_the_original(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        diver = create_user(db)
        await store_picture(
            async_db,
            user_id=diver.id,
            frame=PORTRAIT_FRAME,
            upload=_upload(plain_png(size=(140, 180))),
            crop=PictureCrop(x=0, y=0, width=140, height=180),
        )
        original = await get_original(async_db, user_id=diver.id, frame=PORTRAIT_FRAME)
        before = await get_rendition(async_db, user_id=diver.id, frame=PORTRAIT_FRAME)
        uuid_before = (
            await async_db.execute(select(UserPicture.uuid).where(UserPicture.user_id == diver.id))
        ).scalar_one()

        digest = await recrop_picture(
            async_db, user_id=diver.id, frame=PORTRAIT_FRAME, crop=PictureCrop(x=70, y=90, width=70, height=90)
        )
        await blob_store._await_pending_removals()

        after = await get_rendition(async_db, user_id=diver.id, frame=PORTRAIT_FRAME)
        assert after is not None and before is not None and digest == after.sha256 != before.sha256
        assert await get_original(async_db, user_id=diver.id, frame=PORTRAIT_FRAME) == original
        assert not blob_store.exists(before.storage_key)
        assert (
            await async_db.execute(select(UserPicture.uuid).where(UserPicture.user_id == diver.id))
        ).scalar_one() == uuid_before

    @pytest.mark.asyncio
    async def test_an_avatar_adjustment_moves_the_columns_with_the_row(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        diver = create_user(db)
        await store_picture(
            async_db,
            user_id=diver.id,
            frame=AVATAR_FRAME,
            upload=_upload(plain_png(size=(32, 32))),
            crop=_square((32, 32)),
        )

        digest = await recrop_picture(
            async_db, user_id=diver.id, frame=AVATAR_FRAME, crop=PictureCrop(x=0, y=0, width=16, height=16)
        )

        rendition = await get_rendition(async_db, user_id=diver.id, frame=AVATAR_FRAME)
        assert rendition is not None and rendition.sha256 == digest
        assert await self._columns(async_db, diver.id) == (rendition.storage_key, digest)

    @pytest.mark.asyncio
    async def test_the_copy_keeps_its_own_files_and_outlives_the_avatar(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        diver = create_user(db)
        assert (
            await copy_avatar_to_portrait(async_db, user_id=diver.id, crop=PictureCrop(x=0, y=0, width=42, height=54))
            is None
        )
        await store_picture(
            async_db,
            user_id=diver.id,
            frame=AVATAR_FRAME,
            upload=_upload(phone_jpeg(), "me.jpg"),
            crop=_square((48, 64)),
        )
        avatar_original = await get_original(async_db, user_id=diver.id, frame=AVATAR_FRAME)

        await copy_avatar_to_portrait(async_db, user_id=diver.id, crop=PictureCrop(x=0, y=0, width=42, height=54))
        portrait_original = await get_original(async_db, user_id=diver.id, frame=PORTRAIT_FRAME)

        assert avatar_original is not None and portrait_original is not None
        assert portrait_original.storage_key != avatar_original.storage_key
        assert portrait_original.storage_key.startswith("user-portraits/")
        assert (portrait_original.sha256, portrait_original.filename) == (avatar_original.sha256, "me.jpg")

        assert await delete_picture(async_db, user_id=diver.id, frame=AVATAR_FRAME) is True
        await blob_store._await_pending_removals()

        assert not blob_store.exists(avatar_original.storage_key)
        portrait = await get_rendition(async_db, user_id=diver.id, frame=PORTRAIT_FRAME)
        assert portrait is not None
        assert _open(await user_pictures.read_picture_bytes(portrait)).size == (42, 54)
        assert await user_pictures.read_picture_bytes(portrait_original) == await blob_store.get(
            portrait_original.storage_key
        )

    @pytest.mark.asyncio
    async def test_delete_clears_the_row_and_the_columns_and_unlinks_both_files(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        diver = create_user(db)
        await store_picture(
            async_db, user_id=diver.id, frame=AVATAR_FRAME, upload=_upload(phone_jpeg()), crop=_square((48, 64))
        )

        assert await delete_picture(async_db, user_id=diver.id, frame=AVATAR_FRAME) is True
        await blob_store._await_pending_removals()

        assert await get_rendition(async_db, user_id=diver.id, frame=AVATAR_FRAME) is None
        assert await self._columns(async_db, diver.id) == (None, None)
        assert list(blob_store.iter_keys()) == []
        assert await delete_picture(async_db, user_id=diver.id, frame=AVATAR_FRAME) is False

    @pytest.mark.asyncio
    async def test_the_google_seed_writes_a_rendition_only_row_and_the_columns(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        seeded = StoredAvatar(storage_key=f"user-avatars/ab/{uuid7()}_{'a' * 64}", sha256="a" * 64)

        await seed_google_avatar(async_db, user_id=diver.id, stored=seeded)
        await async_db.commit()

        assert await get_original(async_db, user_id=diver.id, frame=AVATAR_FRAME) is None
        rendition = await get_rendition(async_db, user_id=diver.id, frame=AVATAR_FRAME)
        assert rendition is not None and (rendition.storage_key, rendition.sha256) == (
            seeded.storage_key,
            seeded.sha256,
        )
        assert await self._columns(async_db, diver.id) == (seeded.storage_key, seeded.sha256)


_REVISION = "272bb184cbdd_a_portrait_beside_the_avatar_and_both"


def _revision_sql(name: str) -> str:
    """One of the revision's statements, loaded by path - `migrations/versions/` is not an
    importable package, and a second copy of the SQL here could quietly stop matching."""
    spec = importlib.util.spec_from_file_location(_REVISION, MIGRATIONS_PATH / "versions" / f"{_REVISION}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(str, getattr(module, name))


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheRevisionsDataPath:
    """The flagship's stored avatars move into rows on upgrade, and back on downgrade."""

    def test_every_stored_avatar_becomes_a_row_naming_the_same_file(self, db: Session) -> None:
        diver, bare = create_user(db), create_user(db)
        key = f"user-avatars/ab/{uuid7()}_{'e' * 64}"
        set_avatar_columns(db, diver, key=key, sha256="e" * 64)
        db.execute(text("DELETE FROM user_picture WHERE user_id IN (:a, :b)"), {"a": diver.id, "b": bare.id})
        db.commit()

        db.execute(text(_revision_sql("BACKFILL") + " AND id IN (:a, :b)"), {"a": diver.id, "b": bare.id})
        db.commit()

        rows = db.execute(select(UserPicture).where(UserPicture.user_id.in_((diver.id, bare.id)))).scalars().all()
        assert [(row.user_id, row.kind, row.rendition_storage_key, row.rendition_sha256) for row in rows] == [
            (diver.id, "avatar", key, "e" * 64)
        ]
        assert (rows[0].original_storage_key, rows[0].crop_x, rows[0].uuid is not None) == (None, None, True)
        columns = db.execute(
            select(USER_AVATAR_STORAGE_KEY, USER_AVATAR_SHA256).where(user_table.c.id == diver.id)
        ).one()
        assert tuple(columns) == (key, "e" * 64)

    def test_the_downgrade_copies_each_avatar_back_into_the_columns(self, db: Session) -> None:
        diver = create_user(db)
        picture = UserPicture(
            user_id=diver.id,
            kind="avatar",
            rendition_storage_key=f"user-avatars/cd/{uuid7()}_{'c' * 64}",
            rendition_sha256="c" * 64,
        )
        db.add(picture)
        db.commit()
        set_avatar_columns(db, diver, key=None, sha256=None)

        db.execute(text(_revision_sql("RESTORE") + ' AND "user".id = :id'), {"id": diver.id})
        db.commit()

        columns = db.execute(
            select(USER_AVATAR_STORAGE_KEY, USER_AVATAR_SHA256).where(user_table.c.id == diver.id)
        ).one()
        assert tuple(columns) == (picture.rendition_storage_key, "c" * 64)
