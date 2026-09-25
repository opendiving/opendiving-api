"""Re-read stored exports for the tech fields Phase 2 added.

Run once after deploying the columns, from the API container:

    docker compose exec api python -m src.scripts.backfill_dive_tech_fields
    docker compose exec api python -m src.scripts.backfill_dive_tech_fields --parser-key suunto_xml
    docker compose exec api python -m src.scripts.backfill_dive_tech_fields --dry-run

Fills each recording's readouts (`cns_start/cns_end/otu_start/otu_end/surface_pressure_bar`),
its six device columns, the mode and salinity the computer ran with and the five `deco_*`
columns of its model; and, from the primary recording, the dive's entry/exit coordinates and -
where the stored cylinders still demonstrably match the file's -
`dive_mixture.po2_limit/gas_number/role`. The readouts and the dive columns are whatever
`RecordingReadouts` and `DiveTechScalars` publish rather than a list kept here.

**The recording's settings are the exception to that**: they come off `ParsedDiveSchema`
rather than a pivot, so `fill_recording_settings` is named in `backfill_tech_fields` beside
`fill_device_fields` and this sentence has to be edited when a member is added. Everything
follows the same never-overwrite rule as the device columns, so a run fills what no earlier
file recorded and takes nothing away.

**It does not touch the profile's samples**, which are a different backfill's
(`backfill_dive_profiles`) and a different question - see *"The decompression channels
arrive for new dives only"* in `DECISIONS.md` for why nothing runs that one.

**Every recording holding a stored file is a candidate on every run**; the dive's own columns
are written from the primary alone. See `services/dive_files.py::backfill_tech_fields` for why
there is no version column to select on, why the run no longer *clears* a reading no file
yields, and why the mixture half is deliberately the timid one.

**One run is the upgrade step for an existing instance.** The migration that introduced
recordings left every migrated one device-less, and the one that moved the readouts onto the
recording copied them onto the primary alone - neither reads a file - so this is what fills
the rest, because it re-parses every stored export anyway.

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
        help="Only re-read recordings holding a file recorded under this parser (e.g. `suunto_xml`). "
        "This is what `dive_file.parser_key` is for; a recording holding two files is a candidate "
        "when either of them names it.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after this many candidate recordings.")
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
        "Tech-field backfill %s: examined=%d recordings_updated=%d mixtures_updated=%d mixtures_skipped=%d failed=%d",
        "(dry run)" if args.dry_run else "complete",
        report.examined,
        report.recordings_updated,
        report.mixtures_updated,
        report.mixtures_skipped,
        report.failed,
    )


if __name__ == "__main__":
    asyncio.run(main())
