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

import anyio

from app.config import CITY_NAME
from app.errors import InsufficientDataError
from app.models.aqi import category_for_aqi
from app.models.schemas import (
    ForecastAccuracy,
    ForecastAccuracyPoint,
    HistoryPoint,
)
from app.services import predictor
from app.services import readings as readings_service

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
