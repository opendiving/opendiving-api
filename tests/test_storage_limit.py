"""The per-account storage limit, against Postgres, at every write that can grow an account.

Each site is driven through its real service or route, and the account is filled to within a
byte of the limit by a card row of a chosen size - a row with no file behind it, which
nothing here reads - so "at the limit" and "one byte over" are exact rather than
approximate. What a site's write adds is measured by running it once on a scratch account
first, because what a dive-computer file or a picture occupies once stored is the store's to
say, not this module's.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import hashlib
import io
import json
import random
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import logbook_import as import_routes
from src.app.api.v1.dives import parse_dive
from src.app.api.v1.users import read_storage_usage
from src.app.core.config import settings
from src.app.core.security import create_dive_file_token, create_logbook_import_token
from src.app.models.certification import Certification
from src.app.models.certification_file import CertificationFile
from src.app.models.dive import Dive
from src.app.models.dive_file import DiveFile
from src.app.models.dive_species import DiveSpecies
from src.app.models.user import User
from src.app.schemas.certification import CertificationSide
from src.app.schemas.logbook_import import ImportPortraitChoice
from src.app.schemas.user_picture import PictureCrop
from src.app.services import blob_store
from src.app.services.certification_files import store_certification_file
from src.app.services.dive_files import KEY_KIND as DIVE_FILE_KIND
from src.app.services.dive_files import store_recording_file
from src.app.services.export import load_export_bundle
from src.app.services.export.archive import DIVEJSON_NAME, write_archive
from src.app.services.logbook_import import batch_digest
from src.app.services.storage_usage import format_size, get_storage_usage
from src.app.services.user_pictures import (
    AVATAR_FRAME,
    PORTRAIT_FRAME,
    copy_avatar_to_portrait,
    recrop_picture,
    store_picture,
)
from tests.conftest import db_available
from tests.helpers.generators import create_certification, create_dive, create_species, create_user
from tests.helpers.images import phone_jpeg, plain_png
from tests.helpers.import_parts import import_request, part_of

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

LIMIT = 1024 * 1024
REFUSAL = "of 1.0 MB used"

SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"
EXPORT = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><StartTime>2026-09-08T15:17:38</StartTime>
<MaxDepth>25.5</MaxDepth><Duration>1800</Duration></Dive>
""".encode()


def _noise_png(size: tuple[int, int], *, seed: int = 7) -> bytes:
    """Pixels that do not compress, from a fixed seed: a picture whose crop decides how big
    its rendition is, and the same bytes on every run."""
    pixels = random.Random(seed).randbytes(size[0] * size[1] * 3)
    buffer = io.BytesIO()
    Image.frombytes("RGB", size, pixels).save(buffer, format="PNG")
    return buffer.getvalue()


NOISE = _noise_png((64, 64))


@pytest.fixture(autouse=True)
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def one_megabyte(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "STORAGE_LIMIT_MB", 1)


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """The import routes throttle and drop caches through Redis, which is beside the point."""
    monkeypatch.setattr(import_routes, "_enforce_import_limit", AsyncMock())
    monkeypatch.setattr(import_routes, "resolve_catalog_gaps", AsyncMock(return_value=frozenset()))
    for name in dir(import_routes):
        if name.startswith("invalidate_"):
            monkeypatch.setattr(import_routes, name, AsyncMock())


def _upload(content: bytes, filename: str) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


def _caller(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "is_superuser": user.is_superuser}


def _hold(db: Session, user: User, size: int) -> None:
    """Make `user` hold `size` more stored bytes: a card side of a certification of its own."""
    certification = create_certification(db, user)
    db.add(
        CertificationFile(
            certification_id=certification.id,
            side="front",
            content_type="image/jpeg",
            byte_size=size,
            original_filename="filler.jpg",
            sha256="f" * 64,
            storage_key=f"certification-files/ff/{uuid7()}_{'f' * 64}",
        )
    )
    db.commit()


async def _used(async_db: AsyncSession, user: User) -> int:
    usage = await get_storage_usage(async_db, user_id=user.id)
    await async_db.rollback()
    return usage.used_bytes


async def _attach(async_db: AsyncSession, user: User, dive_id: int, content: bytes = EXPORT) -> Any:
    return await store_recording_file(
        async_db,
        user_id=user.id,
        user_uuid=user.uuid,
        dive_id=dive_id,
        upload=_upload(content, "export.xml"),
        file_token=create_dive_file_token(
            user_uuid=user.uuid, sha256=hashlib.sha256(content).hexdigest(), parser_key="suunto_xml"
        ),
    )


async def _archive_of(async_db: AsyncSession, user: User) -> bytes:
    bundle = await load_export_bundle(async_db, user_id=user.id)
    buffer = await write_archive(async_db, bundle, exported_at=datetime.now(UTC))
    try:
        return buffer.read()
    finally:
        buffer.close()


async def _preview(async_db: AsyncSession, user: User, archive: bytes) -> Any:
    return await _preview_files(async_db, user, [("logbook.zip", archive)])


async def _preview_files(async_db: AsyncSession, user: User, files: list[tuple[str, bytes]]) -> Any:
    return await import_routes.preview_logbook_import(
        request=import_request(files), current_user=_caller(user), db=async_db
    )


async def _apply(
    async_db: AsyncSession, user: User, archive: bytes, portrait: ImportPortraitChoice | None = None
) -> Any:
    return await _apply_files(async_db, user, [("logbook.zip", archive)], portrait)


async def _apply_files(
    async_db: AsyncSession,
    user: User,
    files: list[tuple[str, bytes]],
    portrait: ImportPortraitChoice | None = None,
) -> Any:
    parts = [part_of(data, name, index) for index, (name, data) in enumerate(files)]
    fields: dict[str, Any] = {"token": create_logbook_import_token(user_uuid=user.uuid, sha256=batch_digest(parts))}
    if portrait is not None:
        fields["portrait"] = portrait.model_dump()
    return await import_routes.apply_logbook_import(
        request=import_request(files, fields), current_user=_caller(user), db=async_db
    )


# A dive computer's two exports of one dive, which an import keeps on the dive it becomes.
DIVE_FILES = [
    (name, (Path(__file__).parent / "fixtures" / "dive_files" / name).read_bytes())
    for name in ("suunto-ocean-2026.fit", "suunto-ocean-2026.json")
]


# -------------------------------------------------------------------- the sites


@dataclass(frozen=True)
class Site:
    """One write that can grow an account: what it needs first, and the write itself."""

    name: str
    write: Callable[[Session, AsyncSession, User, dict[str, Any]], Awaitable[object]]
    arrange: Callable[[Session, AsyncSession, User], Awaitable[dict[str, Any]]] | None = None
    # What the write adds, where it stores nothing to measure it by: the parse pre-check.
    adds: Callable[[], Awaitable[int]] | None = None


async def _a_dive(db: Session, async_db: AsyncSession, user: User) -> dict[str, Any]:
    return {"dive_id": create_dive(db, user).id}


async def _a_certification(db: Session, async_db: AsyncSession, user: User) -> dict[str, Any]:
    return {"certification_id": create_certification(db, user).id}


async def _an_avatar_of_a_corner(db: Session, async_db: AsyncSession, user: User) -> dict[str, Any]:
    """An avatar kept with its original and cropped to a corner, so re-cropping it to the
    whole picture draws a larger rendition."""
    await store_picture(
        async_db,
        user_id=user.id,
        frame=AVATAR_FRAME,
        upload=_upload(NOISE, "noise.png"),
        crop=PictureCrop(x=0, y=0, width=8, height=8),
    )
    return {}


async def _an_archive_with_files(db: Session, async_db: AsyncSession, user: User) -> dict[str, Any]:
    """Another account's archive carrying one dive-computer file and one card image."""
    source = create_user(db)
    await _attach(async_db, source, create_dive(db, source).id)
    await store_certification_file(
        async_db,
        user_id=source.id,
        certification_id=create_certification(db, source).id,
        side=CertificationSide.FRONT,
        upload=_upload(plain_png(size=(9, 6)), "card.png"),
    )
    return {"archive": await _archive_of(async_db, source)}


SITES = [
    Site(
        "parse",
        write=lambda db, async_db, user, ctx: parse_dive(
            current_user=_caller(user), db=async_db, file=_upload(EXPORT, "export.xml")
        ),
        adds=lambda: blob_store.stored_size(DIVE_FILE_KIND, EXPORT),
    ),
    Site("attach", arrange=_a_dive, write=lambda db, async_db, user, ctx: _attach(async_db, user, ctx["dive_id"])),
    Site(
        "card",
        arrange=_a_certification,
        write=lambda db, async_db, user, ctx: store_certification_file(
            async_db,
            user_id=user.id,
            certification_id=ctx["certification_id"],
            side=CertificationSide.FRONT,
            upload=_upload(plain_png(size=(9, 6)), "card.png"),
        ),
    ),
    Site(
        "avatar",
        write=lambda db, async_db, user, ctx: store_picture(
            async_db,
            user_id=user.id,
            frame=AVATAR_FRAME,
            upload=_upload(phone_jpeg(), "me.jpg"),
            crop=PictureCrop(x=0, y=0, width=48, height=48),
        ),
    ),
    Site(
        "portrait",
        write=lambda db, async_db, user, ctx: store_picture(
            async_db,
            user_id=user.id,
            frame=PORTRAIT_FRAME,
            upload=_upload(phone_jpeg(), "me.jpg"),
            crop=PictureCrop(x=0, y=0, width=35, height=45),
        ),
    ),
    Site(
        "portrait from avatar",
        arrange=_an_avatar_of_a_corner,
        write=lambda db, async_db, user, ctx: copy_avatar_to_portrait(
            async_db, user_id=user.id, crop=PictureCrop(x=0, y=0, width=49, height=63)
        ),
    ),
    Site(
        "recrop",
        arrange=_an_avatar_of_a_corner,
        write=lambda db, async_db, user, ctx: recrop_picture(
            async_db, user_id=user.id, frame=AVATAR_FRAME, crop=PictureCrop(x=0, y=0, width=64, height=64)
        ),
    ),
    Site(
        "import",
        arrange=_an_archive_with_files,
        write=lambda db, async_db, user, ctx: _apply(async_db, user, ctx["archive"]),
    ),
    Site("import files", write=lambda db, async_db, user, ctx: _apply_files(async_db, user, DIVE_FILES)),
]


def _site(name: str) -> Site:
    return next(site for site in SITES if site.name == name)


async def _arranged(site: Site, db: Session, async_db: AsyncSession, user: User) -> dict[str, Any]:
    return {} if site.arrange is None else await site.arrange(db, async_db, user)


async def _growth(site: Site, db: Session, async_db: AsyncSession) -> int:
    """What `site`'s write adds to an account, measured on a scratch one."""
    if site.adds is not None:
        return await site.adds()
    scratch = create_user(db)
    ctx = await _arranged(site, db, async_db, scratch)
    before = await _used(async_db, scratch)
    await site.write(db, async_db, scratch, ctx)
    return await _used(async_db, scratch) - before


@pytest.mark.parametrize("site", SITES, ids=[site.name for site in SITES])
class TestEverySiteStopsAtTheLimit:
    @pytest.mark.asyncio
    async def test_one_stored_byte_over_is_refused_and_writes_nothing(
        self, site: Site, db: Session, async_db: AsyncSession
    ) -> None:
        growth = await _growth(site, db, async_db)
        assert growth > 0, "the site must grow the account for its limit to mean anything"
        diver = create_user(db)
        ctx = await _arranged(site, db, async_db, diver)
        _hold(db, diver, LIMIT + 1 - await _used(async_db, diver) - growth)
        before = await _used(async_db, diver)
        keys = set(blob_store.iter_keys())

        with pytest.raises(HTTPException) as refused:
            await site.write(db, async_db, diver, ctx)
        await async_db.rollback()

        assert refused.value.status_code == 413
        assert f"{format_size(before)} {REFUSAL}" in refused.value.detail
        assert await _used(async_db, diver) == before
        assert set(blob_store.iter_keys()) == keys, "a refusal leaves no file behind"

    @pytest.mark.asyncio
    async def test_exactly_the_limit_is_admitted(self, site: Site, db: Session, async_db: AsyncSession) -> None:
        growth = await _growth(site, db, async_db)
        diver = create_user(db)
        ctx = await _arranged(site, db, async_db, diver)
        _hold(db, diver, LIMIT - await _used(async_db, diver) - growth)

        await site.write(db, async_db, diver, ctx)

        assert await _used(async_db, diver) == (LIMIT - growth if site.adds is not None else LIMIT)


# -------------------------------------------------------------------- what is never refused


class TestWhatIsNeverRefused:
    @pytest.mark.asyncio
    async def test_bytes_the_account_holds_add_nothing_at_parse_or_at_attach(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """A repeat, and the repair after a lost file, re-upload bytes already counted - and
        attach admits only bytes carrying a parse's token, so a parse that refused them would
        make both impossible for an account at or past its limit."""
        diver = create_user(db)
        dive = create_dive(db, diver)
        first = await _attach(async_db, diver, dive.id)
        _hold(db, diver, LIMIT + 1 - await _used(async_db, diver))

        await parse_dive(current_user=_caller(diver), db=async_db, file=_upload(EXPORT, "export.xml"))
        again = await _attach(async_db, diver, dive.id)

        assert again.recording_id == first.recording_id

    @pytest.mark.asyncio
    async def test_a_smaller_card_replaces_a_larger_one_on_an_account_past_its_limit(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """After a lowered setting or an overshoot, the diver can still shrink what they hold."""
        diver = create_user(db)
        certification = create_certification(db, diver)

        async def photograph(image: bytes) -> None:
            await store_certification_file(
                async_db,
                user_id=diver.id,
                certification_id=certification.id,
                side=CertificationSide.FRONT,
                upload=_upload(image, "card.png"),
            )

        await photograph(NOISE)
        _hold(db, diver, LIMIT + 1 - await _used(async_db, diver))
        before = await _used(async_db, diver)

        await photograph(plain_png(size=(9, 6)))

        assert await _used(async_db, diver) < before

    @pytest.mark.asyncio
    async def test_a_smaller_picture_replaces_a_larger_one_on_an_account_past_its_limit(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        await store_picture(
            async_db,
            user_id=diver.id,
            frame=AVATAR_FRAME,
            upload=_upload(NOISE, "noise.png"),
            crop=PictureCrop(x=0, y=0, width=64, height=64),
        )
        _hold(db, diver, LIMIT + 1 - await _used(async_db, diver))
        before = await _used(async_db, diver)

        await store_picture(
            async_db, user_id=diver.id, frame=AVATAR_FRAME, upload=_upload(plain_png(size=(8, 8)), "dot.png"), crop=None
        )

        assert await _used(async_db, diver) < before

    @pytest.mark.asyncio
    async def test_with_no_limit_nothing_is_refused(
        self, db: Session, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "STORAGE_LIMIT_MB", None)
        diver = create_user(db)
        _hold(db, diver, 2_000_000_000)

        await store_certification_file(
            async_db,
            user_id=diver.id,
            certification_id=create_certification(db, diver).id,
            side=CertificationSide.FRONT,
            upload=_upload(plain_png(size=(9, 6)), "card.png"),
        )
        await _attach(async_db, diver, create_dive(db, diver).id)


class TestNobodyIsExempt:
    @pytest.mark.asyncio
    async def test_a_superuser_is_refused_like_anyone(self, db: Session, async_db: AsyncSession) -> None:
        """The invitations quota exempts superusers; this is about bytes the operator pays
        for, which cost the same whoever uploads them."""
        admin = create_user(db, is_super_user=True)
        _hold(db, admin, LIMIT)

        with pytest.raises(HTTPException) as refused:
            await store_certification_file(
                async_db,
                user_id=admin.id,
                certification_id=create_certification(db, admin).id,
                side=CertificationSide.FRONT,
                upload=_upload(plain_png(size=(9, 6)), "card.png"),
            )

        assert refused.value.status_code == 413


# -------------------------------------------------------------------- an import, whole


def _archive_with_a_portrait(member: bytes) -> bytes:
    """An archive another producer wrote, whose diver's portrait is `member` and nothing else."""
    stored = {
        "uuid": str(uuid7()),
        "original_filename": "face.jpg",
        "content_type": "image/jpeg",
        "byte_size": len(member),
        "sha256": hashlib.sha256(member).hexdigest(),
        "archive_path": "face.jpg",
    }
    document = json.dumps({"format": "divejson", "version": "1.0", "diver": {"portrait_file": stored}}).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(DIVEJSON_NAME, document)
        archive.writestr("face.jpg", member)
    return buffer.getvalue()


class TestAnImport:
    @pytest.mark.asyncio
    async def test_the_preview_refuses_what_the_apply_would_and_the_apply_writes_no_row(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        archive = (await _an_archive_with_files(db, async_db, create_user(db)))["archive"]
        growth = await _growth(_site("import"), db, async_db)
        diver = create_user(db)
        _hold(db, diver, LIMIT + 1 - growth)

        with pytest.raises(HTTPException) as previewed:
            await _preview(async_db, diver, archive)
        await async_db.rollback()
        with pytest.raises(HTTPException) as applied:
            await _apply(async_db, diver, archive)
        await async_db.rollback()

        assert previewed.value.status_code == applied.value.status_code == 413
        assert REFUSAL in previewed.value.detail and REFUSAL in applied.value.detail
        for model in (Dive, DiveFile):
            owned = await async_db.scalar(select(func.count()).select_from(model).where(model.user_id == diver.id))
            assert owned == 0, model.__tablename__
        assert (
            await async_db.scalar(
                select(func.count()).select_from(Certification).where(Certification.user_id == diver.id)
            )
            == 1
        ), "only the filler's certification"

    @pytest.mark.asyncio
    async def test_files_kept_as_themselves_are_refused_whole_at_the_preview_and_at_the_apply(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """Measured as they would be stored, both of them, before a row or an object of either
        is written."""
        growth = await _growth(_site("import files"), db, async_db)
        diver = create_user(db)
        _hold(db, diver, LIMIT + 1 - growth)
        keys = set(blob_store.iter_keys())

        with pytest.raises(HTTPException) as previewed:
            await _preview_files(async_db, diver, DIVE_FILES)
        await async_db.rollback()
        with pytest.raises(HTTPException) as applied:
            await _apply_files(async_db, diver, DIVE_FILES)
        await async_db.rollback()

        assert previewed.value.status_code == applied.value.status_code == 413
        assert growth == sum([await blob_store.stored_size(DIVE_FILE_KIND, data) for _, data in DIVE_FILES]), (
            "each file counts at the size it is stored at"
        )
        for model in (Dive, DiveFile):
            owned = await async_db.scalar(select(func.count()).select_from(model).where(model.user_id == diver.id))
            assert owned == 0, model.__tablename__
        assert set(blob_store.iter_keys()) == keys

    @pytest.mark.asyncio
    async def test_the_archive_s_portrait_counts_only_once_it_is_taken(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The preview measures the import with the account's own portrait kept, which is the
        apply's default - so a portrait that tips the account over is refused at the apply
        that takes it, and nowhere else."""
        archive = _archive_with_a_portrait(phone_jpeg(size=(320, 240), orientation=None))
        scratch = create_user(db)
        await _apply(async_db, scratch, archive, ImportPortraitChoice(choice="take", account_sha256=None))
        growth = await _used(async_db, scratch)
        diver = create_user(db)
        _hold(db, diver, LIMIT + 1 - growth)

        preview = await _preview(async_db, diver, archive)
        await _apply(async_db, diver, archive, ImportPortraitChoice(choice="keep", account_sha256=None))
        with pytest.raises(HTTPException) as refused:
            await _apply(async_db, diver, archive, ImportPortraitChoice(choice="take", account_sha256=None))

        assert preview.portrait is not None
        assert refused.value.status_code == 413


# -------------------------------------------------------------------- the usage route


class TestTheUsageRoute:
    @pytest.mark.asyncio
    async def test_the_total_is_its_three_parts_and_species_photos_are_nobody_s(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        dive = create_dive(db, diver)
        await _attach(async_db, diver, dive.id)
        card = plain_png(size=(9, 6))
        await store_certification_file(
            async_db,
            user_id=diver.id,
            certification_id=create_certification(db, diver).id,
            side=CertificationSide.FRONT,
            upload=_upload(card, "card.png"),
        )
        await store_picture(
            async_db,
            user_id=diver.id,
            frame=AVATAR_FRAME,
            upload=_upload(phone_jpeg(), "me.jpg"),
            crop=PictureCrop(x=0, y=0, width=48, height=48),
        )
        photo = plain_png(size=(30, 20))
        photo_key = blob_store.new_key("species-photos", sha256=hashlib.sha256(photo).hexdigest())
        await blob_store.put(photo_key, photo)
        species = create_species(db, photo_storage_key=photo_key, photo_sha256=hashlib.sha256(photo).hexdigest())
        db.add(DiveSpecies(dive_id=dive.id, species_id=species.id, position=0))
        db.commit()

        usage = await read_storage_usage(request=MagicMock(), current_user=_caller(diver), db=async_db)

        stored_export = (
            await async_db.execute(select(DiveFile.stored_byte_size).where(DiveFile.user_id == diver.id))
        ).scalar_one()
        assert usage.dive_files_bytes == stored_export == await blob_store.stored_size(DIVE_FILE_KIND, EXPORT)
        assert usage.certification_files_bytes == len(card)
        assert usage.pictures_bytes > 0
        assert usage.used_bytes == usage.dive_files_bytes + usage.certification_files_bytes + usage.pictures_bytes
        assert usage.limit_bytes == LIMIT

    @pytest.mark.asyncio
    async def test_no_limit_reads_as_null(
        self, db: Session, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "STORAGE_LIMIT_MB", None)

        usage = await read_storage_usage(request=MagicMock(), current_user=_caller(create_user(db)), db=async_db)

        assert usage.limit_bytes is None
        assert usage.used_bytes == 0


class TestTheFiguresReadAsTheWebShowsThem:
    """`formatFileSize` rounds a tie upward - `Math.round` and `toFixed` on quotients that are
    exact in binary - where Python's `round` would go to even."""

    @pytest.mark.parametrize(
        ("num_bytes", "shown"),
        [
            (0, "0 KB"),
            (1, "1 KB"),
            (2560, "3 KB"),
            (1048575, "1024 KB"),
            (1048576, "1.0 MB"),
            (1310720, "1.3 MB"),
            (1073741823, "1024.0 MB"),
            (1073741824, "1.0 GB"),
            (1342177280, "1.3 GB"),
        ],
    )
    def test_each_unit_and_its_tie(self, num_bytes: int, shown: str) -> None:
        assert format_size(num_bytes) == shown
