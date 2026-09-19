"""Neighbourhood-level readings for the city, from Open-Meteo's gridded model.

Lahore has no official per-area monitoring network (WAQI reports no stations in
the city, let alone per district), so this feed reads Open-Meteo's air-quality
model at the real coordinates of known neighbourhoods. Each point is a genuine
model reading for a genuine place - nothing here is fabricated - but it is
model-derived, and every area carries `source: "open-meteo-model"` and
`basis: "gridded-model"` so it is never mistaken for a physical station.

The city headline (`/current`) and the per-area feed are independent: the
headline uses the city-centre point plus WAQI/OpenWeatherMap as before, and
`/areas` is purely the neighbourhood grid.

Overall summary calculation (documented - see below): the mean of the area AQI
values. Every area carries Open-Meteo's `us_aqi`, so the summary, the endpoints
printed beside it and the city headline in /current are all the same index over
the same pollutants. This is one representative number, not a measured value.
"""

from __future__ import annotations

import asyncio
import logging
import math

import httpx

from app.config import CITY_NAME, NEIGHBORHOODS, READING_STALE_AFTER_HOURS, get_settings
from app.errors import UpstreamError
from app.models.aqi import category_for_aqi
from app.models.schemas import (
    AreaReading,
    AreasResponse,
    OverallSummary,
)
from app.services.http_client import get_client
from app.services.timestamps import parse_provider_timestamp, utc_now

logger = logging.getLogger(__name__)

_AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

SOURCE_OPEN_METEO_MODEL = "open-meteo-model"
SOURCE_OPEN_METEO_WEATHER = "open-meteo"
BASIS_GRIDDED_MODEL = "gridded-model"

_AQ_CURRENT_FIELDS = ",".join(
    (
        # AQI + concentrations.
        "us_aqi",
        "pm2_5",
        "pm10",
    )
)

_WEATHER_CURRENT_FIELDS = ",".join(
    (
        "temperature_2m",
        "relative_humidity_2m",
        "wind_speed_10m",
        "wind_direction_10m",
    )
)


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return None if math.isnan(numeric) else numeric
    return None


async def _get_current(url: str, params: dict[str, str], what: str) -> dict:
    """One current-reading request, with the shared error handling."""
    try:
        response = await get_client().get(url, params=params)
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Open-Meteo %s request failed for %s: %s", what, params.get("latitude"), exc)
        raise UpstreamError(f"The {what} provider is unavailable.") from exc
    except ValueError as exc:
        raise UpstreamError(f"The {what} provider returned an unreadable response.") from exc
    if not isinstance(body, dict):
        raise UpstreamError(f"The {what} provider returned an unexpected response.")
    return body


async def _fetch_area(name: str, latitude: float, longitude: float) -> AreaReading:
    """One neighbourhood reading: model AQI + concentrations + weather.

    AQI and weather come from different Open-Meteo endpoints, so both fire
    together. A failed weather fetch degrades to missing weather rather than
    dropping the area; a failed air-quality fetch drops the area.
    """
    aq_body, weather_body = await asyncio.gather(
        _get_current(
            _AQ_URL,
            {
                "latitude": str(latitude),
                "longitude": str(longitude),
                "current": _AQ_CURRENT_FIELDS,
                "timeformat": "unixtime",
            },
            "air quality",
        ),
        _get_current(
            _WEATHER_URL,
            {
                "latitude": str(latitude),
                "longitude": str(longitude),
                "current": _WEATHER_CURRENT_FIELDS,
                "timeformat": "unixtime",
            },
            "weather",
        ),
        return_exceptions=True,
    )

    current = aq_body.get("current") if isinstance(aq_body, dict) else None
    if not isinstance(current, dict):
        raise UpstreamError("The area provider reported no current reading.")

    observed_at = parse_provider_timestamp(current.get("time"))
    aqi = _to_float(current.get("us_aqi"))
    if observed_at is None or aqi is None:
        raise UpstreamError("The area provider reported no usable reading.")

    weather = weather_body.get("current") if isinstance(weather_body, dict) else {}
    age_hours = max(0.0, (utc_now() - observed_at).total_seconds() / 3600.0)
    slug = name.lower().replace(" ", "-")

    return AreaReading(
        uid=f"lhr-{slug}",
        name=name,
        latitude=latitude,
        longitude=longitude,
        source=SOURCE_OPEN_METEO_MODEL,
        basis=BASIS_GRIDDED_MODEL,
        observed_at=observed_at,
        age_hours=round(age_hours, 2),
        is_stale=age_hours > READING_STALE_AFTER_HOURS,
        aqi=round(aqi),
        pm25=_to_float(current.get("pm2_5")),
        pm10=_to_float(current.get("pm10")),
        temperature_c=_to_float(weather.get("temperature_2m")),
        humidity_pct=_to_float(weather.get("relative_humidity_2m")),
        wind_speed_ms=_to_float(weather.get("wind_speed_10m")),
        wind_direction_deg=_to_float(weather.get("wind_direction_10m")),
        weather_source=SOURCE_OPEN_METEO_WEATHER
        if isinstance(weather_body, dict)
        else None,
    )


def _overall(areas: list[AreaReading]) -> OverallSummary:
    """The city-wide summary from the area points.

    Method: the mean of the area AQI values. Each area carries Open-Meteo's
    `us_aqi`, which is also what /current reports for the city point, so the
    summary, the highest/lowest figures printed beside it and the headline
    gauge are all the same index over the same pollutants.

    This deliberately does *not* recompute an index from the mean PM2.5/PM10.
    Doing that created a second, particulate-only definition of AQI: ozone and
    the other gases are in `us_aqi` but were excluded here, so a city whose
    worst area read Very Unhealthy could show a Moderate summary immediately
    above it. Two numbers on one screen that could not both be true.

    The result is a representative number, never a point measurement.
    """
    indices = [area.aqi for area in areas if area.aqi is not None]
    mean_aqi = round(sum(indices) / len(indices)) if indices else None

    # Reported beside the index for context, no longer used to derive it.
    pm25_values = [area.pm25 for area in areas if area.pm25 is not None]
    overall_pm25 = sum(pm25_values) / len(pm25_values) if pm25_values else None

    ranked = sorted(areas, key=lambda area: area.aqi)
    lowest = ranked[0] if ranked else None
    highest = ranked[-1] if ranked else None

    return OverallSummary(
        aqi=mean_aqi if mean_aqi is not None else 0,
        category=category_for_aqi(mean_aqi) if mean_aqi is not None else "Good",
        pm25=overall_pm25,
        areas_with_data=len([area for area in areas if area.aqi is not None]),
        area_count=len(areas),
        highest_name=highest.name if highest else None,
        highest_aqi=highest.aqi if highest else None,
        lowest_name=lowest.name if lowest else None,
        lowest_aqi=lowest.aqi if lowest else None,
        observed_at=max((area.observed_at for area in areas), default=None),
    )


async def fetch_areas() -> AreasResponse:
    """Current reading for every neighbourhood point, worst air first."""
    # All areas are independent of one another, so they go out together. A
    # single failed area degrades that area to absent rather than failing the
    # whole feed.
    results = await asyncio.gather(
        *(
            _fetch_area(name, latitude, longitude)
            for name, latitude, longitude in NEIGHBORHOODS
        ),
        return_exceptions=True,
    )

    areas = [area for area in results if isinstance(area, AreaReading)]
    if not areas:
        raise UpstreamError("No area readings are available.")

    # Deterministic order: worst air first, then by name so ties do not shuffle.
    areas.sort(key=lambda area: (-area.aqi, area.name))

    return AreasResponse(
        city=CITY_NAME,
        count=len(areas),
        overall=_overall(areas),
        areas=areas,
    )