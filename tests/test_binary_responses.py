"""Every route that serves stored bytes, and the uncacheable 404s beside them, send exactly
these headers - checked on the response the handler builds, before any middleware fills in its
defaults, so a header dropped from a handler cannot hide behind `SecurityHeadersMiddleware`."""

import io
import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import Request, Response

from src.app.api.v1 import certifications, checkin_links, dives, export, invitations, map_tiles, species, users
from src.app.schemas.certification import CertificationSide
from src.app.services.map_tiles import ServedMapTile
from src.app.schemas.user_picture import PictureKind
from src.app.services.user_pictures import StoredPictureFile

SHA = "a" * 64
ETAG = f'"{SHA}"'
CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"
DATA = b"\x89PNG-bytes"


def _request(if_none_match: str | None = None) -> Request:
    headers = [] if if_none_match is None else [(b"if-none-match", if_none_match.encode())]
    return Request({"type": "http", "method": "GET", "headers": headers})


def _headers(response: Response) -> dict[str, str]:
    return dict(response.headers)


def _served(cache_control: str, *, content_type: str = "image/png", disposition: str | None = None) -> dict[str, str]:
    expected = {
        "content-length": str(len(DATA)),
        "content-type": content_type,
        "x-content-type-options": "nosniff",
        "content-security-policy": CSP,
        "cache-control": cache_control,
        "etag": ETAG,
    }
    if disposition is not None:
        expected["content-disposition"] = disposition
    return expected


def _not_modified(cache_control: str) -> dict[str, str]:
    return {"etag": ETAG, "cache-control": cache_control}


@pytest.fixture
def dive_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dives, "_get_owned_dive", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(dives, "resolve_dive_file", AsyncMock(return_value=(2, None)))
    monkeypatch.setattr(dives, "get_dive_file_sha256", AsyncMock(return_value=SHA))
    monkeypatch.setattr(
        dives,
        "load_dive_file",
        AsyncMock(return_value=SimpleNamespace(data=DATA, content_type="image/png", original_filename="café.png")),
    )


class TestDiveFile:
    async def _read(self, if_none_match: str | None = None) -> Response:
        return await dives.read_dive_file(
            request=_request(if_none_match), uuid=uuid_pkg.uuid4(), fid=uuid_pkg.uuid4(), current_user={}, db=None
        )

    @pytest.mark.asyncio
    async def test_served(self, dive_file: None) -> None:
        disposition = "attachment; filename=\"cafe.png\"; filename*=UTF-8''caf%C3%A9.png"
        assert _headers(await self._read()) == _served("private, max-age=300", disposition=disposition)

    @pytest.mark.asyncio
    async def test_not_modified(self, dive_file: None) -> None:
        response = await self._read(ETAG)
        assert (response.status_code, _headers(response)) == (304, _not_modified("private, max-age=300"))


class TestDiveProfile:
    @pytest.mark.asyncio
    async def test_not_modified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dives, "_get_owned_dive", AsyncMock(return_value=SimpleNamespace(id=1)))
        monkeypatch.setattr(dives, "resolve_recording", AsyncMock(return_value=2))
        monkeypatch.setattr(dives, "get_profile_version", AsyncMock(return_value=SHA))
        response = await dives.read_dive_profile(
            request=_request(ETAG), uuid=uuid_pkg.uuid4(), rid=uuid_pkg.uuid4(), current_user={}, db=None
        )
        assert isinstance(response, Response)
        assert (response.status_code, _headers(response)) == (304, _not_modified("private, max-age=300"))


@pytest.fixture
def certification_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(certifications, "_get_owned_certification", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(certifications, "get_certification_file_sha256", AsyncMock(return_value=SHA))
    monkeypatch.setattr(
        certifications,
        "load_certification_file",
        AsyncMock(return_value=SimpleNamespace(data=DATA, content_type="image/png", original_filename="card.png")),
    )


class TestCertificationFile:
    async def _read(self, if_none_match: str | None = None) -> Response:
        return await certifications.read_certification_file(
            request=_request(if_none_match),
            uuid=uuid_pkg.uuid4(),
            side=CertificationSide.FRONT,
            current_user={},
            db=None,
        )

    @pytest.mark.asyncio
    async def test_served(self, certification_file: None) -> None:
        disposition = "attachment; filename=\"card.png\"; filename*=UTF-8''card.png"
        assert _headers(await self._read()) == _served("private, max-age=300", disposition=disposition)

    @pytest.mark.asyncio
    async def test_not_modified(self, certification_file: None) -> None:
        response = await self._read(ETAG)
        assert (response.status_code, _headers(response)) == (304, _not_modified("private, max-age=300"))


@pytest.fixture
def species_photo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(species, "get_stored_photo", AsyncMock(return_value=SimpleNamespace(sha256=SHA)))
    monkeypatch.setattr(species, "read_photo_bytes", AsyncMock(return_value=DATA))


class TestSpeciesPhoto:
    async def _read(self, if_none_match: str | None = None) -> Response:
        return await species.read_species_photo(request=_request(if_none_match), uuid=uuid_pkg.uuid4(), db=None)

    @pytest.mark.asyncio
    async def test_served(self, species_photo: None) -> None:
        assert _headers(await self._read()) == _served("public, max-age=300", content_type="image/webp")

    @pytest.mark.asyncio
    async def test_not_modified(self, species_photo: None) -> None:
        response = await self._read(ETAG)
        assert (response.status_code, _headers(response)) == (304, _not_modified("public, max-age=300"))


class TestUserPicture:
    STORED = StoredPictureFile(storage_key="k", sha256=SHA, content_type="image/png", filename="me.png")

    async def _read(self, monkeypatch: pytest.MonkeyPatch, if_none_match: str | None = None) -> Response:
        monkeypatch.setattr(users, "read_picture_bytes", AsyncMock(return_value=DATA))
        return await users._serve_picture(_request(if_none_match), self.STORED, kind=PictureKind.AVATAR)

    @pytest.mark.asyncio
    async def test_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        disposition = "attachment; filename=\"me.png\"; filename*=UTF-8''me.png"
        assert _headers(await self._read(monkeypatch)) == _served("private, max-age=300", disposition=disposition)

    @pytest.mark.asyncio
    async def test_not_modified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = await self._read(monkeypatch, ETAG)
        assert (response.status_code, _headers(response)) == (304, _not_modified("private, max-age=300"))


class TestCheckinPortrait:
    async def _read(self, monkeypatch: pytest.MonkeyPatch, if_none_match: str | None = None) -> Response:
        monkeypatch.setattr(checkin_links, "resolve_checkin_link", AsyncMock(return_value=SimpleNamespace(user_id=1)))
        monkeypatch.setattr(checkin_links, "get_rendition", AsyncMock(return_value=TestUserPicture.STORED))
        monkeypatch.setattr(checkin_links, "read_picture_bytes", AsyncMock(return_value=DATA))
        return await checkin_links.read_checkin_portrait(request=_request(if_none_match), token="t", db=None)

    @pytest.mark.asyncio
    async def test_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _headers(await self._read(monkeypatch)) == _served("private, no-store", disposition="inline")

    @pytest.mark.asyncio
    async def test_not_modified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = await self._read(monkeypatch, ETAG)
        assert (response.status_code, _headers(response)) == (304, _not_modified("private, no-store"))

    @pytest.mark.asyncio
    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(checkin_links, "resolve_checkin_link", AsyncMock(return_value=None))
        response = await checkin_links.read_checkin_portrait(request=_request(), token="t", db=None)
        assert (response.status_code, response.body, _headers(response)) == (
            404,
            b'{"detail":"Not found"}',
            {"content-length": "22", "content-type": "application/json", "cache-control": "private, no-store"},
        )


class TestCheckinCardFront:
    FRONT = SimpleNamespace(sha256=SHA, content_type="image/png", data=DATA)

    async def _read(self, monkeypatch: pytest.MonkeyPatch, if_none_match: str | None = None) -> Response:
        monkeypatch.setattr(checkin_links, "resolve_checkin_link", AsyncMock(return_value=SimpleNamespace(user_id=1)))
        monkeypatch.setattr(checkin_links, "find_card_front", AsyncMock(return_value=self.FRONT))
        monkeypatch.setattr(checkin_links, "load_card_front", AsyncMock(return_value=self.FRONT))
        return await checkin_links.read_checkin_card_front(
            request=_request(if_none_match), token="t", uuid=str(uuid_pkg.uuid4()), db=None
        )

    @pytest.mark.asyncio
    async def test_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _headers(await self._read(monkeypatch)) == _served("private, no-store", disposition="inline")

    @pytest.mark.asyncio
    async def test_not_modified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = await self._read(monkeypatch, ETAG)
        assert (response.status_code, _headers(response)) == (304, _not_modified("private, no-store"))


class TestMapTile:
    async def _read(self, monkeypatch: pytest.MonkeyPatch, data: bytes | None) -> Response:
        monkeypatch.setattr(map_tiles, "find_or_draw", AsyncMock(return_value=ServedMapTile(sha256=SHA, data=data)))
        return await map_tiles.read_map_tile(
            request=_request(), theme="light", z=1, x=0, y=0, current_user={"id": 1}, db=None
        )

    @pytest.mark.asyncio
    async def test_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        disposition = "attachment; filename=\"map-tile.webp\"; filename*=UTF-8''map-tile.webp"
        assert _headers(await self._read(monkeypatch, DATA)) == _served(
            "private, max-age=300", content_type="image/webp", disposition=disposition
        )

    @pytest.mark.asyncio
    async def test_not_modified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = await self._read(monkeypatch, None)
        assert (response.status_code, _headers(response)) == (304, _not_modified("private, max-age=300"))


class TestExportDownload:
    def test_served(self) -> None:
        response = export._download(io.BytesIO(DATA), filename="logbook.json", media_type="application/json")
        assert _headers(response) == {
            "content-type": "application/json",
            "content-disposition": 'attachment; filename="logbook.json"',
            "content-length": str(len(DATA)),
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
            "content-security-policy": CSP,
        }


class TestInviteLinkNotFound:
    @pytest.mark.asyncio
    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(invitations, "registration_is_invite_only", lambda: False)
        response: Any = await invitations.resolve_invite_link(token="t", response=Response(), db=None)
        assert (response.status_code, response.body, _headers(response)) == (
            404,
            b'{"detail":"This invite link is not active."}',
            {"content-length": "44", "content-type": "application/json", "cache-control": "private, no-store"},
        )
