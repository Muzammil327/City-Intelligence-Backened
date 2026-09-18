"""GET /areas - neighbourhood readings and a representative city summary."""

from __future__ import annotations

from fastapi import APIRouter

from app.models.schemas import AreasResponse
from app.services import areas as areas_service
from app.services import cache

router = APIRouter(tags=["areas"])

# The most expensive endpoint here: one Open-Meteo call per neighbourhood.
CACHE_TTL_SECONDS = 10 * 60


@router.get("/areas", response_model=AreasResponse, summary="Neighbourhood AQI")
async def get_areas() -> AreasResponse:
    return await cache.cached("areas", CACHE_TTL_SECONDS, areas_service.fetch_areas)