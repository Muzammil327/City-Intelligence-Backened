"""GET /forecast/accuracy - how close the model's last predictions were."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.models.schemas import ForecastAccuracy
from app.services import cache, verification

router = APIRouter(tags=["forecast"])

DEFAULT_HORIZON_HOURS = 24
MIN_HORIZON_HOURS = 6
MAX_HORIZON_HOURS = 48

# A hindcast refits the model, so it costs what /forecast costs. The answer only
# moves when a new hour is stored, so it is cached for the same window.
CACHE_TTL_SECONDS = 30 * 60


@router.get(
    "/forecast/accuracy",
    response_model=ForecastAccuracy,
    summary="Out-of-sample forecast skill",
)
async def get_forecast_accuracy(
    hours: int = Query(
        DEFAULT_HORIZON_HOURS,
        ge=MIN_HORIZON_HOURS,
        le=MAX_HORIZON_HOURS,
        description="How many recent hours to hold out and score against.",
    ),
) -> ForecastAccuracy:
    return await cache.cached(
        f"accuracy:{hours}",
        CACHE_TTL_SECONDS,
        lambda: verification.evaluate_forecast(hours),
    )
