"""Rewrite the dives whose submerged time and average depth are still the whole recording's.

Run once, after `backfill_dive_profiles`, from the API container:

    docker compose exec api python -m src.scripts.backfill_dive_figures --dry-run
    docker compose exec api python -m src.scripts.backfill_dive_figures --device suunto "Suunto Ocean"

A dive's figures are its time in the water and the mean depth over it, where a stored row can
hold the whole recording's span and mean, the minutes at the surface after the dive included.
A figure that still equals what the whole recording gives is rewritten from the recording's
files, or, where none can re-yield it, from its stored samples; any other figure is the diver's
and is left alone. `services/figures_backfill.py` has the predicate.

**`--device` names the primary devices whose logbook-imported dives may be rewritten**, as
`dive_recording` stores them - a brand, and the model where it has one. Nothing stored says
whether such a dive's document stated a figure that happens to equal its span, so those dives
are only rewritten for a device whose reader the operator knows wrote the span: choose them from
the dives' devices first. Without one, no imported dive is touched.

The report names the devices before any dive, so a run that rewrites nothing still says what it
was scoped to. Safe to run repeatedly: a second run finds every figure it rewrote already at its
new value.
"""

import argparse
import asyncio
import logging

from ..app.services.figures_backfill import Device, backfill_dive_figures
from ._common import add_user_id_argument, log_at_info, session_with_cache

logger = logging.getLogger(__name__)


def _device(values: list[str]) -> Device:
    if len(values) > 2:
        raise argparse.ArgumentTypeError(f"--device takes a brand and at most a model, got {values!r}")
    return Device(brand=values[0], model=values[1] if len(values) == 2 else None)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--device",
        action="append",
        nargs="+",
        metavar=("BRAND", "MODEL"),
        default=[],
        help="A primary device whose logbook-imported dives may be rewritten, as the recording stores it: "
        '`--device suunto "Suunto Ocean"`, or a brand alone for a recording with no model. Repeatable.',
    )
    add_user_id_argument(parser)
    parser.add_argument("--dry-run", action="store_true", help="Report what would be rewritten, write nothing.")
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    try:
        devices = [_device(values) for values in args.device]
    except argparse.ArgumentTypeError as exc:
        raise SystemExit(str(exc)) from exc

    async with session_with_cache() as session:
        report = await backfill_dive_figures(session, devices=devices, user_id=args.user_id, dry_run=args.dry_run)

    logger.info(
        "Imported dives from: %s", "; ".join(str(device) for device in report.devices) or "no device (none touched)"
    )
    for rewrite in report.rewrites:
        changes = ", ".join(
            f"{name} {change[0]} -> {change[1]}"
            for name, change in (("submerged_time", rewrite.submerged_time), ("avg_depth", rewrite.avg_depth))
            if change is not None
        )
        logger.info(
            "%s dive %s (user %d, from %s): %s",
            "Would rewrite" if report.dry_run else "Rewrote",
            rewrite.dive_uuid,
            rewrite.user_id,
            rewrite.source,
            changes,
        )
    logger.info(
        "Figures backfill %s: examined=%d rewritten=%d failed=%d",
        "(dry run)" if report.dry_run else "complete",
        report.examined,
        len(report.rewrites),
        report.failed,
    )


if __name__ == "__main__":
    log_at_info()
    asyncio.run(main())
