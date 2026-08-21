"""The avatar feature: `services/user_avatars.py` and the three `/user/avatar` routes.

Three groups, and the first is the one that earns the dependency. **Normalization is a
security boundary**, not a resizing convenience: Pillow is being handed bytes an anonymous
caller chose, and what comes out has to be metadata-free whatever went in. So the branches
tested here are the rejections and the strippings, not the happy resize.

Three of those rejections are about size, and they look like one check. An image between
the app's claimed-size cap and Pillow's own limit is rejected by `_normalize`'s explicit
test; an image past 178,956,970 px never reaches that line, because `Image.open` raises
`DecompressionBombError` first; and an image that passes both can still be refused for what
it would *rasterize* to, which is a different question from what it claims and the only one
that governs memory. A test suite that covered one of the three would leave the largest
inputs 500ing and the most compressible ones taking hundreds of megabytes. All three are
covered below, and the bomb band twice - once against Pillow's real limit, once against a
lowered one, so the `except` clause is executed rather than merely reasoned about.

The store/delete group is about ordering (file before row, unlink after commit) and about
*where the retired key comes from*, which is the other silent failure: reading it from the
request-start `current_user` snapshot rather than from the database would let one upload
unlink another's freshly committed blob.
"""

import hashlib
import io
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import httpx
import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.services import blob_store, user_avatars
from src.app.services.user_avatars import (
    AVATAR_DIMENSION,
    MAX_AVATAR_DECODE_PIXELS,
    MAX_AVATAR_PIXELS,
    MAX_AVATAR_UPLOAD_SIZE,
    StoredAvatar,
    UnsupportedAvatarImageError,
    _is_google_avatar_url,
    _normalize,
    delete_user_avatar,
    get_stored_avatar,
    import_google_avatar,
    store_user_avatar,
)
from tests.conftest import db_available
from tests.helpers.generators import create_user
from tests.helpers.images import (
    MARKED_JPEG_SIZE,
    animated_gif,
    bmp,
    jpeg_with_exif,
    large_jpeg,
    plain_png,
    png_declaring,
    png_with_alpha,
    webp_with_alpha,
)

USER_UUID = uuid7()
CURRENT_USER = {"id": 1, "uuid": USER_UUID, "username": "ada", "is_superuser": False}

# Copied verbatim from `read_certification_file`'s response. `frame-ancestors` is the
# load-bearing clause: a response that sets its own policy opts out of
# `SecurityHeadersMiddleware`'s default, so a shorter CSP here would silently make the
# avatar framable however strict the rest of the app is.
EXPECTED_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"

AVATAR_PATH = "/api/v1/user/avatar"


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


def _upload(content: bytes, filename: str = "me.jpg") -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


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

        monkeypatch.setattr(user_avatars.Image, "open", spy)

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
        real_run_sync = user_avatars.anyio.to_thread.run_sync

        async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return await real_run_sync(func, *args, **kwargs)

        monkeypatch.setattr(user_avatars.anyio.to_thread, "run_sync", spy)

        await user_avatars.process_avatar(jpeg_with_exif())

        assert captured["limiter"] is user_avatars._DECODE_LIMITER
        assert user_avatars._DECODE_LIMITER.total_tokens == 1
        assert user_avatars._DECODE_LIMITER is not anyio.to_thread.current_default_thread_limiter()

    def test_an_image_over_the_pixel_cap_is_refused_from_its_header_alone(self) -> None:
        """The app's own band. The fixture declares 56 MP in 74 bytes, which is the point:
        the check reads `Image.size` and never decodes, so a real raster would be 168 MB of
        test memory spent proving nothing extra."""
        oversized = png_declaring(8000, 7000)
        assert 8000 * 7000 > MAX_AVATAR_PIXELS

        with pytest.raises(UnsupportedAvatarImageError):
            _normalize(oversized)

    def test_a_decompression_bomb_is_refused_before_the_cap_can_run(self) -> None:
        """Pillow's band, above 178,956,970 px, where `Image.open` raises on its own.

        Uncaught this is a 500 rather than a 415 - and the test above would never notice,
        because any fixture under that threshold exercises only the app's own check.
        """
        with pytest.raises(UnsupportedAvatarImageError):
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
        assert 3000 * 3000 < MAX_AVATAR_PIXELS
        assert 3000 * 3000 > MAX_AVATAR_DECODE_PIXELS

        with pytest.raises(UnsupportedAvatarImageError) as exc_info:
            _normalize(png_declaring(3000, 3000))

        # No format advice in the message, deliberately: a JPEG can reach this branch too
        # (`draft` does not reduce one whose short edge is under 1024), and "save it as a
        # JPEG" would be unactionable for exactly that caller.
        assert "JPEG" not in str(exc_info.value)

    def test_the_same_dimensions_as_a_jpeg_are_accepted(self) -> None:
        """The pair to the test above, and the reason the decode cap does not read as
        "no photos above 4 MP". A camera produces JPEG, `draft` reduces JPEG before the
        cap is asked, and the same 9 MP that is refused as a PNG arrives here as 0.6 MP.
        """
        result = _open(_normalize(large_jpeg(size=(3000, 3000))))

        assert result.size == (AVATAR_DIMENSION, AVATAR_DIMENSION)

    def test_the_bomb_guard_is_the_catch_and_not_the_fixture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The same claim with the threshold moved under an ordinary image, so the `except`
        clause is *executed* rather than inferred from a fixture nobody can shrink."""
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 16)

        with pytest.raises(UnsupportedAvatarImageError):
            _normalize(plain_png(size=(64, 64)))

    def test_a_format_outside_the_allowlist_is_refused(self) -> None:
        """A valid BMP, and refused for being a BMP. `formats=` is what keeps the exotic
        decoders Pillow ships unreachable from an anonymous byte string."""
        with pytest.raises(UnsupportedAvatarImageError):
            _normalize(bmp())

    def test_bytes_that_are_not_an_image_at_all_are_refused(self) -> None:
        with pytest.raises(UnsupportedAvatarImageError):
            _normalize(b"not an image, just some text")

    @pytest.mark.asyncio
    async def test_an_empty_upload_is_refused_rather_than_decoded(self) -> None:
        with pytest.raises(UnsupportedAvatarImageError):
            await user_avatars.process_avatar(b"")


def _session(*, existing_key: str | None = None, rowcount: int = 1) -> AsyncMock:
    """A mocked session whose narrow key lookup answers `existing_key`.

    Mocked deliberately, the same split `tests/test_file_storage_integration.py` documents:
    a session that never commits never fires `delete_after_commit`'s hook, so these tests
    can assert what was *scheduled* and in what order. The hook itself is exercised against
    a real session in `tests/test_blob_store.py`, and end to end against Postgres below.
    """
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing_key
    result.one_or_none.return_value = (
        None if existing_key is None else SimpleNamespace(avatar_storage_key=existing_key, avatar_sha256="0" * 64)
    )
    result.rowcount = rowcount

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.info = {}
    return db


class TestStoreOrdering:
    @pytest.mark.asyncio
    async def test_the_file_is_on_the_volume_before_the_commit(self, volume: Path) -> None:
        """The ordering rule. A crash after this point strands an unreferenced file, which
        the sweeper reclaims; the reverse order commits a row naming bytes that never
        existed."""
        db = _session()
        when_committed: list[bool] = []
        db.commit = AsyncMock(side_effect=lambda: when_committed.append(any(volume.rglob("user-avatars/*/*"))))

        await store_user_avatar(db, user_id=1, upload=_upload(jpeg_with_exif()))

        assert when_committed == [True]

    @pytest.mark.asyncio
    async def test_the_stored_bytes_are_the_normalized_ones_and_the_key_names_their_digest(self, volume: Path) -> None:
        """The digest on the row is of what was *stored*, not of what was uploaded - it is
        the `ETag` the download route answers with, so anything else makes conditional
        requests lie."""
        source = jpeg_with_exif()
        db = _session()

        digest = await store_user_avatar(db, user_id=1, upload=_upload(source))

        written = list(blob_store.iter_keys())
        assert len(written) == 1
        kind, shard, name = written[0].split("/")
        assert kind == user_avatars.KEY_KIND
        assert shard == digest[:2]
        assert name.endswith(f"_{digest}")

        stored = (volume / written[0]).read_bytes()
        assert hashlib.sha256(stored).hexdigest() == digest
        assert digest != hashlib.sha256(source).hexdigest()
        assert _open(stored).format == "WEBP"

    @pytest.mark.asyncio
    async def test_the_replaced_key_comes_from_the_database_not_from_the_caller(self, volume: Path) -> None:
        """The stale-snapshot trap. `store_user_avatar` takes no `current_user` at all, and
        what it schedules for unlinking is exactly what its own narrow select returned - so
        a second upload arriving while the first is in flight cannot retire the blob the
        first has just committed."""
        db = _session(existing_key="user-avatars/ff/previous")

        await store_user_avatar(db, user_id=1, upload=_upload(jpeg_with_exif()))

        assert db.info["blob_store_pending_deletes"] == ["user-avatars/ff/previous"]

    @pytest.mark.asyncio
    async def test_a_first_upload_retires_nothing(self, volume: Path) -> None:
        db = _session(existing_key=None)

        await store_user_avatar(db, user_id=1, upload=_upload(jpeg_with_exif()))

        assert "blob_store_pending_deletes" not in db.info

    @pytest.mark.asyncio
    async def test_the_read_transaction_is_released_before_the_write(self, volume: Path) -> None:
        """The lookup autobegins a transaction that would otherwise idle across a threadpool
        write with an `fsync` in it - fifteen concurrent uploads and the pool is gone."""
        db = _session()

        await store_user_avatar(db, user_id=1, upload=_upload(jpeg_with_exif()))

        db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_oversized_upload_is_a_413_before_anything_is_decoded(self, volume: Path) -> None:
        db = _session()

        with pytest.raises(HTTPException) as exc_info:
            await store_user_avatar(db, user_id=1, upload=_upload(b"\xff\xd8\xff" + b"\x00" * MAX_AVATAR_UPLOAD_SIZE))

        assert exc_info.value.status_code == 413
        assert list(blob_store.iter_keys()) == []

    @pytest.mark.asyncio
    async def test_an_undecodable_upload_writes_nothing(self, volume: Path) -> None:
        db = _session()

        with pytest.raises(UnsupportedAvatarImageError):
            await store_user_avatar(db, user_id=1, upload=_upload(bmp()))

        assert list(blob_store.iter_keys()) == []
        db.commit.assert_not_awaited()


class TestDelete:
    @pytest.mark.asyncio
    async def test_it_schedules_the_unlink_and_reports_that_there_was_one(self) -> None:
        db = _session(existing_key="user-avatars/ff/current")

        assert await delete_user_avatar(db, user_id=1) is True
        assert db.info["blob_store_pending_deletes"] == ["user-avatars/ff/current"]
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_account_with_no_avatar_reports_nothing_to_remove(self) -> None:
        db = _session(existing_key=None)

        assert await delete_user_avatar(db, user_id=1) is False
        assert db.info == {}
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_delete_that_lost_to_a_replacement_unlinks_nothing(self) -> None:
        """The conditional `UPDATE` matched no row, which means the key it read is no longer
        the account's - a concurrent replacement wrote a new one. Unlinking here would
        destroy the replacement's live blob."""
        db = _session(existing_key="user-avatars/ff/superseded", rowcount=0)

        assert await delete_user_avatar(db, user_id=1) is False
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

        monkeypatch.setattr(user_avatars.httpx, "AsyncClient", build)

    @pytest.mark.asyncio
    async def test_the_happy_path_stores_a_normalized_avatar(
        self, volume: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._transport(monkeypatch, lambda request: httpx.Response(200, content=jpeg_with_exif()))

        stored = await import_google_avatar("https://lh3.googleusercontent.com/a/photo")

        assert stored is not None
        assert stored.storage_key.startswith(f"{user_avatars.KEY_KIND}/")
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
            lambda request: httpx.Response(200, content=b"\xff\xd8\xff" + b"\x00" * MAX_AVATAR_UPLOAD_SIZE),
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


class _FakeAvatarStore:
    """The service layer, in a dict, so the route tests exercise the handlers end to end.

    What the routes own is the status mapping, the `ETag` comparison and the response
    headers; storage itself is covered against a real volume and a real session above.
    """

    def __init__(self) -> None:
        self.stored: StoredAvatar | None = None
        self.blobs: dict[str, bytes] = {}

    async def store(self, db: Any, *, user_id: int, upload: UploadFile) -> str:
        data = await upload.read()
        processed = await user_avatars.process_avatar(data)
        digest = hashlib.sha256(processed).hexdigest()
        key = blob_store.new_key(user_avatars.KEY_KIND, sha256=digest)
        self.blobs[key] = processed
        self.stored = StoredAvatar(storage_key=key, sha256=digest)
        return digest

    async def get(self, db: Any, *, user_id: int) -> StoredAvatar | None:
        return self.stored

    async def read(self, stored: StoredAvatar) -> bytes:
        return self.blobs[stored.storage_key]

    async def delete(self, db: Any, *, user_id: int) -> bool:
        if self.stored is None:
            return False
        self.stored = None
        return True


@pytest.fixture(scope="module")
def avatar_app() -> Any:
    """The **real** application, not a bare `FastAPI()` with the router bolted on.

    That distinction is the point of these tests. `SecurityHeadersMiddleware` and
    `ClientCacheMiddleware` both rewrite headers on the way out, and the avatar download's
    whole header story is about opting out of the first one's default without losing
    `frame-ancestors` - which a hand-built app would not exercise at all.

    `apply_migrations_on_start=False` for the reason `test_export_endpoints.py` gives:
    nothing below the route is real here, so there is no call for a database at startup.
    """
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def avatar_client(avatar_app: Any, monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, _FakeAvatarStore]]:
    store = _FakeAvatarStore()
    monkeypatch.setattr("src.app.api.v1.users.store_user_avatar", store.store)
    monkeypatch.setattr("src.app.api.v1.users.get_stored_avatar", store.get)
    monkeypatch.setattr("src.app.api.v1.users.read_avatar_bytes", store.read)
    monkeypatch.setattr("src.app.api.v1.users.delete_user_avatar", store.delete)

    avatar_app.dependency_overrides[get_current_user] = lambda: CURRENT_USER
    avatar_app.dependency_overrides[async_get_db] = lambda: AsyncMock()
    with TestClient(avatar_app) as client:
        yield client, store
    avatar_app.dependency_overrides = {}


class TestAvatarRoutes:
    def test_put_then_get_serves_back_exactly_what_was_stored(
        self, avatar_client: tuple[TestClient, _FakeAvatarStore]
    ) -> None:
        client, store = avatar_client

        put = client.put(AVATAR_PATH, files={"file": ("me.jpg", jpeg_with_exif(), "image/jpeg")})
        assert put.status_code == 200
        digest = put.json()["sha256"]

        got = client.get(AVATAR_PATH, params={"v": digest})

        assert got.status_code == 200
        assert hashlib.sha256(got.content).hexdigest() == digest
        assert got.headers["content-type"] == "image/webp"
        assert _open(got.content).format == "WEBP"

    def test_the_response_carries_the_card_routes_headers_verbatim(
        self, avatar_client: tuple[TestClient, _FakeAvatarStore]
    ) -> None:
        """`frame-ancestors` above all: a response with its own CSP opts out of
        `SecurityHeadersMiddleware`, so a shorter policy here is a silent downgrade."""
        client, _ = avatar_client
        client.put(AVATAR_PATH, files={"file": ("me.jpg", jpeg_with_exif(), "image/jpeg")})

        got = client.get(AVATAR_PATH)

        assert got.headers["content-security-policy"] == EXPECTED_CSP
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["cache-control"] == "private, max-age=300"
        assert got.headers["content-disposition"].startswith('attachment; filename="avatar.webp"')

    def test_a_matching_if_none_match_is_a_304_with_no_body(
        self, avatar_client: tuple[TestClient, _FakeAvatarStore]
    ) -> None:
        client, _ = avatar_client
        client.put(AVATAR_PATH, files={"file": ("me.jpg", jpeg_with_exif(), "image/jpeg")})
        etag = client.get(AVATAR_PATH).headers["etag"]

        conditional = client.get(AVATAR_PATH, headers={"If-None-Match": etag})

        assert conditional.status_code == 304
        assert conditional.content == b""
        assert conditional.headers["cache-control"] == "private, max-age=300"

    def test_a_stale_if_none_match_gets_the_new_bytes(self, avatar_client: tuple[TestClient, _FakeAvatarStore]) -> None:
        """The replace case, which is the only reason the `v` contract exists."""
        client, _ = avatar_client
        client.put(AVATAR_PATH, files={"file": ("me.jpg", jpeg_with_exif(), "image/jpeg")})
        first = client.get(AVATAR_PATH).headers["etag"]

        client.put(AVATAR_PATH, files={"file": ("me.png", png_with_alpha(), "image/png")})
        second = client.get(AVATAR_PATH, headers={"If-None-Match": first})

        assert second.status_code == 200
        assert second.headers["etag"] != first

    def test_an_account_with_no_avatar_gets_404(self, avatar_client: tuple[TestClient, _FakeAvatarStore]) -> None:
        client, _ = avatar_client

        assert client.get(AVATAR_PATH).status_code == 404

    def test_delete_removes_it_and_a_second_delete_is_404(
        self, avatar_client: tuple[TestClient, _FakeAvatarStore]
    ) -> None:
        client, _ = avatar_client
        client.put(AVATAR_PATH, files={"file": ("me.jpg", jpeg_with_exif(), "image/jpeg")})

        assert client.delete(AVATAR_PATH).status_code == 200
        assert client.get(AVATAR_PATH).status_code == 404
        assert client.delete(AVATAR_PATH).status_code == 404

    def test_an_undecodable_upload_is_415(self, avatar_client: tuple[TestClient, _FakeAvatarStore]) -> None:
        client, _ = avatar_client

        response = client.put(AVATAR_PATH, files={"file": ("card.bmp", bmp(), "image/bmp")})

        assert response.status_code == 415
        assert "JPEG" in response.json()["detail"]

    def test_an_oversized_upload_is_413(
        self, avatar_client: tuple[TestClient, _FakeAvatarStore], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`read_upload_within_limit` raises this from inside the service, so the route's
        job is to let it through rather than to catch it as an unsupported image."""
        client, _ = avatar_client

        async def oversized(db: Any, *, user_id: int, upload: UploadFile) -> str:
            raise HTTPException(status_code=413, detail="File too large. Maximum allowed size is 10 MB.")

        monkeypatch.setattr("src.app.api.v1.users.store_user_avatar", oversized)

        response = client.put(AVATAR_PATH, files={"file": ("huge.jpg", b"\xff\xd8\xff", "image/jpeg")})

        assert response.status_code == 413


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestAgainstPostgres:
    """The round trip with a real session, a real row and a real volume.

    What only a real database can settle: the columns actually land on the row, the
    `after_commit` hook actually unlinks (the mocked sessions above never commit, by
    design), and a replacement retires exactly the blob it replaced - the assertion the
    sweeper would otherwise be the only thing to notice.
    """

    @pytest.mark.asyncio
    async def test_the_round_trip_serves_back_the_bytes_that_were_stored(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        diver = create_user(db)

        digest = await store_user_avatar(async_db, user_id=diver.id, upload=_upload(jpeg_with_exif()))

        stored = await get_stored_avatar(async_db, user_id=diver.id)
        assert stored is not None
        assert stored.sha256 == digest

        data = await user_avatars.read_avatar_bytes(stored)
        assert hashlib.sha256(data).hexdigest() == digest
        assert data == (volume / stored.storage_key).read_bytes()
        assert _open(data).format == "WEBP"

    @pytest.mark.asyncio
    async def test_a_replacement_retires_the_blob_it_replaced(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        diver = create_user(db)
        await store_user_avatar(async_db, user_id=diver.id, upload=_upload(jpeg_with_exif()))
        first = await get_stored_avatar(async_db, user_id=diver.id)
        assert first is not None

        await store_user_avatar(async_db, user_id=diver.id, upload=_upload(png_with_alpha(), "me.png"))
        second = await get_stored_avatar(async_db, user_id=diver.id)

        assert second is not None and second.storage_key != first.storage_key
        assert blob_store.exists(second.storage_key)
        assert not blob_store.exists(first.storage_key), "the replaced avatar is still on the volume"

    @pytest.mark.asyncio
    async def test_delete_clears_the_columns_and_unlinks_the_file(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        diver = create_user(db)
        await store_user_avatar(async_db, user_id=diver.id, upload=_upload(jpeg_with_exif()))
        stored = await get_stored_avatar(async_db, user_id=diver.id)
        assert stored is not None

        assert await delete_user_avatar(async_db, user_id=diver.id) is True

        assert await get_stored_avatar(async_db, user_id=diver.id) is None
        assert not blob_store.exists(stored.storage_key)
        assert await delete_user_avatar(async_db, user_id=diver.id) is False

    @pytest.mark.asyncio
    async def test_an_account_that_never_had_one_reads_as_none(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)

        assert await get_stored_avatar(async_db, user_id=diver.id) is None
