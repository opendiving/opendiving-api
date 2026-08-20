"""The files volume: the only module in the app that touches the filesystem.

One level down from the seams `services/dive_files.py` and
`services/certification_files.py` document. Those two remain the only modules that know
*what* is stored and against which row; this one is the only module that knows *where and
how*. Callers hand over an opaque `key` and bytes, and never see a `Path` - which is what
makes the S3 backend a rewrite of this file rather than a change to every call site.

Deliberately no abstract `BlobStore` base class and no `FILE_STORAGE_BACKEND` setting:
there is one implementation, and a setting with one valid value is a lie about choice.
Same rule `services/certification_files.py` already states - the module boundary is the
seam.

Whole `bytes` rather than streams, because every caller already holds the whole payload:
`read_upload_within_limit` buffers at most 10 MB, and the routes, the export archive and
the backfills all read the entire file. A streaming interface is photo-era work, added
when there is a caller that can use it.
"""

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import anyio.to_thread
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from ..core.config import settings

logger = logging.getLogger(__name__)

# Where `put`'s temp files live, relative to the storage root. **Inside** the root, not
# `/tmp`: `os.replace` raises `EXDEV` across filesystems, and in a container the overlayfs
# root and a mounted volume are always different filesystems. A temp dir on the same
# volume is what makes the rename atomic instead of a cross-device copy that can be
# interrupted halfway.
TMP_DIRNAME = "tmp"

# Key on the session's `info` dict under which `delete_after_commit` parks keys until the
# transaction it rode in on commits.
_PENDING_DELETES = "blob_store_pending_deletes"


class BlobMissingError(Exception):
    """A row names a key the volume does not have.

    Never a "not found" in the API sense - the row's existence is the claim that the bytes
    exist. This is data loss or an unmounted volume, and each caller decides how loudly to
    fail: the download routes 500, the export archive skips the member, the backfills count
    it as a failure.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"No stored file for key {key!r}")


def new_key(kind: str, *, sha256: str) -> str:
    """Mint a key for one write: `{kind}/{sha256[:2]}/{uuid7}_{sha256}`.

    **Deliberately not a pure function.** Every call returns a different key for the same
    arguments, and that is the entire point: the uuid is a nonce, minted fresh per write and
    never reused, which is what makes the post-commit unlink in `delete_after_commit` safe
    without any locking. A key that has been retired can never be minted again, so an unlink
    scheduled for it cannot destroy a file some concurrent write has since put there.

    It used to take the owning row's uuid instead, which was wrong in a way worth recording
    because it *looked* right. For dive files it happened to be safe - each content change
    inserts a fresh row, so the row uuid was already per-write - but a card's row survives
    replacement by design (`ON CONFLICT DO UPDATE` preserves its uuid), so its key was really
    keyed on (slot, content) and *was* re-mintable. Replace a card while a concurrent request
    re-uploads the photo being replaced, and the replacement's unlink deletes the file the
    re-upload just wrote, leaving a committed row pointing at nothing. Minting here removes
    the chance to get that wrong at a call site.

    What it costs: re-uploading a card's existing bytes now writes a new file and retires the
    old one rather than landing byte-identically on the same path. Cheap, and the row's
    `updated_at` moved on that request anyway.

    The **content hash** still earns its place - it lets an operator verify any file in the
    tree with `sha256sum`, and it keeps blobs immutable, since new content always means a new
    key and a reader mid-replacement can never be handed new bytes under old metadata.

    Sharded on the hash prefix rather than the uuid's: uuid7's leading hex is a millisecond
    timestamp, so uuid-sharding would put every key minted in one month in a handful of
    directories. sha256's first byte is uniformly random. One level of 256 is plenty for a
    corpus of thousands - the same fanout git and the OCI registry use.
    """
    return f"{kind}/{sha256[:2]}/{uuid7()}_{sha256}"


def storage_root() -> Path:
    """Read at call time rather than captured at import, so tests can repoint it."""
    return Path(settings.FILE_STORAGE_DIR)


def _path_for(key: str) -> Path:
    return storage_root() / key


def ensure_root_writable() -> None:
    """Create the storage root and its temp dir, and prove we can write into them.

    Called from the lifespan **before** migrations, because the revision that moves the
    bytes out of Postgres writes files itself. Fail-fast beats four gunicorn workers each
    discovering an unwritable volume on their first upload, hours later, one diver at a
    time.

    Those same four workers are why the probe carries the pid and tolerates an already-gone
    file. The lifespan runs once *per worker* - that is what `apply_migrations` takes an
    advisory lock for - so all four reach this within milliseconds of each other at
    container start. On a shared filename the second worker to finish unlinks a file the
    first already took, raising `FileNotFoundError` - an `OSError` - which this would turn
    into a fatal "not writable" against a volume that is perfectly healthy. A flaky startup
    failure accusing the wrong thing is worse than no check at all. `_write_atomically`
    below already names its temp files per-pid, for the same reason.
    """
    root = storage_root()
    tmp = root / TMP_DIRNAME
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        probe = tmp / f".writable-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"The files volume at {root} is not writable ({exc}). Uploaded dive-computer exports "
            f"and c-card images live there. Check that the volume is mounted and owned by the "
            f"container's user (uid 1000), or point FILE_STORAGE_DIR somewhere writable."
        ) from exc


def _write_atomically(key: str, data: bytes) -> None:
    """Durable, atomic write of one blob. Runs in a worker thread.

    Write to a temp file, `fsync` it, `os.replace` into place, then `fsync` the parent
    directory - the last step being what makes the *rename* durable rather than merely the
    contents. `os.replace` is atomic on POSIX, so four workers writing the same key
    concurrently is safe: keys embed the content hash, so they are writing identical bytes.
    """
    root = storage_root()
    destination = root / key
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = root / TMP_DIRNAME
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # `os.getpid()` and `id(data)` rather than a random name: the file is renamed away
    # immediately, and a collision only has to be impossible *within* one process's
    # concurrent writes.
    tmp_path = tmp_dir / f".{os.getpid()}-{id(data):x}.part"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, destination)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    dir_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


async def put(key: str, data: bytes) -> None:
    """Store bytes under `key`, atomically and durably.

    Idempotent by construction: keys embed the content hash, so re-`put`ting the same key
    writes byte-identical content over itself.
    """
    await anyio.to_thread.run_sync(_write_atomically, key, data)


def _read(key: str) -> bytes:
    try:
        return _path_for(key).read_bytes()
    except FileNotFoundError as exc:
        raise BlobMissingError(key) from exc
    except IsADirectoryError as exc:
        # A key that names a shard directory rather than a file. Same operational meaning
        # as a missing file, and the caller has one exception to catch either way.
        raise BlobMissingError(key) from exc


async def get(key: str) -> bytes:
    """Fetch the bytes stored under `key`, or raise `BlobMissingError`."""
    return await anyio.to_thread.run_sync(_read, key)


def _unlink(key: str) -> None:
    _path_for(key).unlink(missing_ok=True)


async def delete(key: str) -> None:
    """Remove `key`. Tolerates an already-absent file, so retries are free."""
    await anyio.to_thread.run_sync(_unlink, key)


def exists(key: str) -> bool:
    """Whether `key` has bytes on the volume. Synchronous and cheap (one `stat`)."""
    return _path_for(key).is_file()


async def has(key: str) -> bool:
    """`exists`, off the event loop - for the request path."""
    return await anyio.to_thread.run_sync(exists, key)


def delete_after_commit(db: AsyncSession | Session, keys: str | list[str]) -> None:
    """Schedule an unlink for when the session's *current* transaction commits.

    This is what keeps every existing `commit: bool = False` composition working
    unchanged. `erase_certification` soft-deletes the row and hard-deletes its files in one
    caller-owned transaction, so the unlink has to ride *that* commit rather than an inner
    one - and must not happen at all if the caller rolls back.

    Only appends to the session's `info` dict; the listeners registered below do the
    unlinking. That is deliberate rather than incidental: much of the suite drives these
    services with `AsyncMock` sessions, which never commit, so they never fire the hook -
    while the Postgres-backed tests exercise it for real.

    Failures are logged, not raised: the row is already gone, the transaction has already
    committed, and the leftover file is an orphan the sweeper reclaims
    (`src/scripts/sweep_orphaned_files.py`).

    **Call this immediately after the statement that retired the rows**, not before it.
    SQLAlchemy autobegins on the first statement, so a `rollback()` on a session with no
    transaction open fires no event and would leave the registration standing for whatever
    that session committed next. Every caller here registers straight after its `DELETE`
    (or, in the cert upsert, its `INSERT ... ON CONFLICT`), which is what makes the
    rollback path clear itself.

    Takes either kind of session because it only ever touches `info`, and the listeners are
    on the sync `Session` in both cases - `AsyncSession.info` is a proxy to exactly that
    dict. Nothing in the app registers from a sync session; the tests do.
    """
    if isinstance(keys, str):
        keys = [keys]
    if not keys:
        return
    pending = db.info.setdefault(_PENDING_DELETES, [])
    pending.extend(keys)


def _take_pending(session: Session) -> list[str]:
    keys: list[str] = session.info.pop(_PENDING_DELETES, [])
    return keys


@event.listens_for(Session, "after_commit")
def _unlink_committed_blobs(session: Session) -> None:
    """Unlink what the committed transaction retired.

    Registered against the `Session` class rather than one factory, so it covers the
    request sessions, the script sessions and the test ones alike - and synchronously,
    because SQLAlchemy's session events are sync even under `AsyncSession`. The unlinks are
    a handful of `unlink` syscalls on a local volume, on a path that has just done a
    database round trip; a thread hop to save microseconds would buy an ordering problem.
    """
    for key in _take_pending(session):
        try:
            (Path(settings.FILE_STORAGE_DIR) / key).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not unlink stored file %r after commit; the sweeper will reclaim it", key)


@event.listens_for(Session, "after_rollback")
@event.listens_for(Session, "after_soft_rollback")
def _forget_rolled_back_blobs(session: Session, *args: object) -> None:
    """Drop the pending unlinks when the transaction that scheduled them is rolled back.

    `after_soft_rollback` as well as `after_rollback`, because a rollback with nothing
    flushed to the database emits only the former - and the rows those keys belonged to are
    still there.
    """
    _take_pending(session)


def iter_keys() -> Iterator[str]:
    """Every stored key on the volume, for the sweeper. Skips the temp dir.

    Yields keys (`{kind}/{shard}/{name}`), not paths, because a key is what the database
    columns hold and what the caller diffs against.
    """
    root = storage_root()
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts[0] == TMP_DIRNAME:
            continue
        yield relative.as_posix()


def stat_mtime(key: str) -> float | None:
    """The blob's mtime as a POSIX timestamp, or `None` if it is gone. Sweeper only."""
    try:
        return _path_for(key).stat().st_mtime
    except OSError:
        return None
