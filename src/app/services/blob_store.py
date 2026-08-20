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
import uuid as uuid_pkg
from collections.abc import Iterator
from pathlib import Path

import anyio.to_thread
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

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


def build_key(kind: str, *, row_uuid: uuid_pkg.UUID, sha256: str) -> str:
    """The key one row's payload is stored under: `{kind}/{sha256[:2]}/{uuid}_{sha256}`.

    Both halves earn their place.

    The **row uuid** makes a retired key unrepeatable, which is what lets a post-commit
    unlink be safe without a lock. Keyed on content alone, deleting a card while the same
    photo is concurrently re-uploaded would have the delete's unlink destroy the blob the
    re-upload just wrote - a committed row pointing at nothing, i.e. data loss rather than
    an orphan. Row uuids are never reused, so an unlink can only ever name a file no live
    row references. It also removes the sweeper's TOCTOU: a key the sweep walked cannot be
    re-minted while it deliberates.

    The **content hash** keeps writes idempotent (a retried write of the same row and
    content lands byte-identically on the same path), keeps blobs immutable (replacing a
    card mints a new key, so a reader mid-replacement can never get new bytes under old
    metadata), and lets an operator verify any file in the tree with `sha256sum`.

    Sharded on the hash prefix rather than the uuid's: `PublicUUIDMixin` uses uuid7, whose
    leading hex is a millisecond timestamp, so every key minted in one month would land in
    a handful of directories. sha256's first byte is uniformly random. One level of 256 is
    plenty for a corpus of thousands - the same fanout git and the OCI registry use.
    """
    return f"{kind}/{sha256[:2]}/{row_uuid}_{sha256}"


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
    """
    root = storage_root()
    tmp = root / TMP_DIRNAME
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        probe = tmp / ".writable"
        probe.write_bytes(b"")
        probe.unlink()
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


def delete_after_commit(db: AsyncSession, keys: str | list[str]) -> None:
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
