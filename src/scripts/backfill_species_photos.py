"""Fetch a Commons photograph for catalog species that have never been asked about.

Run from the API container:

    docker compose exec api python -m src.scripts.backfill_species_photos --dry-run --limit 5
    docker compose exec api python -m src.scripts.backfill_species_photos
    docker compose exec api python -m src.scripts.backfill_species_photos --force

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
`--limit` will not break that up; see `_candidates`.

Three things differ from the nearest sibling, and copying that one blindly gets each wrong:

- **It creates no Redis pool, and that is not an oversight.** `backfill_dive_profiles` needs
  one because `delete_keys_by_pattern` silently no-ops when the pool is absent and that script
  invalidates. This one invalidates nothing: filling a photo column goes stale only in the
  single-dive cache, which self-heals within its TTL, and building a cross-user invalidation
  surface is precisely what the species catalog's design defers. There is nothing here to
  no-op.
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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..app.core.db.database import local_session
from ..app.models.species import Species
from ..app.services import species_photos
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
    `--force` drops the predicate altogether, so there is nothing left to exclude the rows the
    previous run just handled and the same first `limit` ids come back every time - verified,
    not inferred. Chunking a forced re-walk is not something these two flags can express
    together: run `--force` on its own and let it finish.
    """
    statement = select(Species.id, Species.aphia_id, Species.scientific_name).order_by(Species.id)
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Stop after this many candidate species.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-attempt every species regardless of when it was last tried, replacing any photo "
        "already stored. Without this only species never attempted at all are considered.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be fetched, write nothing.")
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

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
