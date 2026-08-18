"""Storage for certification card images and PDFs.

The **only** module that reads or writes `certification_file.data`. Routes go through
these functions and never see bytes-in-a-column, so moving the payload to object storage
later means rewriting this file (and adding a `storage_key` column) rather than touching
every call site.

Deliberately no abstract `FileStorage` base class or runtime-selected backend: there is
exactly one implementation and no configuration that would choose between two. The
module boundary is the seam; an interface with a single implementor would be
scaffolding for a migration that hasn't happened yet.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer
from uuid6 import uuid7

from ..core.utils.uploads import read_upload_within_limit, safe_filename
from ..models.certification_file import CertificationFile
from ..schemas.certification import CertificationFileInfo, CertificationSide

# Phone photos of a card run 2-4 MB; a flatbed scan of one can reach 8. Ten is generous
# headroom over both while still bounding what a single request can push into the
# database - which, with the bytes living in Postgres, is the resource actually at risk.
MAX_CARD_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Leading-byte signatures for the formats we accept, checked instead of trusting the
# client's `Content-Type`. The sniffed value is what gets stored and later handed back as
# the response's `Content-Type`, so accepting the uploader's claim here would let someone
# have us serve arbitrary bytes as a type of their choosing.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
)

# HEIC/HEIF is what an iPhone stores natively. It is detected separately from the
# accepted formats purely so the caller can say *why* it was rejected - see
# `UnsupportedCardFileError` below. The signature sits at offset 4 (`ftyp` box) rather
# than at the start of the file.
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")


class UnsupportedCardFileError(Exception):
    """The uploaded bytes are not one of the accepted card formats."""


@dataclass(frozen=True, slots=True)
class LoadedCardFile:
    """A card file's bytes plus everything the download route needs to serve them."""

    data: bytes
    content_type: str
    original_filename: str
    sha256: str


def sniff_content_type(data: bytes) -> str:
    """Identify an upload from its leading bytes, or raise `UnsupportedCardFileError`.

    HEIC gets its own message because it is the format an iPhone produces by default and
    a bare "unsupported file type" would read as a bug to anyone who just photographed
    their card. In practice iOS transcodes to JPEG when uploading through a file input,
    so this mostly catches files picked out of the Files app.
    """
    for signature, content_type in _SIGNATURES:
        if data.startswith(signature):
            return content_type

    # WEBP is a RIFF container: "RIFF", a 4-byte length, then "WEBP".
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        raise UnsupportedCardFileError(
            "HEIC images aren't supported yet. Please convert the photo to JPEG or PNG and try again."
        )

    raise UnsupportedCardFileError("Unsupported file type. Upload a JPEG, PNG, WEBP or PDF.")


async def store_certification_file(
    db: AsyncSession, *, certification_id: int, side: CertificationSide, upload: UploadFile
) -> CertificationFileInfo:
    """Store (or replace) one side's card file.

    Raises `HTTPException(413)` via `read_upload_within_limit` if the upload is oversized
    and `UnsupportedCardFileError` if its bytes aren't an accepted format.

    The write is an `ON CONFLICT ... DO UPDATE` against the `(certification_id, side)`
    unique index rather than a read-then-insert-or-update: re-uploading a side is the
    normal case (a diver retaking a blurry photo), and two uploads racing must not be
    able to leave two rows for the same side.
    """
    data = await read_upload_within_limit(upload, MAX_CARD_FILE_SIZE)
    if not data:
        raise UnsupportedCardFileError("The uploaded file is empty.")

    content_type = sniff_content_type(data)
    filename = safe_filename(upload.filename, default="card")
    digest = hashlib.sha256(data).hexdigest()
    now = datetime.now(UTC)

    # Just the parts a replacement changes. Everything identifying the row -
    # `certification_id`, `side`, `uuid`, `created_at` - is set on insert only, so
    # re-photographing a card swaps its bytes without the file changing identity.
    payload = {
        "content_type": content_type,
        "byte_size": len(data),
        "original_filename": filename,
        "sha256": digest,
        "data": data,
    }
    stmt = (
        pg_insert(CertificationFile)
        .values(
            certification_id=certification_id,
            side=side.value,
            # Spelled out rather than left to `PublicUUIDMixin`'s `default_factory`: that
            # is a dataclass-level default, applied when the ORM constructs an instance,
            # and this Core-level INSERT never constructs one. Without it Postgres gets a
            # NULL and rejects the row.
            uuid=uuid7(),
            created_at=now,
            **payload,
        )
        .on_conflict_do_update(
            index_elements=[CertificationFile.certification_id, CertificationFile.side],
            # `created_at` is deliberately absent: it keeps recording when this side was
            # first added, while `updated_at` tracks the replacement.
            set_={**payload, "updated_at": now},
        )
        .returning(CertificationFile.uuid, CertificationFile.updated_at)
    )
    result = await db.execute(stmt)
    row = result.one()
    await db.commit()

    return CertificationFileInfo(
        uuid=row.uuid,
        side=side,
        content_type=content_type,
        byte_size=len(data),
        original_filename=filename,
        updated_at=row.updated_at,
    )


async def load_certification_file(
    db: AsyncSession, *, certification_id: int, side: CertificationSide
) -> LoadedCardFile | None:
    """Fetch one side's bytes, or `None` if that side has no file.

    The only place `data` is ever loaded - hence the explicit `undefer`, which is what
    makes every *other* query against this table cheap by default.
    """
    stmt = (
        select(CertificationFile)
        .where(
            CertificationFile.certification_id == certification_id,
            CertificationFile.side == side.value,
        )
        .options(undefer(CertificationFile.data))
    )
    file = (await db.execute(stmt)).scalar_one_or_none()
    if file is None:
        return None

    # Detached before returning, for the same reason as `load_dive_file`: everything worth
    # having is copied out below and what stays behind is megabytes. The full export walks
    # every card a diver holds in one session (`services/export/archive.py`), which is the
    # run that makes this matter rather than merely tidy.
    db.expunge(file)

    return LoadedCardFile(
        data=file.data,
        content_type=file.content_type,
        original_filename=file.original_filename,
        sha256=file.sha256,
    )


async def get_certification_file_sha256(
    db: AsyncSession, *, certification_id: int, side: CertificationSide
) -> str | None:
    """Fetch just one side's content hash, without touching its bytes.

    Lets the download route answer a conditional request (`If-None-Match`) with a 304
    after a single narrow query, instead of pulling megabytes out of the database only to
    discard them. Returns `None` if that side has no file.
    """
    stmt = select(CertificationFile.sha256).where(
        CertificationFile.certification_id == certification_id,
        CertificationFile.side == side.value,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def delete_certification_file(
    db: AsyncSession, *, certification_id: int, side: CertificationSide, commit: bool = True
) -> bool:
    """Hard-delete one side's file. Returns whether there was one to delete.

    Hard, not soft: a soft-deleted blob keeps occupying its bytes forever with nothing
    able to read it. The metadata it would preserve isn't worth the storage.
    """
    file = (
        await db.execute(
            select(CertificationFile).where(
                CertificationFile.certification_id == certification_id,
                CertificationFile.side == side.value,
            )
        )
    ).scalar_one_or_none()
    if file is None:
        return False

    await db.delete(file)
    if commit:
        await db.commit()
    return True


async def delete_files_for_certification(db: AsyncSession, *, certification_id: int, commit: bool = True) -> None:
    """Hard-delete every card file belonging to a certification.

    Called when the certification itself is deleted. The FK's `ON DELETE CASCADE` can't
    do this for us: deletion here is application-level (`is_deleted`), so no `DELETE FROM
    certification` ever runs and the cascade never fires - the same reasoning as
    `delete_files_for_dive` in `services/dive_files.py`. `gear_service_schedule` used to
    need a third copy of this and no longer does: `gear_item` is hard-deleted, so its
    cascades fire on their own.
    """
    files = (
        (await db.execute(select(CertificationFile).where(CertificationFile.certification_id == certification_id)))
        .scalars()
        .all()
    )
    for file in files:
        await db.delete(file)
    if commit:
        await db.commit()


async def get_file_infos_for_certifications(
    db: AsyncSession, *, certification_ids: list[int]
) -> dict[int, list[CertificationFileInfo]]:
    """Resolve a whole page of certifications' file metadata in one query.

    Every row in the list view shows whether it has a front and a back, so fetching this
    per row would be an N+1 on the hot path - the same reasoning (and shape) as
    `get_schedules_for_gear_items`. Selects explicit columns rather than whole entities
    so the `bytea` cannot be dragged along even by accident.
    """
    if not certification_ids:
        return {}

    stmt = select(
        CertificationFile.certification_id,
        CertificationFile.uuid,
        CertificationFile.side,
        CertificationFile.content_type,
        CertificationFile.byte_size,
        CertificationFile.original_filename,
        CertificationFile.updated_at,
    ).where(CertificationFile.certification_id.in_(set(certification_ids)))

    infos: dict[int, list[CertificationFileInfo]] = {cert_id: [] for cert_id in certification_ids}
    for row in await db.execute(stmt):
        infos[row.certification_id].append(
            CertificationFileInfo(
                uuid=row.uuid,
                side=CertificationSide(row.side),
                content_type=row.content_type,
                byte_size=row.byte_size,
                original_filename=row.original_filename,
                updated_at=row.updated_at,
            )
        )
    # Front before back, so the UI can render the two in a stable order without sorting.
    for file_infos in infos.values():
        file_infos.sort(key=lambda info: info.side != CertificationSide.FRONT)
    return infos
