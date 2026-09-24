"""Report - and, if asked, delete - blobs the store holds that no row references.

Backend-agnostic: it diffs `blob_store.iter_keys()` against the database, and that walk is
a filesystem tree or a bucket listing depending on `FILE_STORAGE_BACKEND`. The one half
that is not backend-agnostic is the stale-temp-file pass, which has nothing to sweep on an
object store - a `PutObject` is atomic, so there are no `.part` leftovers to age out - and
skips itself there.

Run from the API container:

    docker compose exec api python -m src.scripts.sweep_orphaned_files
    docker compose exec api python -m src.scripts.sweep_orphaned_files --delete

**Dry run is the default and `--delete` is explicit**, because the failure mode of getting
this wrong is unrecoverable and the failure mode of getting it right is a report nobody
reads. Gitea shipped an automatic `doctor --fix` that deleted 818 valid LFS files in 2025
([#36227](https://github.com/go-gitea/gitea/issues/36227)) for exactly the reason the
sanity check below exists: the database *looked* empty, so everything on disk scanned as
orphaned. This stack can reproduce that state by running mid-restore, or with `POSTGRES_*`
pointed at the wrong host - and no amount of mtime grace helps, because the files are old
and the database is wrong.

So `--delete` refuses when the numbers smell like a wrong database rather than real
orphans - no key referenced anywhere against a populated tree, or an unreferenced fraction
above `_SUSPICIOUS_FRACTION` - and only `--force` overrides it.

A script, not an arq cron. Every source of an orphan is rare and bounded: a crash between
writing a file and committing its row; two replacements of the same card racing; files
restored from a backup whose rows were deleted after the dump was taken; and a hard delete
of a `Certification` from the admin panel, whose FK cascade removes the file rows with no
service layer in the way to unlink anything; and a species photo re-fetched by
`backfill_species_photos --force` while the run that wrote the old one was still committing.
Scheduled deletion machinery is precisely what the Gitea tale warns against automating.
Revisit when *user-uploaded* photo galleries land - species photos are already covered.
"""

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..app.core.db.database import local_session
from ..app.models.certification_file import CertificationFile
from ..app.models.dive_file import DiveFile
from ..app.models.species import Species
from ..app.models.user_picture import UserPicture
from ..app.services import blob_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# How old an unreferenced file has to be before it counts as an orphan. This is what makes
# an *online* sweep safe: a file is written before the row that references it commits, so
# anything younger than this may simply be an upload in flight.
_GRACE_SECONDS = 24 * 60 * 60

# Above this share of the tree unreferenced, `--delete` refuses without `--force`. A real
# orphan population is a handful of files; a quarter of the store unreferenced means the
# database being compared against is probably not the one these files belong to.
_SUSPICIOUS_FRACTION = 0.25

# ...but only once there are enough files for a fraction to mean anything. On a tree of
# four, one orphan is 25% and says nothing at all, and a guard that fired there would train
# whoever runs this to reach for `--force` - which is precisely the habit that makes the
# guard worthless on the day it matters. The absolute "the database references nothing"
# check below has no floor, because that one is never noise.
_SUSPICIOUS_MIN_FILES = 20


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What one sweep found. `deleted` is 0 on a dry run by construction."""

    referenced: int = 0
    on_disk: int = 0
    orphaned: int = 0
    within_grace: int = 0
    stale_temp_files: int = 0
    deleted: int = 0
    refused: str | None = None


async def _referenced_keys(session: AsyncSession) -> set[str]:
    """Every key any row names, across every column a blob key is stored in.

    **Every new kind of blob has to be added here, and forgetting is destructive rather than
    merely blind.** `blob_store.iter_keys()` walks the whole store, so a kind missing from
    this set counts as stored and referenced by nothing - which classifies every file of it
    past the grace window as an orphan for `--delete` to unlink. The suspicious-fraction
    refusal is the only brake, `--force` overrides it, and it does not engage at all below
    `_SUSPICIOUS_MIN_FILES`, so the smallest instances have no brake at all.

    No count is written in this sentence on purpose: it said "the three places" while there
    were three, and the fourth arriving is exactly the moment nobody re-reads the docstring.

    The nullable columns filter their NULLs out - a species with no photo or a picture with no
    original must not contribute a `None` to a set the tree is diffed against.
    """
    dive_keys = (await session.execute(select(DiveFile.storage_key))).scalars().all()
    card_keys = (await session.execute(select(CertificationFile.storage_key))).scalars().all()
    rendition_keys = (await session.execute(select(UserPicture.rendition_storage_key))).scalars().all()
    original_keys = (
        (
            await session.execute(
                select(UserPicture.original_storage_key).where(UserPicture.original_storage_key.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    species_photo_keys = (
        (await session.execute(select(Species.photo_storage_key).where(Species.photo_storage_key.is_not(None))))
        .scalars()
        .all()
    )
    return set(dive_keys) | set(card_keys) | set(rendition_keys) | set(original_keys) | set(species_photo_keys)


def _classify(referenced: set[str], *, now: float) -> tuple[list[str], int, int]:
    """Split what the store holds into (orphans past the grace window, within grace, total)."""
    orphans: list[str] = []
    within_grace = 0
    on_disk = 0
    for key in blob_store.iter_keys():
        on_disk += 1
        if key in referenced:
            continue
        mtime = blob_store.stat_mtime(key)
        if mtime is None:
            # Deleted between the walk and the stat. Nothing to reclaim.
            continue
        if now - mtime < _GRACE_SECONDS:
            within_grace += 1
            continue
        orphans.append(key)
    return orphans, within_grace, on_disk


def _sweep_temp_dir(*, now: float, delete: bool) -> int:
    """Clear leftovers from interrupted writes, on the same grace window.

    A `.part` file is only ever live for the moment between `os.write` and `os.replace`, so
    anything a day old is the remains of a process that died in between.

    **The local backend's alone**, and the one place in this script that has to ask which
    backend is configured. The object store has no temp prefix to sweep because it needs no
    temp write: `PutObject` either lands whole or does not land, so the failure mode this
    reclaims after cannot occur. It is also the only caller left that builds a `Path`
    outside `blob_store.py`, which is why that rule has always been written with this
    exemption in it.
    """
    if not blob_store.is_local():
        return 0

    tmp_dir = blob_store.storage_root() / blob_store.TMP_DIRNAME
    if not tmp_dir.is_dir():
        return 0

    count = 0
    for path in tmp_dir.iterdir():
        if not path.is_file():
            continue
        try:
            if now - path.stat().st_mtime < _GRACE_SECONDS:
                continue
            count += 1
            if delete:
                path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not inspect temp file %s", path, exc_info=True)
    return count


def _refusal(referenced: set[str], orphans: list[str], on_disk: int) -> str | None:
    """Why `--delete` should not proceed, or `None` if it should. See the module docstring."""
    if on_disk and not referenced:
        return (
            f"the database references no stored files at all while the store holds {on_disk}. "
            "That is what a wrong POSTGRES_* target or a half-finished restore looks like, not "
            "an orphan population."
        )
    if on_disk >= _SUSPICIOUS_MIN_FILES and len(orphans) / on_disk > _SUSPICIOUS_FRACTION:
        return (
            f"{len(orphans)} of {on_disk} stored files are unreferenced "
            f"({len(orphans) / on_disk:.0%}), which is far more than the rare cases that produce "
            "orphans. Check that this is the right database before continuing."
        )
    return None


async def sweep(*, delete: bool = False, force: bool = False) -> SweepReport:
    """Diff the store against every `storage_key` column - see `_referenced_keys`, which is
    the list that has to grow with every new kind of blob."""
    async with local_session() as session:
        referenced = await _referenced_keys(session)

    now = time.time()
    # Blocking, and deliberately not hopped to a thread: on the object store this is a
    # paginated listing plus a `HeadObject` per unreferenced key, but this is a one-shot
    # script whose event loop has nothing else on it to protect.
    orphans, within_grace, on_disk = _classify(referenced, now=now)

    refused = None if force else _refusal(referenced, orphans, on_disk)
    should_delete = delete and refused is None

    deleted = 0
    for key in orphans if should_delete else []:
        await blob_store.delete(key)
        deleted += 1

    stale_temp_files = _sweep_temp_dir(now=now, delete=should_delete)

    return SweepReport(
        referenced=len(referenced),
        on_disk=on_disk,
        orphaned=len(orphans),
        within_grace=within_grace,
        stale_temp_files=stale_temp_files,
        deleted=deleted,
        refused=refused if delete else None,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually unlink the orphans. Without this the run only reports them.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete even when the numbers look like a wrong database rather than real orphans. "
        "Read the refusal message first - it is the only thing standing between a misconfigured "
        "run and every stored file.",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    report = await sweep(delete=args.delete, force=args.force)

    logger.info(
        "Sweep %s: referenced=%d on_disk=%d orphaned=%d within_grace=%d stale_temp_files=%d deleted=%d",
        "complete" if args.delete and report.refused is None else "(report only)",
        report.referenced,
        report.on_disk,
        report.orphaned,
        report.within_grace,
        report.stale_temp_files,
        report.deleted,
    )
    if report.refused is not None:
        logger.error("Refusing to delete: %s Re-run with --force if you are certain.", report.refused)
    elif not args.delete and report.orphaned:
        logger.info("Re-run with --delete to remove them.")


if __name__ == "__main__":
    asyncio.run(main())
