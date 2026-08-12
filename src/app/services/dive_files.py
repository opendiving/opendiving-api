"""Storage for the dive-computer exports dives are imported from.

The **only** module that reads or writes `dive_file.data`. Routes go through these
functions and never see bytes-in-a-column, so moving the payload to object storage later
means rewriting this file (and adding a `storage_key` column) rather than touching every
call site - the same seam, for the same reasons, as `services/certification_files.py`.
"""

import hashlib
import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import UploadFile
from sqlalchemy import CursorResult, delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer
from starlette.concurrency import run_in_threadpool
from uuid6 import uuid7

from ..core.security import verify_dive_file_token
from ..core.utils.uploads import read_upload_within_limit, safe_filename
from ..models.dive import Dive
from ..models.dive_file import DiveFile
from ..schemas.dive import DiveFileInfo
from .dive_parsers import PARSER_BY_KEY
from .dive_profiles import (
    delete_profile_for_dive,
    extract_profile,
    get_existing_profile,
    should_extract,
    store_profile,
)

# Matches the cap `/dive/parse` reads under, since the same file makes both trips: a
# limit here that was lower would let a file pre-fill a form and then be refused
# storage. Exports are small (a few hundred KB); this is headroom, not a target.
MAX_DIVE_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


class InvalidDiveFileTokenError(Exception):
    """The upload wasn't accompanied by proof that this server parsed these bytes."""


class DiveFileAlreadyLinkedError(Exception):
    """These exact bytes are already stored as another dive's source file."""

    def __init__(self, dive_uuid: uuid_pkg.UUID | None) -> None:
        self.dive_uuid = dive_uuid
        super().__init__("This file is already attached to another dive.")


class DiveFileConflictError(Exception):
    """A concurrent upload won a race on one of the table's unique indexes."""


@dataclass(frozen=True, slots=True)
class LoadedDiveFile:
    """A stored export's bytes plus everything the download route needs to serve them."""

    data: bytes
    content_type: str
    original_filename: str
    sha256: str


@dataclass(frozen=True, slots=True)
class _ExistingRow:
    """The dedupe lookup's result - metadata only, never `data`."""

    id: int
    dive_id: int
    uuid: uuid_pkg.UUID
    content_type: str
    byte_size: int
    original_filename: str
    parser_key: str
    updated_at: datetime | None


def reconcile(existing: _ExistingRow | None, dive_id: int) -> Literal["noop", "conflict", "insert"]:
    """Decide what an upload of already-hashed bytes should do.

    Split out from `store_dive_file` so the decision is testable without a database.
    There are only three outcomes, and that is a consequence of `dive_id` being NOT NULL:
    every stored row belongs to a dive the diver can still reach, so there is no fourth
    "orphaned row you could re-claim" case to handle.

    - `noop`: the same bytes are already this dive's source file. `PUT` is idempotent.
    - `conflict`: the same bytes are some *other* dive's source file. Reported rather
      than resolved - see `store_dive_file`.
    - `insert`: unseen bytes. Any file this dive already had is replaced.
    """
    if existing is None:
        return "insert"
    if existing.dive_id == dive_id:
        return "noop"
    return "conflict"


def _info(row: _ExistingRow) -> DiveFileInfo:
    return DiveFileInfo(
        uuid=row.uuid,
        content_type=row.content_type,
        byte_size=row.byte_size,
        original_filename=row.original_filename,
        parser_key=row.parser_key,
        updated_at=row.updated_at,
    )


async def _find_by_digest(db: AsyncSession, *, user_id: int, digest: str) -> _ExistingRow | None:
    """Look up this diver's row for a given content hash.

    Explicit columns rather than `select(DiveFile)`, so the `bytea` cannot be dragged
    along even by accident.
    """
    stmt = select(
        DiveFile.id,
        DiveFile.dive_id,
        DiveFile.uuid,
        DiveFile.content_type,
        DiveFile.byte_size,
        DiveFile.original_filename,
        DiveFile.parser_key,
        DiveFile.updated_at,
    ).where(DiveFile.user_id == user_id, DiveFile.sha256 == digest)
    row = (await db.execute(stmt)).one_or_none()
    return None if row is None else _ExistingRow(*row)


async def _release_read_transaction(db: AsyncSession) -> None:
    """End the read-only transaction the lookups above opened, before a slow extraction.

    `extract_profile` is up to ~1.5 s of pure CPU (see `_MAX_FRAMES`) handed to a worker
    thread. Without this the connection it rode in on sits idle-in-transaction for all of
    it, so a burst of FIT uploads ties up pool connections doing nothing - the event loop
    is free, which is what `run_in_threadpool` bought, but the pool is not.

    Safe at both call sites: nothing has been written yet, so there is nothing to preserve,
    and the writes that follow open their own transaction. Nor can it strand a caller's
    locals - both lookups return frozen dataclasses (`_ExistingRow`, `ExistingProfileRow`)
    rather than ORM instances, so there is nothing to expire.

    `rollback` rather than `commit` because it states what is true here: no work is being
    persisted. If a write ever grows above one of these calls, it wants its own commit
    rather than to be swept up by this.
    """
    await db.rollback()


async def store_dive_file(
    db: AsyncSession,
    *,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    dive_id: int,
    upload: UploadFile,
    file_token: str,
) -> DiveFileInfo:
    """Store (or replace) the export a dive was imported from.

    Raises `HTTPException(413)` via `read_upload_within_limit` if the upload is
    oversized, `InvalidDiveFileTokenError` if it isn't accompanied by a valid parse
    receipt for these exact bytes, and `DiveFileAlreadyLinkedError` if the same content
    is already another dive's source file.

    The token is the admission control. Re-running the parser registry here would only
    establish that the bytes *look* parseable, which would let this endpoint store any
    blob shaped like an export and would not tie the stored file to the parse that
    pre-filled the dive's form. Checking a signature over the content hash establishes
    both, and costs one HMAC over a digest the dedupe needs anyway.
    """
    data = await read_upload_within_limit(upload, MAX_DIVE_FILE_SIZE)
    digest = hashlib.sha256(data).hexdigest()

    claims = verify_dive_file_token(file_token)
    if claims is None:
        raise InvalidDiveFileTokenError("This import has expired. Re-import the file to attach it.")
    if claims.user_uuid != str(user_uuid):
        raise InvalidDiveFileTokenError("This import belongs to a different account. Re-import the file to attach it.")
    if claims.sha256 != digest:
        raise InvalidDiveFileTokenError("This file doesn't match the one that was imported. Re-import it to attach it.")

    # A `parser_key` this build doesn't know means the token outlived a parser being
    # renamed or removed, and there is nothing to record the file as.
    parser = PARSER_BY_KEY.get(claims.parser_key)
    if parser is None:
        raise InvalidDiveFileTokenError("This import is no longer supported. Re-import the file to attach it.")

    existing = await _find_by_digest(db, user_id=user_id, digest=digest)
    outcome = reconcile(existing, dive_id)

    if outcome == "noop" and existing is not None:
        # Nothing is rewritten, not even `original_filename`: the bytes are the file's
        # identity, and re-uploading them is the client repeating itself.
        #
        # The *profile*, though, is a function of (these bytes, the extractor version),
        # so a repeated PUT after `PROFILE_EXTRACTOR_VERSION` was bumped opportunistically
        # upgrades it from bytes already in hand. Still a no-op in the normal case.
        if should_extract(await get_existing_profile(db, dive_id=dive_id), sha256=digest) == "extract":
            await _release_read_transaction(db)
            profile = await run_in_threadpool(extract_profile, parser, data)
            if profile is not None:
                await store_profile(
                    db, dive_id=dive_id, profile=profile, source_sha256=digest, parser_key=parser.key, commit=True
                )
        return _info(existing)

    if outcome == "conflict" and existing is not None:
        # Deliberately not resolved by re-pointing the row at this dive (which would
        # silently strip the file off the dive that has it) or by storing a second copy
        # (which would defeat the dedupe). The realistic cause is logging one export as
        # two dives, and saying so is more use to the diver than either silent fix.
        other_uuid = (await db.execute(select(Dive.uuid).where(Dive.id == existing.dive_id))).scalar_one_or_none()
        raise DiveFileAlreadyLinkedError(other_uuid)

    filename = safe_filename(upload.filename, default="dive-file")
    now = datetime.now(UTC)

    # Deliberately *before* the `try` below, not inside it: an exception raised in there
    # is caught by the `IntegrityError` handler and reported to the diver as a concurrent-
    # upload conflict, which a parse failure is not. `extract_profile` never raises
    # anyway - it logs and returns `None`, because a file that can't be sampled is still
    # worth storing (see `services/dive_profiles.py`) - but the ordering is what makes
    # that true regardless of what it grows into.
    #
    # In a thread for the same reason `POST /dive/parse` parses in one: sampling a FIT
    # file is pure Python and takes up to ~1.5 s at `_MAX_FRAMES`, and this is an
    # `async def`. The read transaction is released first so the connection isn't held
    # idle for the duration - see `_release_read_transaction`.
    await _release_read_transaction(db)
    profile = await run_in_threadpool(extract_profile, parser, data)

    try:
        # Replacement, not versioning: whatever this dive had is gone. Runs before the
        # insert because `ux_dive_file_dive_id` is checked per statement, so two rows
        # for one dive must not coexist even momentarily.
        await db.execute(delete(DiveFile).where(DiveFile.dive_id == dive_id))
        # Same statement-ordering reason, and the same transaction as the file itself: a
        # dive must never end up with a stored file and a profile extracted from a
        # *different* one. Unconditional, so a replacement export with no samples clears
        # the previous export's curves rather than leaving them attributed to it.
        await delete_profile_for_dive(db, dive_id=dive_id, commit=False)
        result = await db.execute(
            insert(DiveFile)
            .values(
                user_id=user_id,
                dive_id=dive_id,
                sha256=digest,
                content_type=parser.content_type,
                byte_size=len(data),
                original_filename=filename,
                parser_key=parser.key,
                data=data,
                # Spelled out rather than left to `PublicUUIDMixin`'s `default_factory`:
                # that is a dataclass-level default applied when the ORM constructs an
                # instance, and this Core-level INSERT never constructs one. Without it
                # Postgres gets a NULL and rejects the row.
                uuid=uuid7(),
                created_at=now,
            )
            .returning(DiveFile.uuid, DiveFile.updated_at)
        )
        row = result.one()
        if profile is not None:
            await store_profile(
                db, dive_id=dive_id, profile=profile, source_sha256=digest, parser_key=parser.key, commit=False
            )
        await db.commit()
    except IntegrityError as exc:
        # Two uploads for the same dive raced between the delete and the insert. One
        # user per dive and a button disabled while in flight make this vanishingly
        # rare; a retry is a better answer than a lock on the hot path.
        await db.rollback()
        raise DiveFileConflictError(
            "The source file for this dive changed while this upload was in flight. Please try again."
        ) from exc

    return DiveFileInfo(
        uuid=row.uuid,
        content_type=parser.content_type,
        byte_size=len(data),
        original_filename=filename,
        parser_key=parser.key,
        updated_at=row.updated_at,
    )


async def load_dive_file(db: AsyncSession, *, dive_id: int) -> LoadedDiveFile | None:
    """Fetch a dive's stored export, or `None` if it has none.

    The only place `data` is ever loaded - hence the explicit `undefer`, which is what
    makes every *other* query against this table cheap by default.
    """
    stmt = select(DiveFile).where(DiveFile.dive_id == dive_id).options(undefer(DiveFile.data))
    file = (await db.execute(stmt)).scalar_one_or_none()
    if file is None:
        return None

    return LoadedDiveFile(
        data=file.data,
        content_type=file.content_type,
        original_filename=file.original_filename,
        sha256=file.sha256,
    )


async def get_dive_file_sha256(db: AsyncSession, *, dive_id: int) -> str | None:
    """Fetch just a dive's stored export's content hash, without touching its bytes.

    Lets the download route answer a conditional request (`If-None-Match`) with a 304
    after a single narrow query, instead of pulling megabytes out of the database only
    to discard them.
    """
    stmt = select(DiveFile.sha256).where(DiveFile.dive_id == dive_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def delete_dive_file(db: AsyncSession, *, dive_id: int, commit: bool = True) -> bool:
    """Hard-delete a dive's stored export. Returns whether there was one to delete.

    Hard, not soft, and not merely unlinked: a row nothing can reach keeps occupying its
    bytes forever, and a "delete" that only hides the file would be a worse trade than
    losing it from the corpus.

    Takes the dive's extracted profile with it. Nothing cascades from removing the file
    (the profile's FK is to `dive`, not to `dive_file`), and a profile whose source export
    is gone can never be re-derived or checked against anything.
    """
    await delete_profile_for_dive(db, dive_id=dive_id, commit=False)
    result = cast(CursorResult, await db.execute(delete(DiveFile).where(DiveFile.dive_id == dive_id)))
    deleted = result.rowcount > 0
    if commit:
        await db.commit()
    return deleted


async def delete_files_for_dive(db: AsyncSession, *, dive_id: int, commit: bool = True) -> None:
    """Hard-delete the stored export belonging to a dive, when the dive itself is deleted.

    The FK's `ON DELETE CASCADE` can't do this for us: deletion is application-level
    (`is_deleted`), so no `DELETE FROM dive` ever runs and the cascade never fires - the
    same reasoning as `delete_files_for_certification`.

    The dive's extracted profile goes too, via `delete_dive_file` - whose own
    `delete_profile_for_dive` call covers this path as well as the explicit
    `DELETE /dive/{uuid}/file` one, so the dive-deletion hook needed no change.
    """
    await delete_dive_file(db, dive_id=dive_id, commit=commit)


async def get_file_infos_for_dives(db: AsyncSession, *, dive_ids: list[int]) -> dict[int, DiveFileInfo | None]:
    """Resolve several dives' source-file metadata in one query.

    Only the detail endpoint asks for this today, and only ever for one dive - but this
    is the `get_file_infos_for_certifications` shape, it makes the explicit-columns
    discipline the default, and it is what showing an attachment marker in the dive list
    would need without a rewrite.
    """
    if not dive_ids:
        return {}

    stmt = select(
        DiveFile.dive_id,
        DiveFile.uuid,
        DiveFile.content_type,
        DiveFile.byte_size,
        DiveFile.original_filename,
        DiveFile.parser_key,
        DiveFile.updated_at,
    ).where(DiveFile.dive_id.in_(set(dive_ids)))

    infos: dict[int, DiveFileInfo | None] = dict.fromkeys(dive_ids)
    for row in await db.execute(stmt):
        infos[row.dive_id] = DiveFileInfo(
            uuid=row.uuid,
            content_type=row.content_type,
            byte_size=row.byte_size,
            original_filename=row.original_filename,
            parser_key=row.parser_key,
            updated_at=row.updated_at,
        )
    return infos
