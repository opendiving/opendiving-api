"""Re-read stored exports for the tech fields Phase 2 added.

Run once after deploying the columns, from the API container:

    docker compose exec api python -m src.scripts.backfill_dive_tech_fields
    docker compose exec api python -m src.scripts.backfill_dive_tech_fields --parser-key suunto_xml
    docker compose exec api python -m src.scripts.backfill_dive_tech_fields --dry-run

Fills `dive.cns_start/cns_end/otu_start/otu_end/surface_pressure_bar`, the entry/exit
coordinates beside them, and - where the stored cylinders still demonstrably match the
file's - `dive_mixture.po2_limit/gas_number/role`. The dive columns are whatever
`DiveTechScalars` publishes rather than a list kept here, which is what let the
coordinates arrive without editing this script. Every dive with a stored export is a
candidate on every run; see
`services/dive_files.py::backfill_tech_fields` for why there is no version column to
select on and why the mixture half is deliberately the timid one.

A script rather than an arq job, and a second script rather than a flag on
`backfill_dive_profiles`, for the reasons recorded in that file and in DECISIONS.md.
Safe to run repeatedly.
"""

import argparse
import asyncio
import logging

from ..app.core.db.database import local_session
from ..app.core.setup import close_redis_cache_pool, create_redis_cache_pool
from ..app.services.dive_files import backfill_tech_fields

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
    parser.add_argument("--dry-run", action="store_true", help="Report what would be written, write nothing.")
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    # Same reason as `backfill_dive_profiles`: `delete_keys_by_pattern` silently returns
    # when `cache.client is None`, which is only ever set by the API's lifespan - and this
    # script does not go through it. Without this pool every backfilled dive's cached
    # detail response would keep serving the old values for up to an hour, silently.
    await create_redis_cache_pool()
    try:
        async with local_session() as session:
            report = await backfill_tech_fields(
                session,
                parser_key=args.parser_key,
                limit=args.limit,
                dry_run=args.dry_run,
            )
    finally:
        await close_redis_cache_pool()

    logger.info(
        "Tech-field backfill %s: examined=%d dives_updated=%d mixtures_updated=%d mixtures_skipped=%d failed=%d",
        "(dry run)" if args.dry_run else "complete",
        report.examined,
        report.dives_updated,
        report.mixtures_updated,
        report.mixtures_skipped,
        report.failed,
    )


if __name__ == "__main__":
    asyncio.run(main())
