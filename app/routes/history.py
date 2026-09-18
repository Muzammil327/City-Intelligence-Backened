"""GET /history - stored readings, topped up from the air quality archive."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.config import CITY_NAME
from app.models.schemas import HistoryResponse
from app.services import cache
from app.services import readings as readings_service

router = APIRouter(tags=["history"])

DEFAULT_LIMIT = 24
MAX_LIMIT = 500
MAX_WINDOW_HOURS = 24 * 30

# Stored rows plus an archive top-up, both of which move hourly at most.
CACHE_TTL_SECONDS = 10 * 60


@router.get("/history", response_model=HistoryResponse, summary="Past readings")
async def get_history(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Newest-first row cap."),
    hours: int | None = Query(
        None,
        ge=1,
        le=MAX_WINDOW_HOURS,
        description="Only return readings observed within this many hours.",
    ),
    include_archive: bool = Query(
        True,
        alias="includeArchive",
        description="Fill hours the store does not cover from the Open-Meteo archive.",
    ),
) -> HistoryResponse:
    async def load() -> HistoryResponse:
        points = await readings_service.load_readings(
            limit=limit, hours=hours, include_archive=include_archive
        )
        return HistoryResponse(
            city=CITY_NAME,
            count=len(points),
            sources=readings_service.count_by_source(points),
            readings=points,
        )

    # Keyed by the query, so a 24-hour window and a 7-day one do not share an
    # entry — the frontend asks for both.
    return await cache.cached(
        f"history:{limit}:{hours}:{include_archive}", CACHE_TTL_SECONDS, load
    )
