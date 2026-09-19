"""Forecast skill: the current figure, and how it has moved over time."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.models.schemas import AccuracyHistoryResponse, ForecastAccuracy
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
    async def load() -> ForecastAccuracy:
        accuracy = await verification.evaluate_forecast(hours)
        # Kept so the figure can be trended. Inside the loader for the same
        # reason as /forecast: one write per cache window, not one per request.
        await verification.record_accuracy(accuracy)
        return accuracy

    return await cache.cached(f"accuracy:{hours}", CACHE_TTL_SECONDS, load)


# The trend reads stored snapshots only - it never refits. That makes it cheap,
# and it is why the window can be much wider than a hindcast horizon.
HISTORY_CACHE_TTL_SECONDS = 10 * 60

DEFAULT_HISTORY_HOURS = 168
MIN_HISTORY_HOURS = 24
MAX_HISTORY_HOURS = 24 * 90

DEFAULT_HISTORY_LIMIT = 200
MAX_HISTORY_LIMIT = 500


@router.get(
    "/forecast/accuracy/history",
    response_model=AccuracyHistoryResponse,
    summary="How forecast skill has moved over time",
)
async def get_accuracy_history(
    hours: int = Query(
        DEFAULT_HISTORY_HOURS,
        ge=MIN_HISTORY_HOURS,
        le=MAX_HISTORY_HOURS,
        description="How far back to read stored accuracy snapshots.",
    ),
    limit: int = Query(
        DEFAULT_HISTORY_LIMIT,
        ge=1,
        le=MAX_HISTORY_LIMIT,
        description="Maximum snapshots to return, newest first.",
    ),
) -> AccuracyHistoryResponse:
    return await cache.cached(
        f"accuracy-history:{hours}:{limit}",
        HISTORY_CACHE_TTL_SECONDS,
        lambda: verification.load_accuracy_history(hours=hours, limit=limit),
    )
