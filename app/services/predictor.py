"""Short-horizon AQI forecasting.

The dataset is small, so the model is refit from stored history on every call and
nothing is persisted. Features are time-of-day, a linear trend, and the weather
values that travel with each reading.

Future wind and humidity come from Open-Meteo's hourly forecast, so the model
projects onto real predicted weather rather than an assumption. When that call
fails the last observed values are carried forward instead, and the response
says which of the two happened via `weatherBasis`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import anyio
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.config import CITY_NAME
from app.errors import InsufficientDataError
from app.models.aqi import category_for_aqi, clamp_aqi
from app.models.schemas import (
    WEATHER_BASIS_FORECAST,
    WEATHER_BASIS_PERSISTED,
    ForecastPoint,
    ForecastResponse,
    HistoryPoint,
)
from app.services.weather_client import HourlyConditions, fetch_hourly_conditions_or_empty
from app.services.timestamps import utc_now

logger = logging.getLogger(__name__)

MODEL_NAME = "ridge"
MIN_TRAINING_SAMPLES = 12
MIN_HORIZON_HOURS = 6
MAX_HORIZON_HOURS = 24

_FEATURE_COLUMNS = (
    "trend_hours",
    "hour_sin",
    "hour_cos",
    "wind_speed_ms",
    "humidity_pct",
)


def _build_frame(readings: list[HistoryPoint]) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "observed_at": point.observed_at,
                "aqi": float(point.aqi),
                "wind_speed_ms": point.wind_speed_ms,
                "humidity_pct": point.humidity_pct,
            }
            for point in readings
        ]
    )
    frame = frame.sort_values("observed_at").drop_duplicates(
        subset="observed_at", keep="last"
    )
    return frame.reset_index(drop=True)


def _add_features(frame: pd.DataFrame, origin: datetime) -> pd.DataFrame:
    hours = (frame["observed_at"] - origin).dt.total_seconds() / 3600.0
    hour_of_day = frame["observed_at"].dt.hour + frame["observed_at"].dt.minute / 60.0

    featured = frame.copy()
    featured["trend_hours"] = hours
    # Time of day is cyclical: 23:00 is adjacent to 00:00, not 23 hours from it.
    featured["hour_sin"] = np.sin(2 * np.pi * hour_of_day / 24.0)
    featured["hour_cos"] = np.cos(2 * np.pi * hour_of_day / 24.0)
    return featured


def _fill_weather_gaps(frame: pd.DataFrame) -> tuple[pd.DataFrame, float, float]:
    """Fill missing weather with the column median, and return the carry-forward values."""
    filled = frame.copy()
    carried: dict[str, float] = {}
    for column in ("wind_speed_ms", "humidity_pct"):
        series = pd.to_numeric(filled[column], errors="coerce")
        median = series.median()
        if pd.isna(median):
            median = 0.0
        filled[column] = series.fillna(median)
        # The latest known value is what we assume holds over the horizon.
        last_known = series.dropna()
        carried[column] = float(last_known.iloc[-1]) if not last_known.empty else float(median)
    return filled, carried["wind_speed_ms"], carried["humidity_pct"]


def _future_timestamps(last_observed: datetime, horizon_hours: int) -> list[datetime]:
    # Start at the next whole hour after the last reading.
    start = (last_observed + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return [start + timedelta(hours=offset) for offset in range(horizon_hours)]


def _project_weather(
    timestamps: list[datetime],
    forecast_conditions: dict[datetime, HourlyConditions],
    carried_wind: float,
    carried_humidity: float,
) -> tuple[list[float], list[float], str]:
    """Wind and humidity for each future hour, preferring the real forecast."""
    winds: list[float] = []
    humidities: list[float] = []
    used_forecast = False

    for timestamp in timestamps:
        hourly = forecast_conditions.get(timestamp.replace(minute=0, second=0, microsecond=0))
        wind = hourly.wind_speed_ms if hourly is not None else None
        humidity = hourly.humidity_pct if hourly is not None else None
        if wind is not None or humidity is not None:
            used_forecast = True
        winds.append(carried_wind if wind is None else wind)
        humidities.append(carried_humidity if humidity is None else humidity)

    basis = WEATHER_BASIS_FORECAST if used_forecast else WEATHER_BASIS_PERSISTED
    return winds, humidities, basis


def _fit_and_predict(
    readings: list[HistoryPoint],
    horizon_hours: int,
    forecast_conditions: dict[datetime, HourlyConditions],
) -> ForecastResponse:
    frame = _build_frame(readings)
    if len(frame) < MIN_TRAINING_SAMPLES:
        raise InsufficientDataError(
            f"Need at least {MIN_TRAINING_SAMPLES} stored readings to forecast; "
            f"{len(frame)} are available."
        )

    origin = frame["observed_at"].iloc[0]
    frame, carried_wind, carried_humidity = _fill_weather_gaps(frame)
    frame = _add_features(frame, origin)

    features = frame[list(_FEATURE_COLUMNS)].to_numpy(dtype=float)
    target = frame["aqi"].to_numpy(dtype=float)

    model = Pipeline(
        [
            ("scale", StandardScaler()),
            ("ridge", Ridge(alpha=1.0)),
        ]
    )
    model.fit(features, target)
    in_sample_r2 = float(model.score(features, target))

    last_observed = frame["observed_at"].iloc[-1].to_pydatetime()
    timestamps = _future_timestamps(last_observed, horizon_hours)

    winds, humidities, weather_basis = _project_weather(
        timestamps, forecast_conditions, carried_wind, carried_humidity
    )

    future = pd.DataFrame({"observed_at": pd.to_datetime(pd.Series(timestamps), utc=True)})
    future["wind_speed_ms"] = winds
    future["humidity_pct"] = humidities
    future = _add_features(future, origin)

    predictions = model.predict(future[list(_FEATURE_COLUMNS)].to_numpy(dtype=float))

    points = [
        ForecastPoint(
            predicted_for=timestamp,
            aqi=round(clamp_aqi(value)),
            category=category_for_aqi(clamp_aqi(value)),
        )
        for timestamp, value in zip(timestamps, predictions)
    ]

    return ForecastResponse(
        city=CITY_NAME,
        generated_at=utc_now(),
        horizon_hours=horizon_hours,
        model=MODEL_NAME,
        training_samples=len(frame),
        weather_basis=weather_basis,
        r2_score=round(in_sample_r2, 4),
        points=points,
    )


async def forecast_aqi(readings: list[HistoryPoint], horizon_hours: int) -> ForecastResponse:
    """Fit on the supplied history and predict hourly AQI over the horizon.

    Fitting is CPU-bound and synchronous, so it runs off the event loop.
    """
    if not readings:
        raise InsufficientDataError("No readings are available to forecast from.")

    # Fetched here rather than inside the worker thread: it is I/O, and a failure
    # degrades the forecast instead of ending it.
    forecast_conditions = await fetch_hourly_conditions_or_empty(
        future_hours=horizon_hours + 1
    )
    return await anyio.to_thread.run_sync(
        lambda: _fit_and_predict(readings, horizon_hours, forecast_conditions)
    )
