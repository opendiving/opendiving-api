"""Storage for the dive-computer exports dives are imported from.

The **only** module that knows a dive has a stored export at all. Routes go through these
functions and never see where the bytes are, which is what let the payload move out of a
`bytea` column and onto the files volume without a single call site changing - the same
seam, for the same reasons, as `services/certification_files.py`.
`services/blob_store.py` is the layer below, and the only one that touches a filesystem.
"""

import hashlib
import logging
import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import UploadFile
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from uuid6 import uuid7

from ..core.security import verify_dive_file_token
from ..core.utils.uploads import read_upload_within_limit, safe_filename
from ..models.dive import Dive
from ..models.dive_file import DiveFile
from ..models.dive_mixture import DiveMixture
from ..schemas.dive import DiveFileInfo, DiveTechScalars
from ..schemas.dive_mixture import DiveMixtureRead
from ..schemas.parsed_dive import DiveMixtureSchema
from . import blob_store
from .dive_parsers import PARSER_BY_KEY, DiveParseError, DiveParser, UnsupportedDiveFileError
from .dive_profiles import (
    NormalizedProfile,
    delete_profile_for_dive,
    extract_profile,
    finalize_profile,
    get_existing_profile,
    should_extract,
    store_profile,
)

logger = logging.getLogger(__name__)

# The key prefix every stored export is written under - see `blob_store.build_key`.
KEY_KIND = "dive-files"

# Matches the cap `/dive/parse` reads under, since the same file makes both trips: a
# limit here that was lower would let a file pre-fill a form and then be refused
# storage. Exports are small (a few hundred KB); this is headroom, not a target.
MAX_DIVE_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

# The `dive` columns an import owns outright. Taken from `DiveTechScalars` rather than
# listed here, so the schema that publishes them and the write that fills them cannot
# drift: adding a field to one is adding it to both.
TECH_SCALAR_FIELDS = tuple(DiveTechScalars.model_fields)


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
    """The dedupe lookup's result - metadata only, never the payload."""

    id: int
    dive_id: int
    uuid: uuid_pkg.UUID
    content_type: str
    byte_size: int
    original_filename: str
    parser_key: str
    storage_key: str
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

    Explicit columns rather than `select(DiveFile)`. That predates the payload leaving
    Postgres - it kept the `bytea` from being dragged along by accident - and stays because
    it says exactly what the dedupe decision reads.
    """
    stmt = select(
        DiveFile.id,
        DiveFile.dive_id,
        DiveFile.uuid,
        DiveFile.content_type,
        DiveFile.byte_size,
        DiveFile.original_filename,
        DiveFile.parser_key,
        DiveFile.storage_key,
        DiveFile.updated_at,
    ).where(DiveFile.user_id == user_id, DiveFile.sha256 == digest)
    row = (await db.execute(stmt)).one_or_none()
    return None if row is None else _ExistingRow(*row)


def extract_tech_scalars(parser: type[DiveParser], content: bytes) -> dict[str, float | None] | None:
    """The dive's oxygen-exposure and surface-pressure readings, or `None` if unreadable.

    **Never raises**, on the same terms and for the same reason as `extract_profile`: the
    file is the durable artifact, so a header this build can't read must not fail the
    upload that would have preserved it for a later fix. The dive simply keeps whatever
    it had until a backfill run picks it up.

    Re-parses rather than reusing what `POST /dive/parse` already produced. That parse
    happened in a different request, and the only thing carried forward from it is a
    signature over the *content hash* - so the alternative would be trusting a client to
    hand back the numbers it was shown, for columns the form is deliberately not allowed
    to write. The parse costs a few hundred milliseconds on the thread that is already
    extracting the profile.

    Returns a dict rather than a schema because it is spread straight into an `UPDATE`;
    an all-`None` result is still written, so replacing an export that recorded exposure
    with one that doesn't clears the old dive's readings rather than stranding them.

    **On the attach path this is now the fallback, not the normal route.** `_extract_all`
    goes through `parser.parse_all`, which decodes once and returns both halves; pairing
    this function with `extract_profile` instead makes FIT call `FitParser._scan` twice,
    measured at 55 ms + 58 ms on a 26 KB export where the single scan serving both is
    58 ms, and scaling with `_MAX_FRAMES` up to the ~1.5 s the profile extraction is
    budgeted at. The two Suunto parsers are cheap enough for it not to matter.

    That second pass is what buys the property the fallback exists for: this function and
    `extract_profile` fail *independently*, so a file whose samples are malformed still
    yields its header scalars, and vice versa. Paying a redundant decode on a file that
    was already failing is the right side of that trade.
    """
    try:
        parsed = parser.parse(content)
    except DiveParseError, UnsupportedDiveFileError:
        logger.warning("Tech-scalar extraction failed for a %s file: unreadable header", parser.key, exc_info=True)
        return None
    except Exception:
        logger.exception("Unexpected error extracting tech scalars from a %s file", parser.key)
        return None
    return {name: getattr(parsed, name) for name in TECH_SCALAR_FIELDS}


def _extract_all(
    parser: type[DiveParser], content: bytes
) -> tuple[NormalizedProfile | None, dict[str, float | None] | None]:
    """Both extractions over one set of bytes, for one `run_in_threadpool` hop.

    They are separate functions because they answer separate questions and are tested
    separately, but they are always wanted together and both are pure CPU - so pairing
    them here keeps `store_dive_file` to a single thread handoff instead of two, and
    keeps the "released the read transaction first" reasoning applying to one call.

    Goes through `parse_all` so a parser that can do both off one decode does: FIT
    otherwise scans the file twice, at roughly double the CPU of the single pass it
    needs. On any failure it falls back to the two independent extractions, which is
    what preserves their most useful property - a file whose *samples* are malformed
    still yields its header scalars, and vice versa. The fallback re-decodes, and that
    is the right trade: it costs a second pass only on a file that was already failing,
    where nothing about the latency budget matters any more.
    """
    try:
        parsed, profile = parser.parse_all(content)
    except Exception:
        # Deliberately bare: `parse_all` promises the two parser exceptions, but the
        # fallback is correct for anything at all and swallowing more here costs nothing
        # - `extract_profile` and `extract_tech_scalars` do their own logging, with the
        # per-half message that says which of the two actually went wrong.
        return extract_profile(parser, content), extract_tech_scalars(parser, content)

    try:
        scalars: dict[str, float | None] | None = {name: getattr(parsed, name) for name in TECH_SCALAR_FIELDS}
    except AttributeError:
        logger.exception("Unexpected error extracting tech scalars from a %s file", parser.key)
        scalars = None

    try:
        return finalize_profile(profile), scalars
    except Exception:
        logger.exception("Unexpected error extracting a profile from a %s file", parser.key)
        return None, scalars


async def store_tech_scalars(
    db: AsyncSession, *, dive_id: int, scalars: dict[str, float | None], commit: bool = False
) -> None:
    """Write a dive's parsed tech scalars, in the caller's transaction.

    `commit=False` by default for the same reason as `store_profile`: `store_dive_file`
    writes the file, the profile and these in one transaction, so a dive can never end up
    describing an export it doesn't have.
    """
    await db.execute(update(Dive).where(Dive.id == dive_id).values(**scalars))
    if commit:
        await db.commit()


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
        #
        # The tech scalars ride that same version gate, having none of their own, which
        # makes a scalar-only parser fix invisible here: correcting a CNS or surface-
        # pressure reading without touching the profile leaves `should_extract` saying
        # "current" and the stale numbers in place. `backfill_tech_fields` is what picks
        # those up - it re-reads every candidate on every run precisely so it needs no
        # version to bump - so a fix of that shape ships with a backfill run, not with a
        # re-upload.
        if should_extract(await get_existing_profile(db, dive_id=dive_id), sha256=digest) == "extract":
            await _release_read_transaction(db)
            profile, scalars = await run_in_threadpool(_extract_all, parser, data)
            try:
                if profile is not None:
                    await store_profile(
                        db, dive_id=dive_id, profile=profile, source_sha256=digest, parser_key=parser.key, commit=False
                    )
                if scalars is not None:
                    await store_tech_scalars(db, dive_id=dive_id, scalars=scalars, commit=False)
                # One commit for both, where the profile used to commit on its own: they
                # come out of the same bytes, and a dive whose exposure readings were
                # upgraded but whose profile wasn't would be describing two different
                # extractions.
                #
                # The writes are inside the `try`, not just the commit: a `CHECK` is not
                # deferrable in Postgres, so it is evaluated as the `UPDATE` runs and
                # `IntegrityError` is raised from `execute()` rather than from `commit()`.
                # A handler wrapped around the commit alone would never see one.
                await db.commit()
            except IntegrityError:
                # This branch is an opportunistic upgrade of a file the dive already has,
                # so failing it must not fail the request: the caller re-uploaded bytes
                # that are already stored, and the correct answer to that is still "you
                # already have this". Rolled back explicitly - without it the session
                # stays in a failed transaction and the *next* statement on it dies
                # somewhere unrelated.
                logger.exception("Opportunistic re-extraction for dive %s could not be stored", dive_id)
                await db.rollback()

        # Re-uploading is the natural repair after a partial loss of the files volume, and
        # this is what makes it actually work: without it the row says "already stored",
        # the download 500s forever, and the server refuses the very bytes that would fix
        # it. Costs one `stat` on the normal path. The write is safe because the key is
        # derived from this row's uuid and these bytes' hash, so it can only ever recreate
        # the file this row already names.
        if not await blob_store.has(existing.storage_key):
            logger.warning("Rewriting the missing stored file for dive %s from a re-upload", dive_id)
            await blob_store.put(existing.storage_key, data)

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
    profile, scalars = await run_in_threadpool(_extract_all, parser, data)

    # The file lands on the volume *before* the transaction that references it, and the
    # replaced one is unlinked *after* that transaction commits. Every database-visible
    # state therefore names bytes that exist; the only thing a crash between the two can
    # produce is an unreferenced file, which is harmless until the sweeper reclaims it.
    #
    # The uuid is minted here rather than left to the INSERT so the key can carry it: a key
    # that embeds a never-reused row uuid can never be re-minted after being retired, which
    # is what makes the post-commit unlink below safe against a concurrent re-upload of the
    # same bytes without any locking.
    row_uuid = uuid7()
    storage_key = blob_store.build_key(KEY_KIND, row_uuid=row_uuid, sha256=digest)
    await blob_store.put(storage_key, data)

    try:
        # Replacement, not versioning: whatever this dive had is gone. Runs before the
        # insert because `ux_dive_file_dive_id` is checked per statement, so two rows
        # for one dive must not coexist even momentarily.
        replaced = list(
            (
                await db.execute(delete(DiveFile).where(DiveFile.dive_id == dive_id).returning(DiveFile.storage_key))
            ).scalars()
        )
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
                storage_key=storage_key,
                # Spelled out rather than left to `PublicUUIDMixin`'s `default_factory`:
                # that is a dataclass-level default applied when the ORM constructs an
                # instance, and this Core-level INSERT never constructs one. Without it
                # Postgres gets a NULL and rejects the row - and here it also has to match
                # the uuid `storage_key` was built from.
                uuid=row_uuid,
                created_at=now,
            )
            .returning(DiveFile.uuid, DiveFile.updated_at)
        )
        row = result.one()
        if profile is not None:
            await store_profile(
                db, dive_id=dive_id, profile=profile, source_sha256=digest, parser_key=parser.key, commit=False
            )
        # Unconditional, and unlike the no-op branch above a *failed* extraction clears
        # them too. There the "couldn't read" / "says nothing" distinction is worth
        # keeping, because the file those readings came from is still attached and a
        # later backfill can re-read it. Here it is gone: the `DELETE` above replaced it,
        # so keeping its CNS and OTU leaves exactly the numbers-nothing-can-re-derive
        # state `delete_dive_file` clears them to avoid, now attributed to an export the
        # dive no longer has. Same rule the profile above already follows.
        await store_tech_scalars(
            db,
            dive_id=dive_id,
            scalars=scalars if scalars is not None else dict.fromkeys(TECH_SCALAR_FIELDS),
            commit=False,
        )
        # Registered before the commit, because the hook that runs it fires *on* the
        # commit; a rollback below clears the registration instead.
        blob_store.delete_after_commit(db, replaced)
        await db.commit()
    except IntegrityError as exc:
        # Two uploads for the same dive raced between the delete and the insert. One
        # user per dive and a button disabled while in flight make this vanishingly
        # rare; a retry is a better answer than a lock on the hot path.
        #
        # The file written above is left on the volume: no row references it, so it is an
        # orphan for the sweeper. Unlinking it here would be wrong on a retry that lands on
        # the same bytes - the retry mints a *new* uuid and so a new key, but a crash
        # between the two leaves nothing worse than the same orphan either way.
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

    `None` means *this dive has no row*. A row whose file is missing from the volume raises
    `blob_store.BlobMissingError` instead, and each caller decides how loudly to fail: the
    download route 500s, the export archive skips the member, the two backfills count it
    failed. Collapsing the two into `None` would report data loss as a 404.

    Explicit columns and no `db.expunge` any more: the row this reads is four short
    strings, so there is no longer a materialized blob sitting in the session's identity
    map for the rest of a backfill run - see *"`load_dive_file` detaches the row it read"*
    in `DECISIONS.md` for the problem that used to solve.
    """
    stmt = select(
        DiveFile.storage_key,
        DiveFile.content_type,
        DiveFile.original_filename,
        DiveFile.sha256,
    ).where(DiveFile.dive_id == dive_id)
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        return None

    return LoadedDiveFile(
        data=await blob_store.get(row.storage_key),
        content_type=row.content_type,
        original_filename=row.original_filename,
        sha256=row.sha256,
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

    Hard, not soft, and not merely unlinked: a row nothing can reach keeps its file on the
    volume forever, and a "delete" that only hides the file would be a worse trade than
    losing it from the corpus. The file itself goes after the caller's transaction commits
    - the mirror of the write ordering in `store_dive_file`.

    Takes the dive's extracted profile with it. Nothing cascades from removing the file
    (the profile's FK is to `dive`, not to `dive_file`), and a profile whose source export
    is gone can never be re-derived or checked against anything.

    The tech scalars go the same way, for the same reason - they are readings off the
    export rather than something the diver typed, so leaving CNS and OTU behind on a dive
    with no file would strand numbers nothing can re-derive or check. The *mixtures* are
    pointedly not cleared alongside them: those went through the form, the diver may have
    edited them since, and they are the dive's own record rather than the file's.
    """
    await delete_profile_for_dive(db, dive_id=dive_id, commit=False)
    await store_tech_scalars(db, dive_id=dive_id, scalars=dict.fromkeys(TECH_SCALAR_FIELDS), commit=False)
    keys = list(
        (
            await db.execute(delete(DiveFile).where(DiveFile.dive_id == dive_id).returning(DiveFile.storage_key))
        ).scalars()
    )
    blob_store.delete_after_commit(db, keys)
    if commit:
        await db.commit()
    return bool(keys)


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


@dataclass(frozen=True, slots=True)
class TechBackfillReport:
    """What one run of `backfill_tech_fields` did.

    Dives and mixtures are counted separately because they are backfilled on different
    terms - the dive's scalars are overwritten from the file outright, the mixture fields
    are applied only where the stored rows still demonstrably describe the parsed ones -
    so one number could not say whether a run went well. `mixtures_skipped` in particular
    is the interesting count: it is the diver having edited their cylinders since the
    import, which is a reason not to touch them rather than a failure.
    """

    examined: int = 0
    dives_updated: int = 0
    mixtures_updated: int = 0
    mixtures_skipped: int = 0
    failed: int = 0


def merge_mixture_fields(
    parsed: list[DiveMixtureSchema], stored: list[DiveMixtureRead]
) -> list[tuple[int, dict[str, object]]] | None:
    """Line parsed mixtures up with stored ones, or refuse to.

    `(id, values)` per row to update, or `None` when the two lists can't be shown to
    describe the same cylinders. Pure and DB-free, following the `reconcile()` idiom in
    this module - the decision worth testing is testable without a database. A row the
    file says nothing about is absent from the result rather than present with an empty
    dict, so `[]` is a legitimate answer meaning "matched, nothing to write".

    Position is the only available join: mixtures are replaced wholesale on every save
    (`crud_dive_mixtures.replace_mixtures_for_dive`), so a stored row's `id` is newer than
    the import and says nothing about which parsed cylinder it came from. Position alone
    is too weak to trust on its own, though - a diver who deleted their deco bottle and
    added a different one would have the parsed second gas written onto it. So the counts
    must match **and** every pair must still agree on `(oxygen, helium)`, which is the
    part of a cylinder a diver has no reason to retype and every reason to leave alone.

    Because the join is positional, **`stored` must be in the order the cylinders were
    saved in**, which is what `get_mixtures_for_dive`'s `ORDER BY id` guarantees and
    nothing in this function can check. The `(oxygen, helium)` agreement above is not a
    backstop for a mis-ordered list either: a parser that records no fractions at all
    leaves both `None` on every row, and `None` is explicitly not evidence of a mismatch
    (below). A 2026 Suunto Ocean export is exactly that shape - `_mixtures_from_cylinders`
    reconstructs its cylinders from sample data, which carries pressures and gas numbers
    but no `Gases` block - so on the one format whose `gas_number` is the file's own label
    rather than a synthesized position, an unordered read would swap the labels with
    nothing to catch it.

    All-or-nothing per dive, not per row: a list that half-matches is a list that has been
    edited, and half-applying to it would leave a set of cylinders that came from two
    different places with nothing recording which is which.
    """
    if len(parsed) != len(stored) or not parsed:
        return None

    updates: list[tuple[int, dict[str, object]]] = []
    for parsed_mix, stored_mix in zip(parsed, stored, strict=True):
        # `None` on the parsed side means the file never recorded a fraction, which
        # cannot be checked against the default the form filled in - so it is not
        # evidence of a mismatch, and not evidence of a match either. Only recorded
        # fractions are compared.
        if parsed_mix.oxygen is not None and parsed_mix.oxygen != stored_mix.oxygen:
            return None
        if parsed_mix.helium is not None and parsed_mix.helium != stored_mix.helium:
            return None
        # **Fill-only: a parsed `None` is dropped, not written.** This is the same rule as
        # the fraction comparison above, applied to the write instead of the guard.
        # `DiveMixtureSchema`'s whole premise is that `None` means "the file did not record
        # this" rather than "this is nothing" - so spreading one into an `UPDATE` turns the
        # absence of a reading into a value, which is the exact conflation that schema
        # exists to prevent.
        #
        # It is also silent data loss, because all three of these are client-writable
        # (`DiveMixtureBase` -> `DiveMixtureCreate`, and `PATCH /dive/{uuid}` replaces
        # mixtures wholesale). A FIT import produces `po2_limit=None` always and
        # `role=None` for any open-circuit gas; a diver who then sets 1.6 and `deco` on
        # their stage bottle has touched neither fraction, so the guard above still admits
        # the join and the backfill would have written both back to `NULL`.
        #
        # The dive's own scalars are overwritten outright a few lines up, and the asymmetry
        # is the point: nothing but the import writes those, so there is no edit to lose.
        # These three have another writer.
        #
        # A parser *correction* still lands, which is what the fill-only rule costs and
        # doesn't: where the file records a value the backfill overwrites as before, and it
        # declines only where the file has nothing to say.
        # Annotated rather than inferred: `dict` is invariant in its value type, so the
        # comprehension's own `dict[str, float | int | GasRole]` is not a `dict[str, object]`.
        values: dict[str, object] = {
            name: value
            for name, value in (
                ("po2_limit", parsed_mix.po2_limit),
                ("gas_number", parsed_mix.gas_number),
                ("role", parsed_mix.role),
            )
            if value is not None
        }
        if values:
            updates.append((stored_mix.id, values))
    return updates


# How many dives are processed between commits. Mirrors `_BACKFILL_BATCH_SIZE` in
# `services/dive_profiles.py`, for the same trade: small enough that an interrupted run
# loses little, large enough that a few hundred dives isn't a few hundred transactions.
_BACKFILL_BATCH_SIZE = 50


async def backfill_tech_fields(
    db: AsyncSession,
    *,
    parser_key: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> TechBackfillReport:
    """Re-read the exports already stored against dives for the fields Phase 2 added.

    A second script rather than an extension of `backfill_profiles`, because the two
    select on different things and stop on different terms. That one is keyed to
    `PROFILE_EXTRACTOR_VERSION` and skips a dive whose profile is already current; these
    columns have no version of their own, and every candidate is re-read every run - which
    is cheap enough (a header parse, not a sample stream) and is what makes it correct to
    run again after a parser fix without a version to bump.

    Idempotent, and safe to run repeatedly. The dive's own scalars are overwritten from
    the file outright: they are the import's to own, no other path writes them, and a
    re-run simply writes the same numbers. The mixture fields are best-effort - see
    `merge_mixture_fields` for why a dive whose cylinders have been edited is skipped
    rather than reconciled.
    """
    # Imported here rather than at module scope, matching `backfill_profiles`: the crud
    # module is not otherwise part of this module's dependency surface, and keeping the
    # import next to its one use says so.
    from ..crud.crud_dive_mixtures import get_mixtures_for_dive
    from .cache_invalidation import invalidate_dive_caches

    stmt = select(DiveFile.dive_id, DiveFile.parser_key, DiveFile.user_id).order_by(DiveFile.dive_id)
    if parser_key is not None:
        stmt = stmt.where(DiveFile.parser_key == parser_key)
    if limit is not None:
        stmt = stmt.limit(limit)

    candidates = list(await db.execute(stmt))
    examined = dives_updated = mixtures_updated = mixtures_skipped = failed = 0
    # Dives written since the last commit, rather than a position in `candidates`. An
    # index-modulo test sits below several `continue`s, so a dive that failed on exactly
    # the boundary skipped that commit and left up to two batches riding on the next one.
    pending = 0
    touched_user_ids: set[int] = set()

    for row in candidates:
        examined += 1

        parser = PARSER_BY_KEY.get(row.parser_key)
        if parser is None:
            logger.warning("Skipping dive %s: unknown parser key %r", row.dive_id, row.parser_key)
            failed += 1
            continue

        try:
            file = await load_dive_file(db, dive_id=row.dive_id)
        except blob_store.BlobMissingError:
            # The row is there and its file is not, which is data loss or an unmounted
            # volume rather than a race. Counted as a failure and the run continues, on the
            # same terms as the parse failure below: a run that stopped here would report
            # less than one that finished and said how many dives are in this state.
            logger.error("Skipping dive %s: its stored file is missing from the volume", row.dive_id)
            failed += 1
            continue
        if file is None:
            logger.warning("Skipping dive %s: its stored file vanished mid-run", row.dive_id)
            failed += 1
            continue

        try:
            parsed = parser.parse(file.data)
            # Inside the `try` rather than below it, matching `extract_tech_scalars`: a
            # name in `TECH_SCALAR_FIELDS` that `ParsedDiveSchema` doesn't carry raises
            # `AttributeError` here, and one dive that can't be read is not a reason to
            # abandon the other 900. `test_covers_exactly_the_columns_the_read_schema_
            # publishes` is what actually stops the two drifting; this just means the
            # drift is reported per dive instead of killing the run.
            scalars = {name: getattr(parsed, name) for name in TECH_SCALAR_FIELDS}
        except DiveParseError, UnsupportedDiveFileError, AttributeError:
            # Counted rather than swallowed. A file that parsed at import time and does
            # not now is a parser regression, and a run that reported only successes
            # would hide it - the same reasoning as `BackfillReport`'s five counts.
            # Worded for all three: an `AttributeError` here is schema drift, not a file
            # that stopped parsing, and a message naming only the latter would send
            # whoever reads the log looking at the wrong thing.
            logger.warning(
                "Skipping dive %s: its %s export could not be read", row.dive_id, row.parser_key, exc_info=True
            )
            failed += 1
            continue

        stored = await get_mixtures_for_dive(db, row.dive_id)
        updates = merge_mixture_fields(parsed.mixtures, stored)
        if updates is None:
            # `max`, not `len(stored)`: the count mismatch that refuses the merge includes
            # the diver having deleted every cylinder, and `len(stored)` is 0 there - so
            # the run reported "nothing skipped" for precisely the dive whose cylinders
            # were edited most. Whichever side has rows is what went unwritten.
            mixtures_skipped += max(len(stored), len(parsed.mixtures))

        if dry_run:
            dives_updated += 1
            mixtures_updated += len(updates or [])
            continue

        try:
            # A savepoint, so a dive the database rejects costs only that dive. Without it
            # the failure propagates out of this function and the enclosing `async with
            # local_session()` rolls back every uncommitted dive since the last batch
            # commit - and because nothing here advances a version column, the next run
            # reaches the same dive and dies the same way. The backfill could then never
            # get past it without hand-narrowing `--parser-key`.
            async with db.begin_nested():
                await store_tech_scalars(db, dive_id=row.dive_id, scalars=scalars, commit=False)
                for mixture_id, values in updates or []:
                    await db.execute(update(DiveMixture).where(DiveMixture.id == mixture_id).values(**values))
        except IntegrityError:
            # A parsed value the schema let through and the database won't take: a parser
            # unit bug, and the file that proves it is still attached to the dive. Counted
            # rather than raised, for the same reason as the parse failure above - a run
            # that stopped on it would report less than one that finished and said so.
            logger.warning("Skipping dive %s: its parsed values violate a constraint", row.dive_id, exc_info=True)
            failed += 1
            continue

        dives_updated += 1
        mixtures_updated += len(updates or [])
        touched_user_ids.add(row.user_id)

        pending += 1
        if pending >= _BACKFILL_BATCH_SIZE:
            await db.commit()
            pending = 0

    if not dry_run:
        await db.commit()
        # Cached dive reads embed both the scalars and the mixtures, so every dive this
        # run touched is now serving stale values. See the script for why this needs a
        # live Redis pool.
        for user_id in touched_user_ids:
            await invalidate_dive_caches(user_id)

    return TechBackfillReport(
        examined=examined,
        dives_updated=dives_updated,
        mixtures_updated=mixtures_updated,
        mixtures_skipped=mixtures_skipped,
        failed=failed,
    )
