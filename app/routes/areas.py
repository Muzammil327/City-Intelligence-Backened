"""GET /areas - neighbourhood readings and a representative city summary."""

from __future__ import annotations

from fastapi import APIRouter

from app.models.schemas import AreasResponse
from app.services import areas as areas_service

router = APIRouter(tags=["areas"])


@router.get("/areas", response_model=AreasResponse, summary="Neighbourhood AQI")
async def get_areas() -> AreasResponse:
    return await areas_service.fetch_areas()