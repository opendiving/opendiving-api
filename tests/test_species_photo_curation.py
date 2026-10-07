"""Species photo curation: the size floor, the outcome an attempt reports, the operator's
routes, and the backfill's respect for what a human decided.

**The property most worth pinning is the one that fails silently**: a re-fetch reading a
provider outage as the rule declining, and clearing a good photo. Every "unavailable" case here
is a way that used to be indistinguishable from "declined".
"""

import inspect
import io
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import anyio
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import admin
from src.app.core.config import settings
from src.app.core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from src.app.core.setup import create_application
from src.app.schemas.species import AdminSpeciesFilter, AdminSpeciesPhotoPin, PhotoCuration
from src.app.services import blob_store, species_photos, species_service
from src.app.services.species_photos import ImageCandidate, PhotoCredit, normalize_file_title
from src.app.services.species_service import PhotoAttempt, PhotoOutcome
from tests.conftest import db_available
from tests.helpers.generators import create_species
from tests.helpers.images import jpeg_with_exif, plain_png

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
_BYTE_HOSTS = frozenset({"thumb.wikimedia.org", "upload.wikimedia.org"})
_ANY_UUID = "01a11779-b240-78e5-b3b6-983bccb45606"


@pytest.fixture(autouse=True)
def unthrottled() -> Generator[None]:
    with patch("src.app.services.species_service.enforce_rate_limit", new_callable=AsyncMock):
        yield


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


def _entity(*, images: list[str], taxon_name: str = "Amphiprion ocellaris", category: str | None = None) -> Any:
    return species_service._WikidataEntity(
        qid="Q1126155",
        aphia_id=278400,
        scientific_name=taxon_name,
        label=None,
        aliases=(),
        rank="Species",
        images=tuple(ImageCandidate(file=title, rank="normal") for title in images),
        commons_category=category,
    )


def _imageinfo(title: str = "Some fish.jpg", *, missing: bool = False) -> dict[str, Any]:
    page: dict[str, Any] = {"title": f"File:{title}"}
    if missing:
        page["missing"] = True
    else:
        page["imageinfo"] = [
            {
                "url": "https://upload.wikimedia.org/wikipedia/commons/e/ef/Some_fish.jpg?utm_content=original",
                "thumburl": "https://thumb.wikimedia.org/wikipedia/commons/thumb/e/ef/Some_fish.jpg/500px-Some_fish.jpg",
                "thumbwidth": 500,
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Some_fish.jpg",
                "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}},
            }
        ]
    return {"query": {"pages": [page]}}


class _Wikimedia:
    """Wikidata, WoRMS-free, and Commons at the transport layer, routed by host and action."""

    def __init__(self, handler: Callable[[httpx.Request], Any]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler
        self._patcher: Any = None

    async def _record(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._handler(request)
        if inspect.isawaitable(response):
            response = await response
        assert isinstance(response, httpx.Response)
        return response

    def __enter__(self) -> _Wikimedia:
        def build(**kwargs: Any) -> httpx.AsyncClient:
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(self._record), **kwargs)

        self._patcher = patch("src.app.services.species_service.httpx.AsyncClient", side_effect=build)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._patcher.stop()


def _commons(
    *,
    imageinfo: dict[str, Any] | None = None,
    image_bytes: bytes = b"",
    commons_status: int = 200,
    wikidata: dict[str, Any] | None = None,
) -> _Wikimedia:
    def handle(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host in _BYTE_HOSTS:
            return httpx.Response(200, content=image_bytes, headers={"content-type": "image/png"})
        if host == "commons.wikimedia.org":
            return httpx.Response(commons_status, json=imageinfo or {"query": {"pages": []}})
        if "wikidata" in host:
            return httpx.Response(200, json=wikidata or {"query": {"search": []}})
        raise AssertionError(f"unexpected request to {request.url}")

    return _Wikimedia(handle)


# -------------- titles an admin pastes --------------


class TestNormalizingAPastedTitle:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Seriphus politus 28977555.jpg", "Seriphus politus 28977555.jpg"),
            ("File:Seriphus_politus_28977555.jpg", "Seriphus politus 28977555.jpg"),
            ("  file:seriphus politus.JPG ", "Seriphus politus.JPG"),
            (
                "https://commons.wikimedia.org/wiki/File:Seriphus_politus_%28Ayres%29.jpg?uselang=en#/media/x",
                "Seriphus politus (Ayres).jpg",
            ),
        ],
    )
    def test_both_forms_reduce_to_the_spelling_p18_uses(self, raw: str, expected: str) -> None:
        """One spelling is what lets the stored `photo_file`, a candidate's `is_current` and the
        de-duplication agree - underscores are the URL's spelling of a space."""
        assert normalize_file_title(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not a file",
            "Range map.svg",
            "https://example.org/wiki/File:Fish.jpg",
            "https://upload.wikimedia.org/wikipedia/commons/e/ef/Fish.jpg",
            "Fish.jpg|Other fish.jpg",
            "Category:Fish.jpg",
            "Fish\x00.jpg",
        ],
    )
    def test_anything_else_is_refused(self, raw: str) -> None:
        """`|` especially: it is the separator a multi-title call joins on, so one admitted here
        would ask Commons about two files."""
        assert normalize_file_title(raw) is None


# -------------- the floor --------------


class TestTheFloor:
    @pytest.mark.asyncio
    async def test_bytes_under_the_floor_are_refused_as_their_own_error(self) -> None:
        with pytest.raises(species_photos.PhotoTooNarrowError):
            await species_photos.process_photo(plain_png(size=(499, 300)), minimum_width=500)

    @pytest.mark.asyncio
    async def test_the_pin_passes_no_floor(self) -> None:
        normalized = await species_photos.process_photo(plain_png(size=(282, 100)), minimum_width=None)

        assert (normalized.width, normalized.height) == (282, 100)

    @pytest.mark.asyncio
    async def test_the_dimensions_are_the_stored_bytes_after_the_transpose(self) -> None:
        """An EXIF-rotated portrait is served upright, so its stored width is the header's
        height - and that is the width the floor and the grid both need."""
        raw = jpeg_with_exif(orientation=6)
        with Image.open(io.BytesIO(raw)) as image:
            header_width, header_height = image.size

        normalized = await species_photos.process_photo(raw, minimum_width=None)

        assert (normalized.width, normalized.height) == (header_height, header_width)
        with Image.open(io.BytesIO(normalized.data)) as stored:
            assert stored.size == (normalized.width, normalized.height)


# -------------- what an attempt reports --------------


class TestTheOutcome:
    """`declined` is the rule's own answer; `unavailable` is every way of never really asking."""

    async def _fetch(self, *, entity: Any, synonyms: list[int] | None = None, **commons: Any) -> PhotoAttempt:
        with _commons(**commons):
            return await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris", aphia_id=278400, entity=entity, synonym_aphia_ids=synonyms
            )

    @pytest.mark.asyncio
    async def test_no_item_and_no_synonyms_is_declined(self) -> None:
        assert (await self._fetch(entity=None, synonyms=[])).outcome is PhotoOutcome.DECLINED

    @pytest.mark.asyncio
    async def test_a_refused_choice_is_declined(self) -> None:
        entity = _entity(taxon_name="Triaenodon obesus", images=["Silvertip.jpg", "Reef shark.jpg"])

        assert (await self._fetch(entity=entity)).outcome is PhotoOutcome.DECLINED

    @pytest.mark.asyncio
    async def test_a_file_commons_does_not_have_is_declined(self) -> None:
        attempt = await self._fetch(
            entity=_entity(images=["Amphiprion ocellaris.jpg"]), imageinfo=_imageinfo(missing=True)
        )

        assert attempt.outcome is PhotoOutcome.DECLINED

    @pytest.mark.asyncio
    async def test_a_synonym_list_that_never_arrived_is_unavailable(self) -> None:
        """The zebra shark's case: the accepted item has no image, so the synonyms are the only
        route to a photo, and not having asked them is not an answer."""
        assert (await self._fetch(entity=None, synonyms=None)).outcome is PhotoOutcome.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_a_failed_synonym_search_is_unavailable(self) -> None:
        refusal = {"error": {"code": "readonly"}}

        attempt = await self._fetch(entity=None, synonyms=[220032], wikidata=refusal)

        assert attempt.outcome is PhotoOutcome.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_a_synonym_search_that_found_nothing_is_declined(self) -> None:
        attempt = await self._fetch(entity=None, synonyms=[220032], wikidata={"query": {"search": []}})

        assert attempt.outcome is PhotoOutcome.DECLINED

    @pytest.mark.asyncio
    async def test_commons_failing_is_unavailable(self) -> None:
        attempt = await self._fetch(entity=_entity(images=["Amphiprion ocellaris.jpg"]), commons_status=503)

        assert attempt.outcome is PhotoOutcome.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_bytes_that_will_not_decode_are_unavailable(self) -> None:
        """A 200 carrying an HTML page says nothing about the file, so a re-fetch must not
        clear a good photo over one."""
        attempt = await self._fetch(
            entity=_entity(images=["Amphiprion ocellaris.jpg"]), imageinfo=_imageinfo(), image_bytes=b"<html>"
        )

        assert attempt.outcome is PhotoOutcome.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_an_entity_lookup_that_failed_is_unavailable_rather_than_no_item(self) -> None:
        with (
            patch.object(species_service, "_wikidata_by_aphia_id", AsyncMock(return_value=(None, False))),
            patch.object(species_service, "_worms_synonyms", AsyncMock(return_value=[])),
        ):
            attempt = await species_service.fetch_photo_for_species(scientific_name="X y", aphia_id=1)

        assert attempt.outcome is PhotoOutcome.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_no_item_at_all_is_declined(self) -> None:
        with (
            patch.object(species_service, "_wikidata_by_aphia_id", AsyncMock(return_value=(None, True))),
            patch.object(species_service, "_worms_synonyms", AsyncMock(return_value=[])),
        ):
            attempt = await species_service.fetch_photo_for_species(scientific_name="X y", aphia_id=1)

        assert attempt.outcome is PhotoOutcome.DECLINED
        assert attempt.completed


class TestTheNamedFile:
    """`fetch_named_photo` has no catch-all, so the pin can tell its failures apart."""

    @pytest.mark.asyncio
    async def test_a_missing_file_raises(self) -> None:
        with _commons(imageinfo=_imageinfo(missing=True)), pytest.raises(species_service.CommonsFileMissingError):
            await species_service.fetch_named_photo("Nope.jpg", minimum_width=None)

    @pytest.mark.asyncio
    async def test_undecodable_bytes_raise(self) -> None:
        with (
            _commons(imageinfo=_imageinfo(), image_bytes=b"<html>"),
            pytest.raises(species_photos.UnsupportedPhotoImageError),
        ):
            await species_service.fetch_named_photo("Some fish.jpg", minimum_width=None)

    @pytest.mark.asyncio
    async def test_a_narrow_file_is_kept_when_no_floor_is_asked_for(self) -> None:
        with _commons(imageinfo=_imageinfo(), image_bytes=plain_png(size=(282, 100))):
            attempt = await species_service.fetch_named_photo("Some fish.jpg", minimum_width=None)

        assert attempt.photo is not None
        assert attempt.photo.width == 282


class TestThePinsBudget:
    @pytest.mark.asyncio
    async def test_a_pin_that_outlasts_the_photo_budget_is_timed_out(self) -> None:
        async def hangs(*_args: Any, **_kwargs: Any) -> PhotoAttempt:
            await anyio.sleep(3600)
            raise AssertionError("unreachable")

        with (
            patch.object(species_service, "fetch_named_photo", hangs),
            patch.object(species_service, "_PHOTO_BUDGET_SECONDS", 0.05),
        ):
            attempt = await species_service.fetch_pinned_photo("Fish.jpg")

        assert (attempt.completed, attempt.photo) == (False, None)


# -------------- the picker --------------


def _picker(
    *, members: list[str], infos: dict[str, int], bytes_type: str = "image/jpeg", file_categories: tuple[str, ...] = ()
) -> _Wikimedia:
    """Commons answering the picker's three kinds of call, and serving preview bytes."""

    def handle(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        params = request.url.params
        if host in _BYTE_HOSTS:
            return httpx.Response(200, content=b"\xff\xd8preview", headers={"content-type": bytes_type})
        if params.get("list") == "categorymembers":
            return httpx.Response(
                200, json={"query": {"categorymembers": [{"ns": 6, "title": f"File:{m}"} for m in members]}}
            )
        if params.get("prop") == "categories":
            return httpx.Response(
                200,
                json={"query": {"pages": [{"categories": [{"title": f"Category:{c}"} for c in file_categories]}]}},
            )
        if params.get("prop") == "imageinfo":
            pages = [
                {
                    "title": f"File:{title.split(':', 1)[1]}",
                    "imageinfo": [
                        {
                            "width": width,
                            "height": 100,
                            "thumburl": f"https://upload.wikimedia.org/x/{width}.jpg",
                            "descriptionurl": "https://commons.wikimedia.org/wiki/File:X.jpg",
                            "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}},
                        }
                    ],
                }
                for title in params["titles"].split("|")
                if (width := infos.get(title.split(":", 1)[1])) is not None
            ]
            return httpx.Response(200, json={"query": {"pages": pages}})
        raise AssertionError(f"unexpected request to {request.url}")

    return _Wikimedia(handle)


class TestThePicker:
    @pytest.mark.asyncio
    async def test_p18_then_the_category_de_duplicated_with_previews_inline(self) -> None:
        entity = _entity(images=["Seriphus politus Mspc094.jpg"], category="Seriphus politus")
        members = ["Seriphus_politus_Mspc094.jpg", "Seriphus politus 28977555.jpg", "Range.svg"]
        infos = {"Seriphus politus Mspc094.jpg": 282, "Seriphus politus 28977555.jpg": 1024}

        with (
            patch.object(species_service, "_wikidata_entities", AsyncMock(return_value=([entity], True))),
            _picker(members=members, infos=infos) as wikimedia,
        ):
            found = await species_service.photo_candidates(
                wikidata_qid="Q1796044", photo_file="Seriphus politus Mspc094.jpg", genus="Seriphus"
            )

        assert found.category == "Seriphus politus"
        assert [(c.file, c.width, c.is_current) for c in found.candidates] == [
            ("Seriphus politus Mspc094.jpg", 282, True),
            ("Seriphus politus 28977555.jpg", 1024, False),
        ]
        assert all(c.preview is not None and c.preview.startswith("data:image/jpeg;base64,") for c in found.candidates)
        assert found.candidates[0].credit.license_name == "CC BY 4.0"
        imageinfo = next(r for r in wikimedia.requests if r.url.params.get("prop") == "imageinfo")
        assert imageinfo.url.params["iiurlwidth"] == "250"

    @pytest.mark.asyncio
    async def test_without_p373_the_stored_files_category_naming_the_genus_is_used(self) -> None:
        with (
            patch.object(species_service, "_wikidata_entities", AsyncMock(return_value=([], True))),
            _picker(
                members=["Seriphus politus 1.jpg"],
                infos={"Seriphus politus 1.jpg": 600},
                file_categories=("Fish of California", "Seriphus politus"),
            ) as wikimedia,
        ):
            found = await species_service.photo_candidates(
                wikidata_qid="Q1", photo_file="Seriphus politus Mspc094.jpg", genus="Seriphus"
            )

        assert found.category == "Seriphus politus"
        listed = next(r for r in wikimedia.requests if r.url.params.get("list") == "categorymembers")
        assert listed.url.params["cmtitle"] == "Category:Seriphus politus"

    @pytest.mark.asyncio
    async def test_the_list_is_capped(self) -> None:
        members = [f"Fish {n}.jpg" for n in range(40)]
        entity = _entity(images=[], category="Fish")

        with (
            patch.object(species_service, "_wikidata_entities", AsyncMock(return_value=([entity], True))),
            _picker(members=members, infos=dict.fromkeys(members, 600)),
        ):
            found = await species_service.photo_candidates(wikidata_qid="Q1", photo_file=None, genus=None)

        assert len(found.candidates) == species_service._CANDIDATE_LIMIT

    @pytest.mark.asyncio
    async def test_a_preview_that_is_not_a_raster_image_is_left_out(self) -> None:
        entity = _entity(images=["Fish.jpg"])

        with (
            patch.object(species_service, "_wikidata_entities", AsyncMock(return_value=([entity], True))),
            _picker(members=[], infos={"Fish.jpg": 600}, bytes_type="text/html"),
        ):
            found = await species_service.photo_candidates(wikidata_qid="Q1", photo_file=None, genus=None)

        assert [c.preview for c in found.candidates] == [None]

    @pytest.mark.asyncio
    async def test_nothing_to_go_on_asks_nobody(self) -> None:
        with _picker(members=[], infos={}) as wikimedia:
            found = await species_service.photo_candidates(wikidata_qid=None, photo_file=None, genus=None)

        assert (found.category, found.candidates, wikimedia.requests) == (None, [], [])

    @pytest.mark.asyncio
    async def test_previews_claim_no_commons_slot_while_the_rules_fetch_does(self) -> None:
        """A dialog of two dozen previews would otherwise spend a fifth of the instance's
        Commons minute, and hand a diver's concurrent resolve a spent counter."""
        claim = AsyncMock(return_value=True)
        entity = _entity(images=["Fish.jpg", "Other fish.jpg"])

        with (
            patch.object(species_service, "_claim_provider_slot", claim),
            patch.object(species_service, "_wikidata_entities", AsyncMock(return_value=([entity], True))),
            _picker(members=[], infos={"Fish.jpg": 600, "Other fish.jpg": 600}),
        ):
            await species_service.photo_candidates(wikidata_qid="Q1", photo_file=None, genus=None)
            metadata_claims = claim.await_count
            await species_service._fetch_photo_bytes("https://upload.wikimedia.org/x/600.jpg")

        assert metadata_claims == 1
        assert claim.await_count == metadata_claims + 1

    @pytest.mark.asyncio
    async def test_media_fetches_keep_to_two_at_a_time(self) -> None:
        """Wikimedia's media rules: a total concurrency of at most 2."""
        in_flight = 0
        peak = 0

        async def handle(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await anyio.sleep(0.02)
            in_flight -= 1
            return httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})

        with _Wikimedia(handle):
            async with anyio.create_task_group() as tasks:
                for n in range(6):
                    tasks.start_soon(species_service._fetch_photo_bytes, f"https://upload.wikimedia.org/{n}.jpg")

        assert peak == 2


# -------------- the gate --------------


@pytest.fixture(scope="module")
def admin_app() -> Any:
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


class TestTheGate:
    # A fixed uuid: parametrize ids are compared across xdist workers, so a generated one fails
    # collection.
    _ROUTES = [
        ("GET", "/api/v1/admin/species"),
        ("GET", f"/api/v1/admin/species/{_ANY_UUID}/photo-candidates"),
        ("PUT", f"/api/v1/admin/species/{_ANY_UUID}/photo"),
        ("DELETE", f"/api/v1/admin/species/{_ANY_UUID}/photo"),
        ("POST", f"/api/v1/admin/species/{_ANY_UUID}/photo/refetch"),
    ]

    @pytest.mark.parametrize(("method", "path"), _ROUTES)
    def test_signed_out_is_a_401(self, admin_app: Any, method: str, path: str) -> None:
        with TestClient(admin_app) as client:
            assert client.request(method, path, json={"file": "Fish.jpg"}).status_code == 401

    @pytest.mark.parametrize(("method", "path"), _ROUTES)
    def test_a_member_is_a_403(self, admin_app: Any, method: str, path: str) -> None:
        admin_app.dependency_overrides[get_current_user] = lambda: {"id": 1, "is_superuser": False}
        try:
            with TestClient(admin_app) as client:
                response = client.request(method, path, json={"file": "Fish.jpg"})
        finally:
            admin_app.dependency_overrides = {}

        assert response.status_code == 403

    def test_an_unknown_chip_is_a_422(self, admin_app: Any) -> None:
        admin_app.dependency_overrides[get_current_user] = lambda: {"id": 1, "is_superuser": True}
        try:
            with TestClient(admin_app) as client:
                response = client.get("/api/v1/admin/species", params={"filter": "blurry"})
        finally:
            admin_app.dependency_overrides = {}

        assert response.status_code == 422


# -------------- the routes, against Postgres --------------


def _photo(data: bytes = b"webp", *, width: int = 600, file: str = "Fish.jpg") -> species_photos.FetchedPhoto:
    return species_photos.fetched_photo(
        photo=species_photos.NormalizedPhoto(data=data, width=width, height=400),
        file=file,
        credit=PhotoCredit(author="A. Diver", license_name="CC BY 4.0", license_url=None, source_url=None),
    )


async def _with_photo(db: Session, async_db: AsyncSession, data: bytes = b"old", **overrides: Any) -> Any:
    """A catalog row holding a stored photo, written the way the rule writes one, then curated
    as `photo_curation` says - the rule's writer would skip a row curated first."""
    curation = overrides.pop("photo_curation", None)
    species = create_species(db, **overrides)
    await species_photos.save_photo_attempt(async_db, species_id=species.id, photo=_photo(data))
    if curation is not None:
        db.query(type(species)).filter_by(id=species.id).update({"photo_curation": curation})
        db.commit()
    db.refresh(species)
    return species


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheCatalogList:
    @pytest.mark.asyncio
    async def test_newest_first_searchable_and_filtered(self, db: Session, async_db: AsyncSession) -> None:
        tag = uuid7().hex[-10:]
        older = create_species(db, scientific_name=f"zzfixture-{tag}-older")
        hidden = create_species(db, scientific_name=f"zzfixture-{tag}-hidden", photo_curation=PhotoCuration.HIDDEN)
        narrow = create_species(
            db,
            scientific_name=f"zzfixture-{tag}-narrow",
            photo_curation=PhotoCuration.PINNED,
            photo_storage_key=f"species-photos/aa/{uuid7()}",
            photo_sha256="a" * 64,
            photo_width=282,
            photo_height=100,
        )

        listed = await admin.read_admin_species(db=async_db, search=tag)
        names = [row["scientific_name"] for row in listed["data"]]

        assert names == [narrow.scientific_name, hidden.scientific_name, older.scientific_name]
        assert listed["total_count"] == 3
        assert listed["data"][0]["photo_curation"] == "pinned"
        assert listed["data"][0]["photo_width"] == 282
        assert "photo_storage_key" not in listed["data"][0]

        async def only(chip: AdminSpeciesFilter) -> list[str]:
            result = await admin.read_admin_species(db=async_db, search=tag, chip=chip)
            return [row["scientific_name"] for row in result["data"]]

        assert await only(AdminSpeciesFilter.HIDDEN) == [hidden.scientific_name]
        assert await only(AdminSpeciesFilter.PINNED) == [narrow.scientific_name]
        assert await only(AdminSpeciesFilter.NARROW) == [narrow.scientific_name]
        assert await only(AdminSpeciesFilter.WITH_PHOTO) == [narrow.scientific_name]
        assert await only(AdminSpeciesFilter.WITHOUT_PHOTO) == [hidden.scientific_name, older.scientific_name]


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestHidingPinningAndRefetching:
    @pytest.mark.asyncio
    async def test_hiding_clears_the_photo_and_its_bytes(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        species = await _with_photo(db, async_db)
        old_key = species.photo_storage_key

        row = await admin.hide_species_photo(uuid=species.uuid, db=async_db)

        assert row.photo_curation == PhotoCuration.HIDDEN
        assert (row.photo_sha256, row.photo_file, row.photo_author, row.photo_width) == (None, None, None, None)
        assert row.photo_fetched_at is not None
        assert not (volume / old_key).exists()
        assert await species_photos.get_stored_photo(async_db, species_uuid=species.uuid) is None

    @pytest.mark.asyncio
    async def test_pinning_stores_the_file_and_marks_it(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        species = await _with_photo(db, async_db)
        old_key = species.photo_storage_key
        pinned = AsyncMock(return_value=PhotoAttempt.found(_photo(b"new", width=282, file="Seriphus politus.jpg")))

        with patch.object(species_service, "fetch_pinned_photo", pinned):
            row = await admin.pin_species_photo(
                uuid=species.uuid,
                body=AdminSpeciesPhotoPin(file="https://commons.wikimedia.org/wiki/File:Seriphus_politus.jpg"),
                db=async_db,
            )

        pinned.assert_awaited_once_with("Seriphus politus.jpg")
        assert (row.photo_curation, row.photo_file, row.photo_width) == ("pinned", "Seriphus politus.jpg", 282)
        assert not (volume / old_key).exists()
        db.refresh(species)
        assert (volume / species.photo_storage_key).read_bytes() == b"new"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            species_service.CommonsFileMissingError("Fish.jpg"),
            species_photos.UnsupportedPhotoImageError("The fetched bytes are not a decodable image."),
        ],
    )
    async def test_a_pin_commons_refuses_is_a_422_and_changes_nothing(
        self, db: Session, async_db: AsyncSession, volume: Path, failure: Exception
    ) -> None:
        species = await _with_photo(db, async_db)

        with (
            patch.object(species_service, "fetch_pinned_photo", AsyncMock(side_effect=failure)),
            pytest.raises(UnprocessableEntityException),
        ):
            await admin.pin_species_photo(uuid=species.uuid, body=AdminSpeciesPhotoPin(file="Fish.jpg"), db=async_db)

        sha = species.photo_sha256
        db.refresh(species)
        assert (species.photo_sha256, species.photo_curation) == (sha, None)

    @pytest.mark.asyncio
    async def test_a_pin_of_something_that_is_not_a_file_is_a_422_before_anything_is_asked(
        self, async_db: AsyncSession
    ) -> None:
        pinned = AsyncMock()
        with (
            patch.object(species_service, "fetch_pinned_photo", pinned),
            pytest.raises(UnprocessableEntityException),
        ):
            await admin.pin_species_photo(uuid=uuid7(), body=AdminSpeciesPhotoPin(file="not a file"), db=async_db)

        pinned.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_candidates_route_reads_the_row_and_maps_the_credit(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        species = create_species(db, wikidata_qid="Q1796044", photo_file="Fish.jpg", genus="Seriphus")
        found = species_service.PhotoCandidates(
            category="Seriphus politus",
            candidates=[
                species_service.PhotoCandidate(
                    file="Fish.jpg",
                    width=282,
                    height=100,
                    credit=PhotoCredit(author="A. Diver", license_name="CC0", license_url=None, source_url=None),
                    preview="data:image/jpeg;base64,AA==",
                    is_current=True,
                )
            ],
        )
        candidates = AsyncMock(return_value=found)

        with patch.object(species_service, "photo_candidates", candidates):
            answer = await admin.read_species_photo_candidates(uuid=species.uuid, db=async_db)

        candidates.assert_awaited_once_with(wikidata_qid="Q1796044", photo_file="Fish.jpg", genus="Seriphus")
        assert answer.category == "Seriphus politus"
        assert answer.candidates[0].model_dump() == {
            "file": "Fish.jpg",
            "width": 282,
            "height": 100,
            "license": "CC0",
            "author": "A. Diver",
            "source_url": None,
            "preview": "data:image/jpeg;base64,AA==",
            "is_current": True,
        }

    @pytest.mark.asyncio
    async def test_an_unknown_species_is_a_404(self, async_db: AsyncSession) -> None:
        with pytest.raises(NotFoundException):
            await admin.hide_species_photo(uuid=uuid7(), db=async_db)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("attempt", [PhotoAttempt.unavailable(), PhotoAttempt.timed_out()])
    async def test_a_pin_or_refetch_commons_could_not_answer_is_a_503_and_changes_nothing(
        self, db: Session, async_db: AsyncSession, volume: Path, attempt: PhotoAttempt
    ) -> None:
        """Byte-identical afterwards, blob included: an outage must never read as the rule
        saying "no photo"."""
        species = await _with_photo(db, async_db, photo_curation=PhotoCuration.PINNED)
        before = (species.photo_storage_key, species.photo_sha256, species.photo_curation, species.photo_fetched_at)

        with (
            patch.object(species_service, "fetch_pinned_photo", AsyncMock(return_value=attempt)),
            patch.object(species_service, "fetch_photo_for_species", AsyncMock(return_value=attempt)),
        ):
            for call in (
                admin.pin_species_photo(uuid=species.uuid, body=AdminSpeciesPhotoPin(file="Fish.jpg"), db=async_db),
                admin.refetch_species_photo(uuid=species.uuid, db=async_db),
            ):
                with pytest.raises(HTTPException) as raised:
                    await call
                assert raised.value.status_code == 503

        db.refresh(species)
        assert (
            species.photo_storage_key,
            species.photo_sha256,
            species.photo_curation,
            species.photo_fetched_at,
        ) == before
        assert (volume / species.photo_storage_key).read_bytes() == b"old"

    @pytest.mark.asyncio
    async def test_a_refetch_the_rule_declines_clears_a_pinned_photo(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        species = await _with_photo(db, async_db, photo_curation=PhotoCuration.PINNED)
        old_key = species.photo_storage_key

        with patch.object(species_service, "fetch_photo_for_species", AsyncMock(return_value=PhotoAttempt.declined())):
            row = await admin.refetch_species_photo(uuid=species.uuid, db=async_db)

        assert (row.photo_curation, row.photo_sha256) == (None, None)
        assert not (volume / old_key).exists()

    @pytest.mark.asyncio
    async def test_a_refetch_that_finds_a_photo_hands_a_hidden_row_back_to_the_rule(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        species = create_species(db, photo_curation=PhotoCuration.HIDDEN, photo_fetched_at=_NOW)

        with patch.object(
            species_service, "fetch_photo_for_species", AsyncMock(return_value=PhotoAttempt.found(_photo(b"rule")))
        ):
            row = await admin.refetch_species_photo(uuid=species.uuid, db=async_db)

        assert row.photo_curation is None
        assert row.photo_file == "Fish.jpg"
        db.refresh(species)
        assert species.photo_storage_key is not None
        assert (volume / species.photo_storage_key).read_bytes() == b"rule"


# -------------- the rule leaves a curated row alone --------------


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheRuleLeavesCuratedRowsAlone:
    @staticmethod
    def _snapshot(species: Any) -> tuple[Any, ...]:
        return (
            species.photo_storage_key,
            species.photo_sha256,
            species.photo_file,
            species.photo_curation,
            species.photo_fetched_at,
        )

    @pytest.mark.asyncio
    async def test_force_skips_hidden_and_pinned_rows_byte_for_byte(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        from src.scripts import backfill_species_photos as backfill

        pinned = await _with_photo(db, async_db, photo_curation=PhotoCuration.PINNED)
        hidden = create_species(db, photo_curation=PhotoCuration.HIDDEN, photo_fetched_at=_NOW)
        before = {pinned.id: self._snapshot(pinned), hidden.id: self._snapshot(hidden)}

        candidates = await backfill._candidates(async_db, limit=None, force=True)
        assert {pinned.id, hidden.id}.isdisjoint({candidate.id for candidate in candidates})

        for species in (pinned, hidden):
            db.refresh(species)
            assert self._snapshot(species) == before[species.id]
        assert (volume / pinned.photo_storage_key).read_bytes() == b"old"

    @pytest.mark.asyncio
    async def test_a_row_curated_after_selection_survives_the_write(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        """The walk selects once and runs for an hour per thousand species, so the guard has to
        be on the write too - and a write that matched nothing must not unlink the bytes the
        freshly pinned row names."""
        pinned = await _with_photo(db, async_db, photo_curation=PhotoCuration.PINNED)
        hidden = create_species(db, photo_curation=PhotoCuration.HIDDEN, photo_fetched_at=_NOW)
        before = {pinned.id: self._snapshot(pinned), hidden.id: self._snapshot(hidden)}

        await species_photos.save_photo_attempt(async_db, species_id=pinned.id, photo=_photo(b"rule"))
        await species_photos.save_photo_attempt(async_db, species_id=pinned.id, photo=None)
        await species_photos.save_photo_attempt(async_db, species_id=hidden.id, photo=_photo(b"rule"))

        for species in (pinned, hidden):
            db.refresh(species)
            assert self._snapshot(species) == before[species.id]
        assert (volume / pinned.photo_storage_key).read_bytes() == b"old"


# -------------- the size re-check --------------


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheSizeRecheck:
    async def _stored(self, db: Session, async_db: AsyncSession, *, width: int, **overrides: Any) -> Any:
        species = create_species(db, photo_fetched_at=_NOW, **overrides)
        data = await species_photos.process_photo(plain_png(size=(width, 200)), minimum_width=None)
        photo = species_photos.fetched_photo(photo=data, file="Fish.jpg", credit=PhotoCredit(None, None, None, None))
        await species_photos.write_curated_photo(
            async_db, species_id=species.id, photo=photo, curation=overrides.get("photo_curation")
        )
        # As a row stored before the dimensions existed.
        db.query(type(species)).filter_by(id=species.id).update({"photo_width": None, "photo_height": None})
        db.commit()
        db.refresh(species)
        return species

    @pytest.mark.asyncio
    async def test_narrow_photos_drop_unless_pinned_and_every_kept_one_is_measured(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        from src.scripts import backfill_species_photos as backfill

        narrow = await self._stored(db, async_db, width=320)
        wide = await self._stored(db, async_db, width=600)
        pinned_narrow = await self._stored(db, async_db, width=320, photo_curation=PhotoCuration.PINNED)
        pinned_missing = await self._stored(db, async_db, width=600, photo_curation=PhotoCuration.PINNED)
        (volume / pinned_missing.photo_storage_key).unlink()
        ours = {narrow.id, wide.id, pinned_narrow.id, pinned_missing.id}
        narrow_key = narrow.photo_storage_key

        # The suite's database holds every other module's rows too, some naming keys with no
        # bytes behind them; the pass is pointed at this test's rows alone.
        stored_photos = backfill._stored_photos

        async def only_ours(session: AsyncSession) -> list[Any]:
            return [photo for photo in await stored_photos(session) if photo.id in ours]

        with patch.object(backfill, "_stored_photos", only_ours):
            dry = await backfill.recheck_photo_size(async_db, dry_run=True)
            db.refresh(narrow)
            assert narrow.photo_storage_key == narrow_key and narrow.photo_width is None
            assert dry.dropped == 2

            done = await backfill.recheck_photo_size(async_db)
            again = await backfill.recheck_photo_size(async_db, dry_run=True)

        assert (done.dropped, done.measured) == (2, 2)
        assert again.dropped == 0
        for species in (narrow, wide, pinned_narrow, pinned_missing):
            db.refresh(species)
        assert (narrow.photo_storage_key, narrow.photo_fetched_at is not None) == (None, True)
        assert not (volume / narrow_key).exists()
        assert (wide.photo_width, wide.photo_height) == (600, 200)
        assert (pinned_narrow.photo_width, pinned_narrow.photo_curation) == (320, PhotoCuration.PINNED)
        assert (pinned_missing.photo_storage_key, pinned_missing.photo_curation) == (None, None)

    def test_it_composes_with_dry_run_only(self) -> None:
        from src.scripts import backfill_species_photos as backfill

        for extra in (["--force"], ["--limit", "5"]):
            with (
                patch("sys.argv", ["backfill", "--recheck-size", *extra]),
                pytest.raises(SystemExit),
            ):
                backfill._parse_args()
