"""Storage for certification card images and PDFs.

The **only** module that knows a certification card has stored bytes at all. Routes go
through these functions and never see where those bytes are, which is what let the payload
move out of a `bytea` column and onto the files volume without a single call site
changing.

`services/blob_store.py` is the layer below: it owns the filesystem, and this module owns
what is stored, under which key, against which row. Deliberately no abstract `FileStorage`
base class or runtime-selected backend at either level - there is exactly one
implementation and no configuration that would choose between two. The module boundary is
the seam; an interface with a single implementor would be scaffolding for a migration that
hasn't happened yet.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import UploadFile
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ..core.db.database import release_read_transaction
from ..core.utils.uploads import read_upload_within_limit, safe_filename
from ..models.certification_file import CertificationFile
from ..schemas.certification import CertificationFileInfo, CertificationSide
from . import blob_store

# The key prefix every card file is stored under. A "kind" rather than a directory, so a
# later kind (dive photos, species images) can pick its own layout without moving anything
# already written - see `blob_store.build_key`.
KEY_KIND = "certification-files"

# Phone photos of a card run 2-4 MB; a flatbed scan of one can reach 8. Ten is generous
# headroom over both while still bounding what a single request buffers in memory -
# `read_upload_within_limit` holds the whole upload, and so does the download that serves
# it back.
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

    **The file is written before the row, and the replaced file is unlinked after the
    commit.** That ordering is the whole consistency story now that the two live in
    different stores: a crash between the two strands an unreferenced file, which is
    harmless and swept, where the reverse order would leave a committed row pointing at
    bytes that do not exist.
    """
    data = await read_upload_within_limit(upload, MAX_CARD_FILE_SIZE)
    if not data:
        raise UnsupportedCardFileError("The uploaded file is empty.")

    content_type = sniff_content_type(data)
    filename = safe_filename(upload.filename, default="card")
    digest = hashlib.sha256(data).hexdigest()
    now = datetime.now(UTC)

    # A narrow read of what this side currently holds, purely so the replaced file can be
    # unlinked afterwards and so the new key carries the row's *existing* uuid rather than
    # a fresh one. Re-feeding a live row the same bytes therefore mints the identical key,
    # which is what makes the whole `PUT` idempotent down to the filesystem: nothing is
    # scheduled for unlinking, and the write lands byte-identically on the file already
    # there.
    #
    # It does not make the upsert below any less atomic - that is still one statement
    # against the unique index. In the vanishingly rare case where a concurrent insert wins
    # between this read and that statement, the `DO UPDATE` fires and the stored key
    # carries a uuid7 minted here instead of the row's. Still unique, still never reusable,
    # and the loser's own file is simply an orphan for the sweeper.
    existing = (
        await db.execute(
            select(CertificationFile.uuid, CertificationFile.storage_key).where(
                CertificationFile.certification_id == certification_id,
                CertificationFile.side == side.value,
            )
        )
    ).one_or_none()

    row_uuid = existing.uuid if existing is not None else uuid7()
    key = blob_store.build_key(KEY_KIND, row_uuid=row_uuid, sha256=digest)
    # Before the write, not after: `blob_store.put` is a threadpool hop with an `fsync` in
    # it, and the lookup above autobegan a transaction that would otherwise be held open
    # across the whole of it. The `Row` of two scalars survives the rollback - see
    # `release_read_transaction` for why that precondition is the caller's to check.
    await release_read_transaction(db)
    await blob_store.put(key, data)

    # Just the parts a replacement changes. Everything identifying the row -
    # `certification_id`, `side`, `uuid`, `created_at` - is set on insert only, so
    # re-photographing a card swaps its bytes without the file changing identity.
    payload = {
        "content_type": content_type,
        "byte_size": len(data),
        "original_filename": filename,
        "sha256": digest,
        "storage_key": key,
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
            uuid=row_uuid,
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
    # Guarded on inequality, and that guard is load-bearing rather than an optimization:
    # the same row re-fed the same bytes produces the same key, and unlinking it would
    # delete the file just written.
    if existing is not None and existing.storage_key != key:
        blob_store.delete_after_commit(db, existing.storage_key)
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

    `None` means *this side has no row*. A row whose file is missing from the volume
    raises `blob_store.BlobMissingError` instead, and every caller has to decide what to do
    with that: the download route 500s, the export archive skips the member, a backfill
    counts it failed. Collapsing the two into `None` here would turn data loss into a 404,
    which is precisely the report that would stop anyone investigating.
    """
    stmt = select(
        CertificationFile.storage_key,
        CertificationFile.content_type,
        CertificationFile.original_filename,
        CertificationFile.sha256,
    ).where(
        CertificationFile.certification_id == certification_id,
        CertificationFile.side == side.value,
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        return None

    return LoadedCardFile(
        data=await blob_store.get(row.storage_key),
        content_type=row.content_type,
        original_filename=row.original_filename,
        sha256=row.sha256,
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

    Hard, not soft: a soft-deleted row keeps a file nothing can read. The metadata it would
    preserve isn't worth the storage.

    Row first, file after the commit - the mirror of the write ordering in
    `store_certification_file`, and for the same reason. `commit=False` callers get the
    unlink riding *their* transaction: `erase_certification` removes the card files and
    soft-deletes the certification together, and a rollback there must leave the files
    where they are.
    """
    keys = list(
        (
            await db.execute(
                delete(CertificationFile)
                .where(
                    CertificationFile.certification_id == certification_id,
                    CertificationFile.side == side.value,
                )
                .returning(CertificationFile.storage_key)
            )
        ).scalars()
    )
    if not keys:
        return False

    blob_store.delete_after_commit(db, keys)
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

    Note the gap this does *not* close: a hard delete of a `Certification` from the admin
    panel fires the FK's cascade with no service layer in the way, so the rows go and the
    files stay. They are unreferenced files at that point, which is exactly what
    `src/scripts/sweep_orphaned_files.py` reclaims.
    """
    keys = list(
        (
            await db.execute(
                delete(CertificationFile)
                .where(CertificationFile.certification_id == certification_id)
                .returning(CertificationFile.storage_key)
            )
        ).scalars()
    )
    blob_store.delete_after_commit(db, keys)
    if commit:
        await db.commit()


async def get_file_infos_for_certifications(
    db: AsyncSession, *, certification_ids: list[int]
) -> dict[int, list[CertificationFileInfo]]:
    """Resolve a whole page of certifications' file metadata in one query.

    Every row in the list view shows whether it has a front and a back, so fetching this
    per row would be an N+1 on the hot path - the same reasoning (and shape) as
    `get_schedules_for_gear_items`. Selects explicit columns rather than whole entities,
    which is now habit rather than necessity: it predates the payload leaving Postgres,
    where a `select(CertificationFile)` would have been one careless `undefer` from
    dragging megabytes through a list view.
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
