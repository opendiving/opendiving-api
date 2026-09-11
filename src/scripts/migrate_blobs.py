"""Copy every stored blob from one backend to the other, for an operator switching.

Run from the API container, with **both** backends configured - the `S3_*` group set *and*
`FILE_STORAGE_DIR` pointing at the volume - and `FILE_STORAGE_BACKEND` still naming the one
you are moving away from:

    docker compose exec api python -m src.scripts.migrate_blobs
    docker compose exec api python -m src.scripts.migrate_blobs --to local

Then flip `FILE_STORAGE_BACKEND` and restart. Nothing else moves: the `storage_key` a row
carries is the same string on both backends (`blob_store.new_key`), which is the whole
reason this is a copy and not a migration.

**Idempotent and resumable**, and the key format is what makes it so. A key ends in the
sha256 of its own content, so an object already present under that key cannot hold
different bytes than the file it came from - skipping it is not an optimisation but the
correct answer. Interrupt this at any point, run it again, and it picks up where it
stopped.

**It never deletes from the source.** A switch that goes wrong has to be a restart away
from working again, so reclaiming the old copy is a separate, deliberate act - `rm -rf` on
the volume, or the bucket's own lifecycle - once the new backend has been seen to serve.

**Run it against a quiet instance.** Anything uploaded after this walks past its key stays
on the old backend, and the `storage_key` column will point at bytes the new one does not
have. Stopping the API (or the diver's own restraint for ten minutes) is the whole
mitigation; a second run afterwards catches what arrived in between, which is the other
thing the resumability buys.
"""

import argparse
import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass

from ..app.core.config import FileStorageBackendOption
from ..app.services import blob_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#: Progress lands in the log every this many keys, because the only feedback a copy of tens
#: of thousands of objects gives otherwise is silence.
_PROGRESS_EVERY = 500

#: The content hash a key ends in. Anchored, so a key shaped some other way - anything
#: written before the format settled - simply skips the digest check rather than failing it.
_DIGEST_TAIL = re.compile(r"_([0-9a-f]{64})$")


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """What one run moved. `failed` is what the operator has to look at."""

    total: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0


def _expected_digest(key: str) -> str | None:
    match = _DIGEST_TAIL.search(key)
    return match.group(1) if match else None


def _copy_one(source: blob_store.Backend, target: blob_store.Backend, key: str) -> str:
    """Move one blob across, returning `"copied"`, `"skipped"` or `"failed"`.

    Synchronous on purpose: both backends are blocking, and `migrate` below runs the whole
    walk in one worker thread rather than hopping per object. A thread hop per key would
    dominate the cost of a local read.
    """
    if target.exists(key):
        return "skipped"

    data = source.read(key)

    expected = _expected_digest(key)
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        # Refusing rather than copying is the point. The key is the claim that the bytes
        # hash to this; bytes that do not are a corrupted source file, and carrying them
        # to the new backend would launder the corruption into somewhere with no older
        # copy to compare against.
        logger.error("%s does not match the content hash in its own key; not copied", key)
        return "failed"

    target.write(key, data)
    return "copied"


def _migrate(source: blob_store.Backend, target: blob_store.Backend) -> MigrationReport:
    tally = {"copied": 0, "skipped": 0, "failed": 0}
    total = 0

    for key in source.iter_keys():
        total += 1
        try:
            outcome = _copy_one(source, target, key)
        except blob_store.BlobMissingError:
            # The walk found it and the read did not: something deleted it in between,
            # which on a quiet instance means the sweeper. Nothing to carry across.
            logger.warning("%s went away between the listing and the read; not copied", key)
            outcome = "skipped"
        except Exception:
            logger.exception("Could not copy %s", key)
            outcome = "failed"

        tally[outcome] += 1

        if total % _PROGRESS_EVERY == 0:
            logger.info(
                "... %d keys seen, %d copied, %d already there, %d failed",
                total,
                tally["copied"],
                tally["skipped"],
                tally["failed"],
            )

    return MigrationReport(total=total, copied=tally["copied"], skipped=tally["skipped"], failed=tally["failed"])


async def migrate(*, to: FileStorageBackendOption) -> MigrationReport:
    """Copy everything the other backend holds into `to`.

    Off the event loop in one hop, for the reason `_copy_one` gives: this is a long
    blocking walk, not a request.
    """
    source_option = FileStorageBackendOption.LOCAL if to is FileStorageBackendOption.S3 else FileStorageBackendOption.S3
    source = blob_store.backend_for(source_option)
    target = blob_store.backend_for(to)

    logger.info("Copying from %s to %s", source.describe(), target.describe())
    return await asyncio.to_thread(_migrate, source, target)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--to",
        choices=[option.value for option in FileStorageBackendOption],
        default=FileStorageBackendOption.S3.value,
        help="Which backend to copy into. The other one is the source.",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    report = await migrate(to=FileStorageBackendOption(args.to))

    logger.info(
        "Copy complete: %d key(s) seen, %d copied, %d already present, %d failed",
        report.total,
        report.copied,
        report.skipped,
        report.failed,
    )
    if report.failed:
        raise SystemExit(
            f"{report.failed} blob(s) could not be copied. Nothing was deleted from the source - "
            "fix what the errors above name and run this again."
        )


if __name__ == "__main__":
    asyncio.run(main())
