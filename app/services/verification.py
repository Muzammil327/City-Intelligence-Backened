"""How well the forecast model actually predicts, measured against what happened.

`r2_score` on a forecast response is *in-sample* fit — how closely the model
reproduces the data it was trained on. It says nothing about predicting an hour
the model has never seen, and the schema says so. This module answers the other
question.

The method is walk-forward validation, and the honest name for it is a
hindcast: hold out the most recent `horizon_hours` of observations, refit on
everything before them, predict forward, and compare each prediction with the
observation that actually followed. Nothing about the held-out hours reaches the
model.

Two deliberate choices, both on the conservative side:

* Weather over the held-out window is **carried forward**, not taken from the
  observations. The actual wind and humidity are sitting right there in the
  held-out rows, and feeding them in would hand the model perfect knowledge of
  the future and inflate the score. Live forecasts project onto predicted
  weather, which is imperfect; carrying the last value forward is imperfect in
  the same direction.
* Band accuracy counts a prediction correct only when it lands in the same EPA
  category as the observation. Being 15 points out inside "Unhealthy" changes
  no advice; crossing a boundary does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import anyio

from app.config import CITY_NAME, get_settings
from app.errors import AppError, InsufficientDataError
from app.models.aqi import category_for_aqi
from app.models.schemas import (
    ACCURACY_BASIS_HINDCAST,
    ACCURACY_BASIS_VERIFIED,
    AccuracyHistoryResponse,
    AccuracySnapshot,
    ForecastAccuracy,
    ForecastAccuracyPoint,
    ForecastResponse,
    HistoryPoint,
    StoredForecastPoint,
)
from app.services import dynamo_client, predictor
from app.services import readings as readings_service
from app.services.timestamps import utc_now

logger = logging.getLogger(__name__)

# How much history to fit the hindcast on, before the held-out window.
TRAINING_WINDOW_HOURS = 168


def _mean_absolute_error(pairs: list[ForecastAccuracyPoint]) -> float:
    return sum(abs(pair.predicted_aqi - pair.observed_aqi) for pair in pairs) / len(pairs)


def _root_mean_square_error(pairs: list[ForecastAccuracyPoint]) -> float:
    squares = sum((pair.predicted_aqi - pair.observed_aqi) ** 2 for pair in pairs)
    return (squares / len(pairs)) ** 0.5


def _evaluate(readings: list[HistoryPoint], horizon_hours: int) -> ForecastAccuracy:
    ordered = sorted(readings, key=lambda point: point.observed_at)

    # Drop duplicate hours the same way the predictor does, so the split counts
    # the rows the model would actually see.
    deduped: dict[str, HistoryPoint] = {}
    for point in ordered:
        deduped[point.observed_at.isoformat()] = point
    ordered = sorted(deduped.values(), key=lambda point: point.observed_at)

    held_out = ordered[-horizon_hours:]
    train = ordered[: -len(held_out)] if held_out else ordered

    if len(train) < predictor.MIN_TRAINING_SAMPLES or not held_out:
        raise InsufficientDataError(
            f"Need at least {predictor.MIN_TRAINING_SAMPLES + horizon_hours} stored "
            f"readings to measure accuracy over {horizon_hours} hours; "
            f"{len(ordered)} are available."
        )

    # No forecast conditions: the model carries the last known weather forward
    # rather than being handed the weather that actually occurred.
    forecast = predictor.fit_and_predict(train, len(held_out))

    observed_by_hour = {
        point.observed_at.replace(minute=0, second=0, microsecond=0): point
        for point in held_out
    }

    pairs: list[ForecastAccuracyPoint] = []
    for offset, predicted in enumerate(forecast.points, start=1):
        hour = predicted.predicted_for.replace(minute=0, second=0, microsecond=0)
        actual = observed_by_hour.get(hour)
        if actual is None:
            continue
        pairs.append(
            ForecastAccuracyPoint(
                predicted_for=predicted.predicted_for,
                hours_ahead=offset,
                predicted_aqi=predicted.aqi,
                observed_aqi=actual.aqi,
                error=predicted.aqi - actual.aqi,
                predicted_category=predicted.category,
                observed_category=category_for_aqi(actual.aqi),
            )
        )

    if not pairs:
        raise InsufficientDataError(
            "No predicted hour lined up with an observed one, so accuracy "
            "cannot be measured."
        )

    same_band = sum(
        1 for pair in pairs if pair.predicted_category == pair.observed_category
    )

    return ForecastAccuracy(
        city=CITY_NAME,
        model=forecast.model,
        horizon_hours=len(pairs),
        training_samples=forecast.training_samples,
        evaluated_from=pairs[0].predicted_for,
        mean_absolute_error=round(_mean_absolute_error(pairs), 1),
        root_mean_square_error=round(_root_mean_square_error(pairs), 1),
        band_accuracy_pct=round(100.0 * same_band / len(pairs), 1),
        points=pairs,
    )


async def evaluate_forecast(horizon_hours: int) -> ForecastAccuracy:
    """Hindcast the last `horizon_hours` and report how close the model was."""
    window = TRAINING_WINDOW_HOURS + horizon_hours
    readings = await readings_service.load_readings(limit=window, hours=window)
    if not readings:
        raise InsufficientDataError("No readings are available to measure against.")

    # Fitting is CPU-bound, exactly as in the predictor.
    return await anyio.to_thread.run_sync(lambda: _evaluate(readings, horizon_hours))


# --- Recording skill over time -----------------------------------------------
#
# Everything above answers "how wrong is the model, right now?" and throws the
# answer away. These functions keep it, so the question becomes "how wrong has
# it been, over time?" - which is the only form that can be trended.
#
# Two bases, reported separately and never averaged together:
#
#   hindcast  the model refit without the most recent hours and scored against
#             them. Available from the first call and backfillable over stored
#             history, but it measures a re-run, not a forecast anyone saw.
#   verified  predictions this service actually published, scored against the
#             observations that later arrived. The honest number, and the one
#             that only accrues forward from the day recording started.

# How far back to look for matured predictions to verify.
VERIFICATION_WINDOW_HOURS = 48

# One target hour can hold several recorded predictions, made at different
# times. The earliest is scored: it had the least information, so it is the
# hardest test and the one a forecast should be judged on.
MAX_RECORDED_POINTS = 500


def _snapshot_from(
    accuracy: ForecastAccuracy, basis: str, recorded_at: datetime
) -> AccuracySnapshot:
    return AccuracySnapshot(
        recorded_at=recorded_at,
        basis=basis,
        city=accuracy.city,
        model=accuracy.model,
        horizon_hours=accuracy.horizon_hours,
        training_samples=accuracy.training_samples,
        scored_points=len(accuracy.points),
        mean_absolute_error=accuracy.mean_absolute_error,
        root_mean_square_error=accuracy.root_mean_square_error,
        band_accuracy_pct=accuracy.band_accuracy_pct,
    )


def _score(
    pairs: list[ForecastAccuracyPoint],
    model: str,
    training_samples: int,
) -> ForecastAccuracy:
    """The three metrics over an already-joined set of predicted/observed pairs."""
    same_band = sum(
        1 for pair in pairs if pair.predicted_category == pair.observed_category
    )
    return ForecastAccuracy(
        city=CITY_NAME,
        model=model,
        horizon_hours=len(pairs),
        training_samples=training_samples,
        evaluated_from=pairs[0].predicted_for,
        mean_absolute_error=round(_mean_absolute_error(pairs), 1),
        root_mean_square_error=round(_root_mean_square_error(pairs), 1),
        band_accuracy_pct=round(100.0 * same_band / len(pairs), 1),
        points=pairs,
    )


def _earliest_per_hour(
    points: list[StoredForecastPoint],
) -> dict[datetime, StoredForecastPoint]:
    earliest: dict[datetime, StoredForecastPoint] = {}
    for point in points:
        hour = point.predicted_for.replace(minute=0, second=0, microsecond=0)
        held = earliest.get(hour)
        if held is None or point.generated_at < held.generated_at:
            earliest[hour] = point
    return earliest


async def verify_recorded_forecasts(
    window_hours: int = VERIFICATION_WINDOW_HOURS,
) -> ForecastAccuracy | None:
    """Score the predictions this service actually published.

    Returns None when nothing has matured yet - no recorded prediction whose
    target hour has both passed and been observed. That is the normal state for
    a fresh deployment, not a failure, so it is not an error.
    """
    now = utc_now()
    since = now - timedelta(hours=window_hours)

    recorded = await dynamo_client.list_forecast_points(
        since=since, until=now, limit=MAX_RECORDED_POINTS
    )
    if not recorded:
        return None

    observed = await readings_service.load_readings(
        limit=window_hours, hours=window_hours, include_weather=False
    )
    if not observed:
        return None

    observed_by_hour = {
        point.observed_at.replace(minute=0, second=0, microsecond=0): point
        for point in observed
    }

    pairs: list[ForecastAccuracyPoint] = []
    for hour, prediction in sorted(_earliest_per_hour(recorded).items()):
        actual = observed_by_hour.get(hour)
        if actual is None:
            continue
        lead_hours = max(
            1, round((prediction.predicted_for - prediction.generated_at).total_seconds() / 3600)
        )
        pairs.append(
            ForecastAccuracyPoint(
                predicted_for=prediction.predicted_for,
                hours_ahead=lead_hours,
                predicted_aqi=prediction.predicted_aqi,
                observed_aqi=actual.aqi,
                error=prediction.predicted_aqi - actual.aqi,
                predicted_category=prediction.predicted_category,
                observed_category=category_for_aqi(actual.aqi),
            )
        )

    if not pairs:
        return None

    model = next((point.model for point in recorded if point.model), predictor.MODEL_NAME)
    return _score(pairs, model=model, training_samples=0)


async def record_accuracy(accuracy: ForecastAccuracy) -> None:
    """Store today's hindcast, and the verified score if anything has matured.

    Writes are best-effort in exactly the way `/current` treats persistence:
    the figure has already been computed and is about to be served, so a store
    that is unreachable must not turn a good response into an error.
    """
    if not get_settings().persist_readings:
        return

    recorded_at = utc_now()
    try:
        await dynamo_client.put_accuracy_snapshot(
            _snapshot_from(accuracy, ACCURACY_BASIS_HINDCAST, recorded_at)
        )
    except AppError as exc:
        logger.warning("Could not store hindcast snapshot: %s", exc.code)

    try:
        verified = await verify_recorded_forecasts()
        if verified is not None:
            await dynamo_client.put_accuracy_snapshot(
                _snapshot_from(verified, ACCURACY_BASIS_VERIFIED, recorded_at)
            )
    except AppError as exc:
        logger.warning("Could not store verified snapshot: %s", exc.code)


async def record_forecast(forecast: ForecastResponse) -> None:
    """Record a forecast as it was served, so it can be scored once it matures.

    Best-effort for the same reason as `record_accuracy`: this runs inside the
    `/forecast` loader, and the caller is waiting on a forecast, not on a write.
    """
    if not get_settings().persist_readings:
        return
    try:
        await dynamo_client.put_forecast_points(forecast)
    except AppError as exc:
        logger.warning("Could not record forecast points: %s", exc.code)


async def load_accuracy_history(hours: int, limit: int) -> AccuracyHistoryResponse:
    """Stored accuracy measurements over the window, newest first."""
    since = utc_now() - timedelta(hours=hours)
    snapshots = await dynamo_client.list_accuracy_snapshots(limit=limit, since=since)

    bases: dict[str, int] = {}
    for snapshot in snapshots:
        bases[snapshot.basis] = bases.get(snapshot.basis, 0) + 1

    return AccuracyHistoryResponse(
        city=CITY_NAME,
        count=len(snapshots),
        bases=bases,
        snapshots=snapshots,
    )


def build_backfill_snapshots(
    readings: list[HistoryPoint],
    horizon_hours: int,
    step_hours: int,
) -> list[AccuracySnapshot]:
    """Hindcast the model repeatedly at past cut-offs, to seed the trend.

    Walks back through the stored history and, at each cut, refits on
    everything before it and scores the following `horizon_hours`. The result
    is what a hindcast would have reported had it been run at that moment.

    These are labelled `hindcast` like any other, and the timestamp is the last
    hour that fed the fit - never the time the backfill ran, which would claim
    a measurement that did not happen then.
    """
    ordered = sorted(readings, key=lambda point: point.observed_at)
    minimum = predictor.MIN_TRAINING_SAMPLES + horizon_hours

    snapshots: list[AccuracySnapshot] = []
    for cut in range(len(ordered), minimum - 1, -step_hours):
        window = ordered[:cut]
        try:
            accuracy = _evaluate(window, horizon_hours)
        except InsufficientDataError:
            continue
        snapshots.append(
            _snapshot_from(
                accuracy,
                ACCURACY_BASIS_HINDCAST,
                window[-1].observed_at,
            )
        )
    return snapshots
