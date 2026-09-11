"""Where stored bytes live: the only module in the app that knows.

One level down from the seams `services/dive_files.py` and
`services/certification_files.py` document. Those two remain the only modules that know
*what* is stored and against which row; this one is the only module that knows *where and
how*. Callers hand over an opaque `key` and bytes, and never see a `Path` or a bucket -
which is what made the second backend a rewrite of this file rather than a change to every
call site.

There are two backends now, chosen by `FILE_STORAGE_BACKEND`:

- `local` (the default) - ordinary files under `FILE_STORAGE_DIR`, which is what every
  compose install mounts a volume at. Nothing about that path changed.
- `s3` - any S3-compatible object store, configured by the `S3_*` group. Written for a
  hosted instance on a platform whose disks attach to one service at a time, which the
  api and the worker both need.

The file used to say there would deliberately never be a `FILE_STORAGE_BACKEND` setting,
because a setting with one valid value is a lie about choice. That was right while there
was one implementation and it named its own expiry condition; `DECISIONS.md`, *"A second
backend, because the disk stopped being shared"*, records the condition firing. What
carried over intact is the shape the old note predicted would take a second backend:
opaque string keys that are also valid S3 object keys, bytes in and bytes out, and no
caller anywhere holding a `Path`. Keys are identical on both backends, so an install can
move either way and back (`src/scripts/migrate_blobs.py`).

Whole `bytes` rather than streams, because every caller already holds the whole payload:
`read_upload_within_limit` buffers at most 10 MB, and the routes, the export archive and
the backfills all read the entire file. A streaming interface is photo-era work, added
when there is a caller that can use it.
"""

import asyncio
import hashlib
import logging
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import anyio.to_thread
import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from ..core.config import FileStorageBackendOption, missing_s3_settings, settings

logger = logging.getLogger(__name__)

# Where the local backend's temp files live, relative to the storage root. **Inside** the
# root, not `/tmp`: `os.replace` raises `EXDEV` across filesystems, and in a container the
# overlayfs root and a mounted volume are always different filesystems. A temp dir on the
# same volume is what makes the rename atomic instead of a cross-device copy that can be
# interrupted halfway.
#
# The S3 backend has no equivalent - a `PutObject` is atomic on its own - but it does put
# its startup probe under this prefix, so that both backends agree on the one name that
# `iter_keys` hides from the sweeper.
TMP_DIRNAME = "tmp"

# Key on the session's `info` dict under which `delete_after_commit` parks keys until the
# transaction it rode in on commits.
_PENDING_DELETES = "blob_store_pending_deletes"

# `DeleteObjects` takes at most 1000 keys per request (the S3 API's own limit), and the
# account purge hands over every blob one diver ever uploaded.
_S3_DELETE_BATCH = 1000

# Post-commit removals that were handed to a thread and have not come back. Only ever
# touched from the event-loop thread. The suite awaits these; nothing in the app does -
# see `_unlink_committed_blobs`.
_pending_removals: set[asyncio.Future[None]] = set()


class BlobMissingError(Exception):
    """A row names a key the store does not have.

    Never a "not found" in the API sense - the row's existence is the claim that the bytes
    exist. This is data loss, an unmounted volume or the wrong bucket, and each caller
    decides how loudly to fail: the download routes 500, the export archive skips the
    member, the backfills count it as a failure.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"No stored file for key {key!r}")


def new_key(kind: str, *, sha256: str) -> str:
    """Mint a key for one write: `{kind}/{sha256[:2]}/{uuid7}_{sha256}`.

    Backend-independent on purpose, and the reason an install can switch between the two
    and back: the same string is a relative path under `FILE_STORAGE_DIR` and an object key
    in a bucket, so nothing has to be rewritten or remapped by a move.

    **Deliberately not a pure function.** Every call returns a different key for the same
    arguments, and that is the entire point: the uuid is a nonce, minted fresh per write and
    never reused, which is what makes the post-commit delete in `delete_after_commit` safe
    without any locking. A key that has been retired can never be minted again, so a delete
    scheduled for it cannot destroy a file some concurrent write has since put there.

    It used to take the owning row's uuid instead, which was wrong in a way worth recording
    because it *looked* right. For dive files it happened to be safe - each content change
    inserts a fresh row, so the row uuid was already per-write - but a card's row survives
    replacement by design (`ON CONFLICT DO UPDATE` preserves its uuid), so its key was really
    keyed on (slot, content) and *was* re-mintable. Replace a card while a concurrent request
    re-uploads the photo being replaced, and the replacement's delete destroys the file the
    re-upload just wrote, leaving a committed row pointing at nothing. Minting here removes
    the chance to get that wrong at a call site.

    What it costs: re-uploading a card's existing bytes now writes a new file and retires the
    old one rather than landing byte-identically on the same path. Cheap, and the row's
    `updated_at` moved on that request anyway.

    The **content hash** still earns its place - it lets an operator verify any file in the
    tree with `sha256sum`, and it keeps blobs immutable, since new content always means a new
    key and a reader mid-replacement can never be handed new bytes under old metadata. On
    the object store it also makes `migrate_blobs.py` resumable: an object that is already
    there under a key cannot hold different bytes than the file that key names.

    Sharded on the hash prefix rather than the uuid's: uuid7's leading hex is a millisecond
    timestamp, so uuid-sharding would put every key minted in one month in a handful of
    directories. sha256's first byte is uniformly random. One level of 256 is plenty for a
    corpus of thousands - the same fanout git and the OCI registry use.
    """
    return f"{kind}/{sha256[:2]}/{uuid7()}_{sha256}"


class Backend(Protocol):
    """What a place to keep bytes has to be able to do.

    An interface at last, which the single-implementation era deliberately refused. It
    exists for one concrete caller as well as for the dispatch below: `migrate_blobs.py`
    holds *both* backends at once, which no setting can express.

    Every method here is synchronous and blocking. The `async` functions further down are
    the only boundary, and they all hop to a thread - which is what keeps an S3 round trip
    off the event loop just as it keeps the local backend's `fsync` off it.
    """

    #: Whether a delete is a network round trip rather than a syscall. Read by the
    #: post-commit hook, which is synchronous and cannot afford to block a loop on a
    #: request that has already committed.
    removal_is_remote: bool

    def describe(self) -> str:
        """Where this backend keeps things, for a startup log line or a failure message."""
        ...

    def ensure_ready(self) -> None: ...
    def write(self, key: str, data: bytes) -> None: ...
    def read(self, key: str) -> bytes: ...
    def remove(self, key: str) -> None: ...
    def discard_many(self, keys: list[str]) -> None: ...
    def exists(self, key: str) -> bool: ...
    def iter_keys(self) -> Iterator[str]: ...
    def has_any_key(self) -> bool: ...
    def stat_mtime(self, key: str) -> float | None: ...


# -------------------- the local backend --------------------


def storage_root() -> Path:
    """Read at call time rather than captured at import, so tests can repoint it."""
    return Path(settings.FILE_STORAGE_DIR)


class LocalBackend:
    """Ordinary files under `FILE_STORAGE_DIR`. The default, and what every compose
    install mounts a named volume at."""

    removal_is_remote = False

    def describe(self) -> str:
        return f"the files volume at {storage_root()}"

    def _path_for(self, key: str) -> Path:
        return storage_root() / key

    def ensure_ready(self) -> None:
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
        failure accusing the wrong thing is worse than no check at all. `write` below already
        names its temp files per-pid, for the same reason.
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

    def write(self, key: str, data: bytes) -> None:
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

    def read(self, key: str) -> bytes:
        try:
            return self._path_for(key).read_bytes()
        except FileNotFoundError as exc:
            raise BlobMissingError(key) from exc
        except IsADirectoryError as exc:
            # A key that names a shard directory rather than a file. Same operational meaning
            # as a missing file, and the caller has one exception to catch either way.
            raise BlobMissingError(key) from exc

    def remove(self, key: str) -> None:
        self._path_for(key).unlink(missing_ok=True)

    def discard_many(self, keys: list[str]) -> None:
        for key in keys:
            try:
                self.remove(key)
            except OSError:
                logger.warning("Could not unlink stored file %r after commit; the sweeper will reclaim it", key)

    def exists(self, key: str) -> bool:
        return self._path_for(key).is_file()

    def iter_keys(self) -> Iterator[str]:
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

    def has_any_key(self) -> bool:
        return next(self.iter_keys(), None) is not None

    def stat_mtime(self, key: str) -> float | None:
        try:
            return self._path_for(key).stat().st_mtime
        except OSError:
            return None


# -------------------- the S3-compatible backend --------------------


def new_s3_client() -> Any:
    """Build the boto3 client the S3 backend talks through.

    Its own function so the suite can hand back a stub without a network or a live bucket,
    and so the four botocore knobs that matter sit in one place:

    - **`signature_version="s3v4"`.** Every S3-compatible store still in service speaks it;
      the legacy `s3` signature is what you get by accident against a store whose region is
      not a real AWS one.
    - **`request_checksum_calculation` / `response_checksum_validation` at
      `when_required`.** botocore 1.36 started sending a CRC32 trailer on every `PutObject`
      by default, which several S3-compatible stores rejected outright. The default costs
      nothing here - the key already carries the content's sha256, so integrity is checked
      by construction on the way back out.
    - **standard retries.** The default legacy mode retries fewer error classes; this is a
      network the request path now depends on.

    Region defaults to `auto`, which is what Cloudflare R2 wants and what every other store
    ignores, so an operator pointing at a real AWS bucket sets `S3_REGION` and everyone else
    leaves it alone.
    """
    secret = settings.S3_SECRET_ACCESS_KEY.get_secret_value() if settings.S3_SECRET_ACCESS_KEY else ""
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        region_name=settings.S3_REGION,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=secret,
        config=BotocoreConfig(
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


#: What an S3-compatible store answers with when the object or its prefix is not there.
#: `NoSuchKey` is `GetObject`'s; `404` and `NotFound` are what `HeadObject` gives, which
#: carries no error code of its own because a HEAD response has no body to put one in.
_S3_MISSING_CODES = frozenset({"NoSuchKey", "NotFound", "404"})


class S3Backend:
    """Any S3-compatible object store, addressed with plain `GetObject`/`PutObject`.

    Deliberately not a vendor SDK. The hosted instance runs on Cloudflare R2 and a
    self-hoster may point this at MinIO, Garage, Ceph, Backblaze or AWS itself; the six
    operations below are the intersection all of them implement, and none of them needs
    versioning, lifecycle rules or multipart.

    No local temp file and no rename: `PutObject` is atomic at the store, so the whole
    `EXDEV`/`fsync` dance the local backend needs has no counterpart here.
    """

    removal_is_remote = True

    def __init__(self, client: Any, *, bucket: str, prefix: str = "") -> None:
        self._client = client
        self._bucket = bucket
        # Normalized once: no leading slash (S3 keys do not start with one and a store that
        # tolerates it gives you an empty first path segment), exactly one trailing slash so
        # that concatenation is the whole of `_object_key`.
        stripped = prefix.strip().strip("/")
        self._prefix = f"{stripped}/" if stripped else ""

    def describe(self) -> str:
        where = f"{self._bucket}/{self._prefix}" if self._prefix else self._bucket
        return f"the object store bucket {where!r} at {settings.S3_ENDPOINT_URL}"

    def _object_key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _blob_key(self, object_key: str) -> str | None:
        """The blob key an object name carries, or `None` if it is not one of ours."""
        if not object_key.startswith(self._prefix):
            return None
        key = object_key[len(self._prefix) :]
        return key or None

    def ensure_ready(self) -> None:
        """Put a probe object and delete it, so bad credentials or a wrong bucket name fail
        at startup rather than on the first diver's upload.

        The counterpart of the local backend's writability check, and it exists for the same
        reason: the failure it catches is a configuration mistake whose natural symptom is a
        500 hours later. This one also covers what the local check never had to - a
        credential that reads but cannot write, and a bucket in someone else's account.

        Under the temp prefix `iter_keys` hides, and carrying the pid, so that four gunicorn
        workers probing within milliseconds of each other cannot collide and so that a probe
        leaked by a crash between the put and the delete is never mistaken for a blob.
        """
        probe = f"{TMP_DIRNAME}/.writable-{os.getpid()}-{uuid7()}"
        try:
            try:
                self.write(probe, b"")
            finally:
                self.remove(probe)
        except (ClientError, BotoCoreError) as exc:
            raise RuntimeError(
                f"Cannot write to {self.describe()} ({exc}). Uploaded dive-computer exports and "
                f"c-card images live there. Check S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY_ID and "
                f"S3_SECRET_ACCESS_KEY, that the bucket exists, and that the credential may write "
                f"to it - or set FILE_STORAGE_BACKEND=local to use a filesystem volume instead."
            ) from exc

    def write(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=self._object_key(key), Body=data)

    def read(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._object_key(key))
        except ClientError as exc:
            if _error_code(exc) in _S3_MISSING_CODES:
                raise BlobMissingError(key) from exc
            raise
        body = response["Body"]
        try:
            return bytes(body.read())
        finally:
            body.close()

    def remove(self, key: str) -> None:
        """`DeleteObject` is already idempotent - it succeeds on a key that was never there -
        so a retry and the post-commit delete of something the sweeper took both cost one
        request and nothing else."""
        self._client.delete_object(Bucket=self._bucket, Key=self._object_key(key))

    def discard_many(self, keys: list[str]) -> None:
        for batch in (keys[i : i + _S3_DELETE_BATCH] for i in range(0, len(keys), _S3_DELETE_BATCH)):
            try:
                response = self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": [{"Key": self._object_key(key)} for key in batch], "Quiet": True},
                )
            except ClientError, BotoCoreError:
                logger.warning(
                    "Could not delete %d stored object(s) after commit; the sweeper will reclaim them",
                    len(batch),
                    exc_info=True,
                )
                continue
            # `Quiet` suppresses the per-object success rows but never the failures, so
            # anything still in `Errors` is a delete that did not happen.
            for failure in response.get("Errors") or ():
                logger.warning(
                    "Could not delete stored object %r after commit (%s); the sweeper will reclaim it",
                    failure.get("Key"),
                    failure.get("Code"),
                )

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._object_key(key))
        except ClientError as exc:
            if _error_code(exc) in _S3_MISSING_CODES:
                return False
            raise
        return True

    def iter_keys(self) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix):
            for entry in page.get("Contents") or ():
                key = self._blob_key(entry["Key"])
                if key is None or key.split("/")[0] == TMP_DIRNAME:
                    continue
                yield key

    def has_any_key(self) -> bool:
        """One bounded request - two in one corner case - and never a listing.

        This runs on every boot (`core/setup.py`'s files-volume warning), and the local
        backend answers it by taking the first entry of a lazy walk. `MaxKeys=1` is the
        object store's version of the same restraint: a `list_objects_v2` with no bound
        would pull a thousand keys per page on an instance with a real corpus, every time
        a container starts.

        The corner case is the temp prefix, which the local backend's walk skips and a
        listing does not. If the one key that comes back is a startup probe that leaked -
        a crash between its `PutObject` and its `DeleteObject` - answering "not empty" here
        would silence the warning about a store that has lost its files. So a second
        single-key request asks past that prefix. `StartAfter` is `tmp0` rather than `tmp/`
        because `/` is 0x2F and `0` is 0x30: every key under `tmp/` sorts below `tmp0` and
        nothing else does.
        """
        first = self._client.list_objects_v2(Bucket=self._bucket, Prefix=self._prefix, MaxKeys=1)
        contents = first.get("Contents") or ()
        if not contents:
            return False

        key = self._blob_key(contents[0]["Key"])
        if key is not None and key.split("/")[0] != TMP_DIRNAME:
            return True

        beyond = self._client.list_objects_v2(
            Bucket=self._bucket,
            Prefix=self._prefix,
            MaxKeys=1,
            StartAfter=f"{self._prefix}{TMP_DIRNAME}0",
        )
        return bool(beyond.get("Contents"))

    def stat_mtime(self, key: str) -> float | None:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=self._object_key(key))
        except ClientError as exc:
            if _error_code(exc) in _S3_MISSING_CODES:
                return None
            raise
        modified = response.get("LastModified")
        return modified.timestamp() if isinstance(modified, datetime) else None


def _error_code(exc: ClientError) -> str:
    """The `Error.Code` botocore hangs off a `ClientError`, or `""` when there isn't one."""
    error = exc.response.get("Error") if isinstance(exc.response, dict) else None
    return str(error.get("Code", "")) if isinstance(error, dict) else ""


# -------------------- choosing one --------------------

_LOCAL = LocalBackend()

#: The live S3 backend, with the settings it was built from. Cached because building one
#: builds a boto3 client, which loads botocore's service model and opens a connection pool;
#: keyed on the settings so that a test repointing them gets a fresh one with no reset call
#: to forget.
_s3_cache: tuple[tuple[str, ...], S3Backend] | None = None


def _s3_identity() -> tuple[str, ...]:
    """What the cached backend was built from, as something safe to hold in a module global.

    The secret is hashed rather than kept: this tuple lives for the life of the process and
    would otherwise put the credential in the `repr` of anything that printed it, which is
    the failure `SecretStr` exists to make structurally impossible. A digest compares
    identically and reveals nothing.
    """
    secret = settings.S3_SECRET_ACCESS_KEY.get_secret_value() if settings.S3_SECRET_ACCESS_KEY else ""
    return (
        settings.S3_ENDPOINT_URL or "",
        settings.S3_BUCKET or "",
        settings.S3_ACCESS_KEY_ID or "",
        hashlib.sha256(secret.encode()).hexdigest(),
        settings.S3_REGION,
        settings.S3_PREFIX or "",
    )


def backend_for(option: FileStorageBackendOption) -> Backend:
    """The named backend, whatever `FILE_STORAGE_BACKEND` currently says.

    `migrate_blobs.py` is why this takes an argument: copying between the two means holding
    both at once, which no setting can express. Everything else wants `_backend()` below.
    """
    global _s3_cache

    if option is FileStorageBackendOption.LOCAL:
        return _LOCAL

    missing = missing_s3_settings(settings)
    if missing:
        raise RuntimeError(
            f"FILE_STORAGE_BACKEND=s3 needs {', '.join(missing)}, which {'is' if len(missing) == 1 else 'are'} "
            f"not set. See the object-storage block in src/.env.example."
        )

    identity = _s3_identity()
    cached = _s3_cache
    if cached is not None and cached[0] == identity:
        return cached[1]

    backend = S3Backend(new_s3_client(), bucket=settings.S3_BUCKET or "", prefix=settings.S3_PREFIX or "")
    _s3_cache = (identity, backend)
    return backend


def _backend() -> Backend:
    """The configured backend, read at call time.

    Never captured at import, for the reason `storage_root()` is a function rather than a
    module constant: the suite repoints the setting per test, and so does `conftest.py`
    before the app is ever built.
    """
    return backend_for(settings.FILE_STORAGE_BACKEND)


def describe_location() -> str:
    """Where blobs are kept, phrased for a log line. Backend-aware."""
    return _backend().describe()


def is_local() -> bool:
    """Whether the configured backend is the filesystem one.

    Exactly two callers, and both are about the filesystem rather than about storage: the
    sweeper's stale-temp-file pass, which has no object-store counterpart, and the tests
    that reach for a `Path`.
    """
    return settings.FILE_STORAGE_BACKEND is FileStorageBackendOption.LOCAL


# -------------------- the interface every caller uses --------------------


def ensure_storage_ready() -> None:
    """Prove at startup that blobs can be written, and fail loudly naming the setting.

    Backend-aware: a writability probe on the volume, or a put-and-delete against the
    bucket. Both the API's lifespan and the worker's startup call it - the worker deletes
    blobs during an account purge, so a worker with credentials that cannot write is a
    silent failure until the first purge, hours or days later.
    """
    _backend().ensure_ready()


async def put(key: str, data: bytes) -> None:
    """Store bytes under `key`.

    Idempotent by construction: keys embed the content hash, so re-`put`ting the same key
    writes byte-identical content over itself.
    """
    await anyio.to_thread.run_sync(_backend().write, key, data)


async def get(key: str) -> bytes:
    """Fetch the bytes stored under `key`, or raise `BlobMissingError`."""
    return await anyio.to_thread.run_sync(_backend().read, key)


async def delete(key: str) -> None:
    """Remove `key`. Tolerates an already-absent blob, so retries are free."""
    await anyio.to_thread.run_sync(_backend().remove, key)


def exists(key: str) -> bool:
    """Whether `key` has bytes. Synchronous, and blocking on the S3 backend - `has` is what
    the request path uses."""
    return _backend().exists(key)


async def has(key: str) -> bool:
    """`exists`, off the event loop - for the request path."""
    return await anyio.to_thread.run_sync(exists, key)


def iter_keys() -> Iterator[str]:
    """Every stored key, for the sweeper. Skips the temp prefix.

    Yields keys (`{kind}/{shard}/{name}`), not paths and not object names, because a key is
    what the database columns hold and what the caller diffs against. On the S3 backend the
    `S3_PREFIX` is stripped back off for the same reason.

    A full walk either way - on the object store that is a paginated listing, which is why
    `has_any_key` below exists rather than this being asked whether it yields anything.
    """
    return _backend().iter_keys()


def has_any_key() -> bool:
    """Whether anything is stored at all, in one bounded request. See `S3Backend.has_any_key`."""
    return _backend().has_any_key()


def stat_mtime(key: str) -> float | None:
    """The blob's last-modified time as a POSIX timestamp, or `None` if it is gone. Sweeper
    only."""
    return _backend().stat_mtime(key)


def delete_after_commit(db: AsyncSession | Session, keys: str | list[str]) -> None:
    """Schedule a delete for when the session's *current* transaction commits.

    This is what keeps every existing `commit: bool = False` composition working
    unchanged. `erase_certification` soft-deletes the row and hard-deletes its files in one
    caller-owned transaction, so the delete has to ride *that* commit rather than an inner
    one - and must not happen at all if the caller rolls back.

    Only appends to the session's `info` dict; the listeners registered below do the
    deleting. That is deliberate rather than incidental: much of the suite drives these
    services with `AsyncMock` sessions, which never commit, so they never fire the hook -
    while the Postgres-backed tests exercise it for real.

    Failures are logged, not raised: the row is already gone, the transaction has already
    committed, and the leftover blob is an orphan the sweeper reclaims
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
    """Delete what the committed transaction retired.

    Registered against the `Session` class rather than one factory, so it covers the
    request sessions, the script sessions and the test ones alike - and synchronously,
    because SQLAlchemy's session events are sync even under `AsyncSession`.

    That sync-ness is why the two backends part company here. On the local volume the
    deletes are a handful of `unlink` syscalls, on a path that has just done a database
    round trip; a thread hop to save microseconds would buy an ordering problem, so they
    run inline. A `DeleteObjects` request is three orders of magnitude slower and would
    block the event loop this listener is called on - `AsyncSession.commit` runs the sync
    session in a greenlet *on the loop thread* - so the object store's deletes go to a
    thread and are not waited for.

    Nothing is lost by not waiting. The contract was already best-effort: the rows are
    gone, the transaction has committed, and a blob that outlives them is an orphan the
    sweeper reclaims. Ordering is safe for the reason it always was - a retired key can
    never be minted again (`new_key`), so a delete in flight cannot name a live row's blob.
    """
    keys = _take_pending(session)
    if not keys:
        return

    backend = _backend()
    if not backend.removal_is_remote:
        backend.discard_many(keys)
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # A sync session outside any event loop: a script, or the suite. Nothing to protect.
        backend.discard_many(keys)
        return

    future = loop.run_in_executor(None, backend.discard_many, keys)
    _pending_removals.add(future)
    future.add_done_callback(_pending_removals.discard)


async def _await_pending_removals() -> None:
    """Wait for the post-commit deletes this loop handed to a thread.

    **The suite is the only caller.** The app never waits - see `_unlink_committed_blobs`
    for why not - and a test asserting that a purge removed a diver's objects would
    otherwise be racing the thread that removes them.
    """
    while True:
        outstanding = [future for future in _pending_removals if not future.done()]
        if not outstanding:
            return
        await asyncio.gather(*outstanding, return_exceptions=True)


@event.listens_for(Session, "after_rollback")
@event.listens_for(Session, "after_soft_rollback")
def _forget_rolled_back_blobs(session: Session, *args: object) -> None:
    """Drop the pending deletes when the transaction that scheduled them is rolled back.

    `after_soft_rollback` as well as `after_rollback`, because a rollback with nothing
    flushed to the database emits only the former - and the rows those keys belonged to are
    still there.
    """
    _take_pending(session)
