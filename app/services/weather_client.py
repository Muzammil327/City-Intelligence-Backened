"""Weather wrapper: OpenWeatherMap first, Open-Meteo as fallback.

Both providers are normalised to the same `Weather` model, so a caller cannot
tell which one answered except by reading `source`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.config import CITY_LATITUDE, CITY_LONGITUDE, get_settings
from app.errors import UpstreamError
from app.models.schemas import Weather
from app.services.http_client import get_client
from app.services.timestamps import parse_provider_timestamp

logger = logging.getLogger(__name__)

_OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

SOURCE_OPENWEATHER = "openweathermap"
SOURCE_OPEN_METEO = "open-meteo"

# Open-Meteo caps how far either way a single request may reach.
MAX_PAST_DAYS = 92
MAX_FORECAST_DAYS = 16


@dataclass(frozen=True)
class HourlyConditions:
    """The weather features the predictor trains on and projects onto."""

    wind_speed_ms: float | None
    humidity_pct: float | None


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


async def _fetch_json(url: str, params: dict[str, str]) -> dict:
    try:
        response = await get_client().get(url, params=params)
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        raise UpstreamError("The weather provider is unavailable.") from exc
    except ValueError as exc:
        raise UpstreamError("The weather provider returned an unreadable response.") from exc
    if not isinstance(body, dict):
        raise UpstreamError("The weather provider returned an unexpected response.")
    return body


async def _fetch_openweather() -> Weather:
    settings = get_settings()
    body = await _fetch_json(
        _OPENWEATHER_URL,
        {
            "lat": str(CITY_LATITUDE),
            "lon": str(CITY_LONGITUDE),
            "appid": settings.openweather_api_key,
            "units": "metric",
        },
    )

    main = body.get("main") if isinstance(body.get("main"), dict) else {}
    wind = body.get("wind") if isinstance(body.get("wind"), dict) else {}
    conditions_list = body.get("weather") if isinstance(body.get("weather"), list) else []
    conditions = None
    if conditions_list and isinstance(conditions_list[0], dict):
        conditions = conditions_list[0].get("description")

    return Weather(
        source=SOURCE_OPENWEATHER,
        temperature_c=_to_float(main.get("temp")),
        feels_like_c=_to_float(main.get("feels_like")),
        humidity_pct=_to_float(main.get("humidity")),
        pressure_hpa=_to_float(main.get("pressure")),
        wind_speed_ms=_to_float(wind.get("speed")),
        wind_direction_deg=_to_float(wind.get("deg")),
        conditions=conditions,
    )


async def _fetch_open_meteo() -> Weather:
    body = await _fetch_json(
        _OPEN_METEO_URL,
        {
            "latitude": str(CITY_LATITUDE),
            "longitude": str(CITY_LONGITUDE),
            "current": (
                "temperature_2m,apparent_temperature,relative_humidity_2m,"
                "surface_pressure,wind_speed_10m,wind_direction_10m"
            ),
            "wind_speed_unit": "ms",
        },
    )

    current = body.get("current") if isinstance(body.get("current"), dict) else {}
    if not current:
        raise UpstreamError("The weather provider returned no current conditions.")

    return Weather(
        source=SOURCE_OPEN_METEO,
        temperature_c=_to_float(current.get("temperature_2m")),
        feels_like_c=_to_float(current.get("apparent_temperature")),
        humidity_pct=_to_float(current.get("relative_humidity_2m")),
        pressure_hpa=_to_float(current.get("surface_pressure")),
        wind_speed_ms=_to_float(current.get("wind_speed_10m")),
        wind_direction_deg=_to_float(current.get("wind_direction_10m")),
        conditions=None,  # Open-Meteo reports a numeric code, not a phrase.
    )


async def fetch_weather() -> Weather:
    """Current weather for the city. Falls back to Open-Meteo, then raises."""
    if get_settings().has_openweather:
        try:
            return await _fetch_openweather()
        except UpstreamError as exc:
            logger.warning("OpenWeatherMap failed, falling back to Open-Meteo: %s", exc)
    return await _fetch_open_meteo()


async def fetch_weather_or_none() -> Weather | None:
    """Weather is supplementary on the air-quality endpoints — never fatal there."""
    try:
        return await fetch_weather()
    except UpstreamError as exc:
        logger.warning("No weather provider answered: %s", exc)
        return None


async def fetch_hourly_conditions(
    past_hours: int = 0, future_hours: int = 0
) -> dict[datetime, HourlyConditions]:
    """Hourly wind and humidity across a window spanning past and future.

    Keyed by the hour it applies to, so a caller can join it onto AQI readings
    from another provider. Open-Meteo needs no key, which is why the predictor
    can rely on it being there.
    """
    past_days = min(MAX_PAST_DAYS, math.ceil(max(0, past_hours) / 24))
    # +1 so the tail of the horizon is covered even when it crosses midnight.
    forecast_days = min(MAX_FORECAST_DAYS, math.ceil(max(0, future_hours) / 24) + 1)

    body = await _fetch_json(
        _OPEN_METEO_URL,
        {
            "latitude": str(CITY_LATITUDE),
            "longitude": str(CITY_LONGITUDE),
            "hourly": "wind_speed_10m,relative_humidity_2m",
            "wind_speed_unit": "ms",
            "past_days": str(past_days),
            "forecast_days": str(forecast_days),
            "timeformat": "unixtime",
        },
    )

    hourly = body.get("hourly") if isinstance(body.get("hourly"), dict) else None
    if not hourly:
        raise UpstreamError("The weather provider returned no hourly series.")

    times = hourly.get("time") or []
    winds = hourly.get("wind_speed_10m") or []
    humidities = hourly.get("relative_humidity_2m") or []
    if not isinstance(times, list):
        raise UpstreamError("The weather provider returned an unexpected series.")

    conditions: dict[datetime, HourlyConditions] = {}
    for position, raw_time in enumerate(times):
        timestamp = parse_provider_timestamp(raw_time)
        if timestamp is None:
            continue
        conditions[timestamp] = HourlyConditions(
            wind_speed_ms=_to_float(winds[position]) if position < len(winds) else None,
            humidity_pct=_to_float(humidities[position]) if position < len(humidities) else None,
        )
    return conditions


async def fetch_hourly_conditions_or_empty(
    past_hours: int = 0, future_hours: int = 0
) -> dict[datetime, HourlyConditions]:
    """As above, but an outage degrades the forecast instead of failing it."""
    try:
        return await fetch_hourly_conditions(past_hours, future_hours)
    except UpstreamError as exc:
        logger.warning("Hourly weather unavailable: %s", exc)
        return {}
