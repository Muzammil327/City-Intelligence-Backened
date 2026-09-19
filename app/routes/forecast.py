"""GET /forecast - predicted AQI for the next 6-24 hours."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.models.schemas import ForecastResponse
from app.services import cache, predictor, verification
from app.services import readings as readings_service

router = APIRouter(tags=["forecast"])

# The model is refit from stored history on every call, so this is CPU as well
# as network. The longest TTL of the four for that reason.
CACHE_TTL_SECONDS = 30 * 60

DEFAULT_HORIZON_HOURS = 12
# How much history to fit on. Bounded like every other read.
TRAINING_WINDOW_HOURS = 168


@router.get("/forecast", response_model=ForecastResponse, summary="Predicted AQI")
async def get_forecast(
    hours: int = Query(
        DEFAULT_HORIZON_HOURS,
        ge=predictor.MIN_HORIZON_HOURS,
        le=predictor.MAX_HORIZON_HOURS,
        description="Forecast horizon in hours.",
    ),
) -> ForecastResponse:
    async def load() -> ForecastResponse:
        history = await readings_service.load_readings(
            limit=TRAINING_WINDOW_HOURS, hours=TRAINING_WINDOW_HOURS
        )
        forecast = await predictor.forecast_aqi(history, horizon_hours=hours)
        # Recorded inside the loader, so it runs once per cache window rather
        # than once per request - the same arrangement /current uses. Without
        # this there is no record of what was predicted, and accuracy can only
        # ever be a hindcast.
        await verification.record_forecast(forecast)
        return forecast

    return await cache.cached(f"forecast:{hours}", CACHE_TTL_SECONDS, load)
