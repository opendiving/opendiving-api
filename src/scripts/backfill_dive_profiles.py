"""Extract dive profiles from exports that are already stored against dives.

Run once per extractor version, from the API container:

    docker compose exec api python -m src.scripts.backfill_dive_profiles
    docker compose exec api python -m src.scripts.backfill_dive_profiles --parser-key suunto_xml
    docker compose exec api python -m src.scripts.backfill_dive_profiles --dry-run

A script rather than an arq job, deliberately. DECISIONS.md's "The Arq worker now does
one real thing" records that the API-side queue plumbing was removed and the worker runs
crons only; a backfill finishes once per extractor version, so scheduling it as a cron
would mean rescanning the whole corpus forever for a job that is already done.

Selects the dives whose profile is missing, was produced by an older extractor, or came
out of different bytes than the file currently on the dive, and re-reads each stored
export one at a time. Safe to run repeatedly: the second run reports 0 extracted.
"""

import argparse
import asyncio
import logging

from ..app.core.db.database import local_session
from ..app.core.setup import close_redis_cache_pool, create_redis_cache_pool
from ..app.services.dive_profiles import backfill_profiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parser-key",
        default=None,
        help="Only re-read files recorded under this parser (e.g. `suunto_xml`). "
        "This is what `dive_file.parser_key` is for.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after this many candidate files.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even where the stored profile is already current - for after a parser fix "
        "that didn't bump PROFILE_EXTRACTOR_VERSION.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be extracted, write nothing.")
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    # `delete_keys_by_pattern` silently returns when `cache.client is None`, and that is
    # only ever set by the API's lifespan - which this script does not go through. Without
    # this pool every backfilled dive's cached detail response would keep claiming the
    # dive has no profile for up to an hour, and the failure would be completely silent.
    await create_redis_cache_pool()
    try:
        async with local_session() as session:
            report = await backfill_profiles(
                session,
                parser_key=args.parser_key,
                limit=args.limit,
                force=args.force,
                dry_run=args.dry_run,
            )
    finally:
        await close_redis_cache_pool()

    logger.info(
        "Backfill %s: examined=%d extracted=%d skipped=%d no_samples=%d failed=%d",
        "(dry run)" if args.dry_run else "complete",
        report.examined,
        report.extracted,
        report.skipped,
        report.no_samples,
        report.failed,
    )


if __name__ == "__main__":
    asyncio.run(main())
