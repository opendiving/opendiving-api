"""Rewrite dives merged before a fold took its start and end values by side.

Run once from the API container, dry run first - it lists every value it would change:

    docker compose exec api python -m src.scripts.backfill_folded_dives --dry-run
    docker compose exec api python -m src.scripts.backfill_folded_dives

See `services/fold_backfill.py` for what it reads and which folds it cannot find. Safe to run
repeatedly: a second run finds nothing to change.
"""

import argparse
import asyncio
import logging

from ..app.services.fold_backfill import backfill_folds
from ._common import add_user_id_argument, log_at_info, session_with_cache

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_user_id_argument(parser)
    parser.add_argument("--dry-run", action="store_true", help="Report what would be written, write nothing.")
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    async with session_with_cache() as session:
        report = await backfill_folds(session, user_id=args.user_id, dry_run=args.dry_run)

    logger.info(
        "Folded-dive backfill %s: examined=%d dives_updated=%d absorbed_not_found=%d failed=%d",
        "(dry run)" if args.dry_run else "complete",
        report.examined,
        report.dives_updated,
        report.absorbed_not_found,
        report.failed,
    )


if __name__ == "__main__":
    log_at_info()
    asyncio.run(main())
