"""GET /stations — every reporting station in the city, for the map."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import CITY_NAME
from app.models.schemas import StationsResponse
from app.services import waqi_client

router = APIRouter(tags=["stations"])


@router.get("/stations", response_model=StationsResponse, summary="Station-level AQI")
async def get_stations() -> StationsResponse:
    stations = await waqi_client.fetch_stations()
    return StationsResponse(city=CITY_NAME, count=len(stations), stations=stations)
