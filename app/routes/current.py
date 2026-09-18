"""GET /current - live AQI, concentrations, and weather for the city."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from app.config import CITY_LATITUDE, CITY_LONGITUDE, CITY_NAME, get_settings
from app.errors import AppError, UpstreamError
from app.models.aqi import category_for_aqi
from app.models.schemas import (
    SOURCE_WAQI,
    Concentrations,
    CurrentReading,
    WaqiReading,
)
from app.services import (
    air_pollution_client,
    air_quality_client,
    dynamo_client,
    waqi_client,
    weather_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["current"])


def _reading_from_waqi(waqi: WaqiReading) -> CurrentReading:
    """Last resort when the headline provider is down.

    WAQI's sub-indices are not concentrations, so `concentrations` stays empty
    rather than being filled with numbers on the wrong scale. The age and
    staleness flag travel with it so nothing presents it as live.
    """
    return CurrentReading(
        city=CITY_NAME,
        source=SOURCE_WAQI,
        aqi=waqi.aqi,
        category=category_for_aqi(waqi.aqi),
        dominant_pollutant=waqi.dominant_pollutant,
        observed_at=waqi.observed_at,
        age_hours=waqi.age_hours,
        is_stale=waqi.is_stale,
        latitude=CITY_LATITUDE,
        longitude=CITY_LONGITUDE,
        concentrations=Concentrations(),
        waqi=waqi,
    )


@router.get("/current", response_model=CurrentReading, summary="Live AQI and weather")
async def get_current() -> CurrentReading:
    # All four are independent of one another, so they go out together.
    headline, weather, air_pollution, waqi = await asyncio.gather(
        air_quality_client.fetch_current_reading(),
        weather_client.fetch_weather_or_none(),
        air_pollution_client.fetch_air_pollution_or_none(),
        waqi_client.fetch_waqi_reading_or_none(),
        return_exceptions=True,
    )

    if isinstance(headline, BaseException):
        # The headline source failed. Serve WAQI rather than nothing - it is
        # clearly marked stale - and only give up if that is missing too.
        logger.warning("Headline air quality source failed: %s", headline)
        if isinstance(waqi, WaqiReading):
            reading = _reading_from_waqi(waqi)
        elif isinstance(headline, AppError):
            raise headline
        else:
            raise UpstreamError("No air quality provider is available.")
    else:
        reading = headline
        reading.waqi = waqi if isinstance(waqi, WaqiReading) else None

    reading.weather = weather if not isinstance(weather, BaseException) else None
    reading.air_pollution = (
        air_pollution if not isinstance(air_pollution, BaseException) else None
    )

    if get_settings().persist_readings:
        try:
            await dynamo_client.put_reading(reading, reading.weather)
        except AppError as exc:
            # Persistence feeds /history and /forecast; it must never take the
            # live endpoint down with it.
            logger.warning("Could not persist reading: %s", exc.code)

    return reading
