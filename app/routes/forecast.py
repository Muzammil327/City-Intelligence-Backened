"""GET /forecast - predicted AQI for the next 6-24 hours."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.models.schemas import ForecastResponse
from app.services import predictor
from app.services import readings as readings_service

router = APIRouter(tags=["forecast"])

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
    history = await readings_service.load_readings(
        limit=TRAINING_WINDOW_HOURS, hours=TRAINING_WINDOW_HOURS
    )
    return await predictor.forecast_aqi(history, horizon_hours=hours)
