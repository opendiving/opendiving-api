"""Rewrite the stored dive-card outlines that no longer match their profile's samples.

Run once after the release that cuts the outline at the dive's end, from the API container:

    docker compose exec api python -m src.scripts.backfill_depth_outlines --dry-run
    docker compose exec api python -m src.scripts.backfill_depth_outlines

Every `dive_profile` row's `depth_outline` is re-derived from its stored samples, whatever they
were read, imported or merged from, and written where it differs. Nothing else on the row is
written, so no sample, span or ETag moves. Until a row is reached the dive list draws its old
outline, which is valid and runs on through the minutes the computer recorded at the surface.
Safe to run repeatedly: a second run rewrites nothing.
"""

import argparse
import asyncio
import logging

from ..app.services.dive_profiles import backfill_depth_outlines
from ._common import add_user_id_argument, log_at_info, session_with_cache

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_user_id_argument(parser)
    parser.add_argument("--dry-run", action="store_true", help="Report how many would be rewritten, write nothing.")
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    async with session_with_cache() as session:
        report = await backfill_depth_outlines(session, user_id=args.user_id, dry_run=args.dry_run)

    logger.info(
        "Depth outline backfill %s: examined=%d %s=%d",
        "(dry run)" if report.dry_run else "complete",
        report.examined,
        "would_rewrite" if report.dry_run else "rewritten",
        report.rewritten,
    )


if __name__ == "__main__":
    log_at_info()
    asyncio.run(main())
