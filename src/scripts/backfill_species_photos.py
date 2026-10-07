"""Fetch a Commons photograph for catalog species that have never been asked about.

Run from the API container:

    docker compose exec api python -m src.scripts.backfill_species_photos --dry-run --limit 5
    docker compose exec api python -m src.scripts.backfill_species_photos
    docker compose exec api python -m src.scripts.backfill_species_photos --force
    docker compose exec api python -m src.scripts.backfill_species_photos --recheck-size --dry-run
    docker compose exec api python -m src.scripts.backfill_species_photos --recheck-size

A script rather than an arq job, for the reason `backfill_dive_profiles.py` gives: the worker
runs crons only, and a backfill finishes rather than recurring. Safe to run repeatedly - the
second run reports 0, **including for the species it found no photo for**, which is the whole
reason `photo_fetched_at` is stamped on a failed attempt. A species whose fetch merely *timed
out* is the exception and is deliberately left for the next run: nothing was established about
it, so writing a no-photo verdict would be a claim nobody checked.

**Upgrading from a build whose photo fence refused Commons' thumbnail host? Run `--force`
once.** Every species attempted under that build carries a stamped `photo_fetched_at` and no
photo, so an ordinary run skips all of them - the predicate below reads them as asked and
answered. `--force` is the whole remedy and no narrower flag is offered on purpose: a row
poisoned that way is **byte-for-byte identical** to a species the selection rule refused on
its merits, both being a stamped timestamp over a null `photo_storage_key`, so a
"retry only the failures" predicate could not tell them apart and would promise a precision it
does not have. What that costs is honest instead: `--force` re-walks the whole catalog,
including the large tail that legitimately has no photo, at the pace below - so budget the
same "under an hour per thousand species" a first run takes, and let it finish in one go.
`--limit` will not break that up; see `_candidates`. A row an admin hid or pinned is skipped,
`--force` included: that decision is what the curation column exists to keep.

**`--recheck-size` is a different pass over the same rows**, and calls nobody. Every stored
photo is opened from its bytes and measured; one narrower than the 500 px floor new fetches
refuse is dropped unless an admin pinned it, and one whose bytes are missing or will not open
is dropped whatever its curation. The stored bytes are the source's own whenever the source was
narrow, because nothing here resizes, so no Commons call is needed to know. It ends with two
counts - photos still without dimensions, and photos dropped (or, on `--dry-run`, to drop) -
and a pass that has run leaves a following `--dry-run` reporting zero for both. Running it again
is harmless. It composes with `--dry-run` and nothing else.

Three things differ from the nearest sibling, and copying that one blindly gets each wrong:

- **It creates no Redis pool, and that is not an oversight.** `backfill_dive_profiles` needs
  one because `delete_keys_by_pattern` silently no-ops when the pool is absent and that script
  invalidates. This one invalidates nothing: a photo it writes or drops leaves the old digest in
  the cached dive and life-list reads until their TTLs run out - for a dropped photo, an image
  that 404s until then - and building a cross-user invalidation surface is precisely what the
  species catalog's design defers. There is nothing here to no-op.
- **Its predicate is `photo_fetched_at IS NULL`, not "has no stored photo".** "No photo" is the
  permanent outcome for most of the catalog - the selection rule refuses some on purpose, and
  88.3% of Wikidata items carrying a WoRMS id have no P18 at all - so a predicate keyed on the
  absence of bytes would never shrink and every re-run would re-query Wikidata and Commons for
  the whole photo-less tail forever. `--force` therefore means "re-attempt regardless of when
  it was last tried", not "re-attempt the ones that failed".
- **It paces itself and completes; it does not degrade and skip.** The provider throttle in
  `services/species_service.py` is built to *degrade*, because its counter is instance-wide and
  one diver's search must not reject another's. A backfill wants the opposite - to wait and
  finish - so this sleeps between species rather than dropping them. Wikimedia publishes no
  numeric limit but prohibits spikes and requires obeying any slow-down response, and the
  anonymous ceiling was measured at roughly 28 heavy `props=claims` calls in about 90 seconds.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..app.core.db.database import local_session
from ..app.models.species import Species
from ..app.services import blob_store, species_photos
from ..app.services.species_service import fetch_photo_for_species

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# What one species costs upstream: a Wikidata entity fetch, possibly a synonym search and a
# second entity fetch, a Commons `imageinfo` call and one byte fetch. Three seconds between
# species keeps the sustained rate an order of magnitude under the measured anonymous ceiling
# for the heaviest of those, which is the "no spikes" the usage guidelines ask for rather than
# a published number there is none of. A thousand-species catalog is then under an hour, run
# once.
_PAUSE_BETWEEN_SPECIES_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """What one run did. `stored`, `without_photo` and `timed_out` are what it actually
    attempted; on a dry run all three are 0 by construction and `examined` is what it would
    have tried.

    `timed_out` is reported separately because it is the one outcome the *next* run will try
    again - those species keep a null `photo_fetched_at`, since a cancelled fetch established
    nothing about them.
    """

    examined: int = 0
    stored: int = 0
    without_photo: int = 0
    timed_out: int = 0


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One species to attempt, as plain detached values rather than an ORM entity.

    **Not a `Species` row, and that is load-bearing.** `save_photo_attempt` calls
    `release_read_transaction` on the path where a photo was fetched, which is a
    `Session.rollback()` - and a rollback expires *every* instance in the identity map
    regardless of `expire_on_commit=False`. A live entity held across that call would have its
    next attribute access turn into a lazy refresh on an `AsyncSession` outside a greenlet, so
    the loop below would die on the first species that actually yielded a photo. That helper's
    own docstring states the precondition: release only where the preceding lookup handed back
    something detached. This is what makes that true here.
    """

    id: int
    aphia_id: int
    scientific_name: str


async def _candidates(session: AsyncSession, *, limit: int | None, force: bool) -> list[_Candidate]:
    """The species to attempt, oldest catalog rows first.

    Ordered by `id` so a `--limit`ed run walks the catalog in a stable order rather than
    whatever order the planner happens to return. Successive *ordinary* runs then continue
    where the last stopped - though it is the stamped `photo_fetched_at` that excludes what was
    already done, not the ordering.

    **A `--force --limit` run does not advance between runs, and no ordering could make it.**
    `--force` drops the timestamp predicate, so there is nothing left to exclude the rows the
    previous run just handled and the same first `limit` ids come back every time - verified,
    not inferred. Chunking a forced re-walk is not something these two flags can express
    together: run `--force` on its own and let it finish.

    `photo_curation IS NULL` holds under `--force` too. A row curated after this selection is
    caught by the same guard in `save_photo_attempt`.
    """
    statement = (
        select(Species.id, Species.aphia_id, Species.scientific_name)
        .where(Species.photo_curation.is_(None))
        .order_by(Species.id)
    )
    if not force:
        statement = statement.where(Species.photo_fetched_at.is_(None))
    if limit is not None:
        statement = statement.limit(limit)
    rows = (await session.execute(statement)).all()
    return [_Candidate(id=row.id, aphia_id=row.aphia_id, scientific_name=row.scientific_name) for row in rows]


async def backfill_species_photos(
    session: AsyncSession, *, limit: int | None = None, force: bool = False, dry_run: bool = False
) -> BackfillReport:
    """Attempt a photo for every candidate species, one at a time with a pause between.

    Serial rather than concurrent, deliberately: the point is to be a well-behaved anonymous
    client of two Wikimedia APIs, and concurrency here would be the spike their usage
    guidelines prohibit. Nothing about this is latency-sensitive.
    """
    candidates = await _candidates(session, limit=limit, force=force)
    if dry_run:
        for species in candidates:
            logger.info("Would fetch a photo for %s (AphiaID %d)", species.scientific_name, species.aphia_id)
        return BackfillReport(examined=len(candidates))

    stored = 0
    without_photo = 0
    timed_out = 0
    for index, species in enumerate(candidates):
        if index:
            await asyncio.sleep(_PAUSE_BETWEEN_SPECIES_SECONDS)

        attempt = await fetch_photo_for_species(scientific_name=species.scientific_name, aphia_id=species.aphia_id)

        if not attempt.completed:
            # Nothing is written, so this species keeps its null `photo_fetched_at` and is a
            # candidate again next run. Stamping a cancelled fetch would read as "asked, and
            # there is nothing" for a species nothing actually asked about.
            timed_out += 1
            logger.warning("Timed out fetching a photo for %s; leaving it for the next run", species.scientific_name)
            continue

        await species_photos.save_photo_attempt(session, species_id=species.id, photo=attempt.photo)
        if attempt.photo is None:
            without_photo += 1
            logger.info("No usable photo for %s (AphiaID %d)", species.scientific_name, species.aphia_id)
        else:
            stored += 1
            logger.info("Stored %s for %s", attempt.photo.file, species.scientific_name)

    return BackfillReport(examined=len(candidates), stored=stored, without_photo=without_photo, timed_out=timed_out)


@dataclass(frozen=True, slots=True)
class RecheckReport:
    """What one `--recheck-size` pass found. `without_dimensions` is counted over the table
    when the pass ends, so a dry run reports what a real run would still have to measure and a
    real run reports what it left - zero, unless a row changed under it. `dropped` is what was
    cleared, or on a dry run what would be."""

    measured: int = 0
    dropped: int = 0
    without_dimensions: int = 0


@dataclass(frozen=True, slots=True)
class _StoredPhoto:
    """One stored photo to measure, as detached values for `_Candidate`'s reason."""

    id: int
    scientific_name: str
    storage_key: str
    curation: str | None


async def _stored_photos(session: AsyncSession) -> list[_StoredPhoto]:
    rows = (
        await session.execute(
            select(Species.id, Species.scientific_name, Species.photo_storage_key, Species.photo_curation)
            .where(Species.photo_storage_key.is_not(None))
            .order_by(Species.id)
        )
    ).all()
    return [
        _StoredPhoto(
            id=row.id,
            scientific_name=row.scientific_name,
            storage_key=row.photo_storage_key,
            curation=row.photo_curation,
        )
        for row in rows
    ]


async def _without_dimensions(session: AsyncSession) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(Species)
        .where(Species.photo_storage_key.is_not(None), Species.photo_width.is_(None))
    )
    return int(count or 0)


async def recheck_photo_size(session: AsyncSession, *, dry_run: bool = False) -> RecheckReport:
    """Measure every stored photo, and drop the ones the floor would refuse today.

    Reads the bytes rather than asking Commons: nothing in this pipeline resizes, so a photo
    whose source was narrower than the floor is stored at the source's own width. A pinned
    photo keeps its width, since an admin chose it knowing it; a photo whose bytes are missing
    or will not open is dropped pinned or not, because there is nothing left to protect.

    Every write is guarded on the storage key read here, so a row pinned or re-fetched while
    the pass ran is left as that write made it.
    """
    measured = 0
    dropped = 0
    for photo in await _stored_photos(session):
        try:
            width, height = species_photos.measure_photo(await blob_store.get(photo.storage_key))
        except blob_store.BlobMissingError, species_photos.UnsupportedPhotoImageError:
            width = height = None

        if width is None or height is None:
            reason = "its bytes are missing or will not open"
        elif width < species_photos.COMMONS_THUMBNAIL_WIDTH and photo.curation is None:
            reason = f"it is {width} px wide"
        else:
            reason = None

        if dry_run:
            if reason is not None:
                dropped += 1
                logger.info("Would drop the photo of %s: %s", photo.scientific_name, reason)
            continue

        if reason is not None:
            if await species_photos.drop_stored_photo(session, species_id=photo.id, storage_key=photo.storage_key):
                dropped += 1
                logger.info("Dropped the photo of %s: %s", photo.scientific_name, reason)
        elif width is not None and height is not None:
            if await species_photos.record_photo_dimensions(
                session, species_id=photo.id, storage_key=photo.storage_key, width=width, height=height
            ):
                measured += 1

    return RecheckReport(measured=measured, dropped=dropped, without_dimensions=await _without_dimensions(session))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Stop after this many candidate species.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-attempt every species regardless of when it was last tried, replacing any photo "
        "already stored, except where an admin hid or pinned it. Without this only species never "
        "attempted at all are considered.",
    )
    parser.add_argument(
        "--recheck-size",
        action="store_true",
        help="Instead of fetching, measure every stored photo from its bytes and drop those under the "
        "500 px floor that no admin pinned, plus any whose bytes are missing. Calls no provider. "
        "Composes with --dry-run only.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be done, write nothing.")
    args = parser.parse_args()
    if args.recheck_size and (args.force or args.limit is not None):
        parser.error("--recheck-size composes with --dry-run only")
    return args


async def main() -> None:
    args = _parse_args()

    if args.recheck_size:
        async with local_session() as session:
            recheck = await recheck_photo_size(session, dry_run=args.dry_run)
        logger.info(
            "Species photo size re-check %s: without_dimensions=%d %s=%d",
            "(dry run)" if args.dry_run else "complete",
            recheck.without_dimensions,
            "to_drop" if args.dry_run else "dropped",
            recheck.dropped,
        )
        return

    async with local_session() as session:
        report = await backfill_species_photos(session, limit=args.limit, force=args.force, dry_run=args.dry_run)

    logger.info(
        "Species photo backfill %s: examined=%d stored=%d without_photo=%d timed_out=%d",
        "(dry run)" if args.dry_run else "complete",
        report.examined,
        report.stored,
        report.without_photo,
        report.timed_out,
    )
    if report.timed_out:
        logger.info("Re-run to retry the %d that timed out; they were not marked as attempted.", report.timed_out)


if __name__ == "__main__":
    asyncio.run(main())
