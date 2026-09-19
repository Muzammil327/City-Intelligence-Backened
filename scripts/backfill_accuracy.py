"""Seed the accuracy trend from history that is already stored.

The verified series can only ever grow forward from the day the service began
recording its forecasts. The hindcast series has no such limit: the readings
are already there, so the model can be refit at a succession of past cut-offs
and scored against what followed each one.

That is what this does. It walks back through stored history, and at each cut
reports what a hindcast would have said had it been run at that moment. Every
snapshot it writes is stamped with the last hour that fed the fit - never with
the time the backfill ran, which would claim a measurement that never happened.

Run it once after deploying the accuracy feature, then leave it alone; the
service records its own snapshots from then on.

    python -m scripts.backfill_accuracy --days 14 --dry-run
    python -m scripts.backfill_accuracy --days 14
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.errors import AppError
from app.services import readings as readings_service
from app.services import dynamo_client, verification

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("backfill")

DEFAULT_DAYS = 14
DEFAULT_HORIZON_HOURS = 24
# Hours between successive cut-offs. Six gives four points a day, which is
# enough to show a trend without writing a row per hour.
DEFAULT_STEP_HOURS = 6


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_HOURS)
    parser.add_argument("--step", type=int, default=DEFAULT_STEP_HOURS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report, but write nothing.",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    window_hours = args.days * 24

    logger.info("Loading %s hours of readings", window_hours)
    readings = await readings_service.load_readings(
        limit=window_hours, hours=window_hours
    )
    if not readings:
        logger.error("No readings available; nothing to backfill.")
        return 1
    logger.info("Loaded %s readings", len(readings))

    snapshots = verification.build_backfill_snapshots(
        readings, horizon_hours=args.horizon, step_hours=args.step
    )
    if not snapshots:
        logger.error(
            "Not enough history to hindcast over %s hours; nothing written.",
            args.horizon,
        )
        return 1

    oldest = min(snapshot.recorded_at for snapshot in snapshots)
    newest = max(snapshot.recorded_at for snapshot in snapshots)
    logger.info(
        "Built %s snapshots spanning %s to %s",
        len(snapshots),
        oldest.isoformat(),
        newest.isoformat(),
    )

    if args.dry_run:
        for snapshot in sorted(snapshots, key=lambda item: item.recorded_at):
            logger.info(
                "%s  MAE %.1f  RMSE %.1f  band %.1f%%",
                snapshot.recorded_at.isoformat(),
                snapshot.mean_absolute_error,
                snapshot.root_mean_square_error,
                snapshot.band_accuracy_pct,
            )
        logger.info("Dry run: nothing written.")
        return 0

    written = 0
    for snapshot in snapshots:
        try:
            await dynamo_client.put_accuracy_snapshot(snapshot)
        except AppError as exc:
            logger.error("Stopped after %s writes: %s", written, exc.code)
            return 1
        written += 1

    logger.info("Wrote %s snapshots.", written)
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    sys.exit(main())
