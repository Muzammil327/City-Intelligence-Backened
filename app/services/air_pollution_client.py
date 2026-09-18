"""OpenWeatherMap Air Pollution API.

Kept separate from `weather_client` because it answers a different question, and
separate from `waqi_client` because the two report different things:

* WAQI reports **sub-indices** already on the 0-500 US AQI scale.
* OpenWeatherMap reports **mass concentrations** in ug/m3, plus its own 1-5
  index that is *not* the US AQI scale.

Mixing those two numbers would be silently wrong, so this module never writes
into the `aqi` field. It exposes the concentrations, OWM's index under its own
name, and a US AQI derived from the particulates via the EPA breakpoints.
"""

from __future__ import annotations

import logging

import httpx

from app.config import CITY_LATITUDE, CITY_LONGITUDE, get_settings
from app.errors import ConfigurationError, UpstreamError
from app.models.aqi import category_for_aqi, overall_aqi
from app.models.schemas import AirPollution, Concentrations
from app.services.http_client import get_client
from app.services.timestamps import parse_provider_timestamp

logger = logging.getLogger(__name__)

_URL = "https://api.openweathermap.org/data/2.5/air_pollution"

SOURCE_OPENWEATHER_POLLUTION = "openweathermap-air-pollution"

# OWM component keys -> our field names.
_COMPONENT_FIELDS = {
    "pm2_5": "pm25",
    "pm10": "pm10",
    "o3": "o3",
    "no2": "no2",
    "so2": "so2",
    "co": "co",
    "nh3": "nh3",
}


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


async def fetch_air_pollution() -> AirPollution:
    """Current pollutant concentrations for the city."""
    settings = get_settings()
    if not settings.has_openweather:
        raise ConfigurationError(
            "Air pollution data is unavailable: OPENWEATHER_API_KEY is not set."
        )

    try:
        response = await get_client().get(
            _URL,
            params={
                "lat": str(CITY_LATITUDE),
                "lon": str(CITY_LONGITUDE),
                "appid": settings.openweather_api_key,
            },
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            logger.warning("OpenWeatherMap rejected the air pollution request")
            raise ConfigurationError(
                "Air pollution data is unavailable: the OpenWeatherMap key was rejected."
            ) from exc
        raise UpstreamError("The air pollution provider is unavailable.") from exc
    except httpx.HTTPError as exc:
        logger.warning("OpenWeatherMap air pollution request failed: %s", exc)
        raise UpstreamError("The air pollution provider is unavailable.") from exc
    except ValueError as exc:
        raise UpstreamError("The air pollution provider returned an unreadable response.") from exc

    entries = body.get("list") if isinstance(body, dict) else None
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        raise UpstreamError("The air pollution provider returned no reading.")

    entry = entries[0]
    measured_at = parse_provider_timestamp(entry.get("dt"))
    if measured_at is None:
        raise UpstreamError("The air pollution provider reported no measurement time.")

    raw_components = entry.get("components") if isinstance(entry.get("components"), dict) else {}
    values: dict[str, float | None] = {}
    for source_key, field_name in _COMPONENT_FIELDS.items():
        values[field_name] = _to_float(raw_components.get(source_key))
    concentrations = Concentrations(**values)

    main = entry.get("main") if isinstance(entry.get("main"), dict) else {}
    owm_index = _to_float(main.get("aqi"))

    derived = overall_aqi(concentrations.pm25, concentrations.pm10)
    us_aqi, dominant = derived if derived is not None else (None, None)

    return AirPollution(
        source=SOURCE_OPENWEATHER_POLLUTION,
        measured_at=measured_at,
        concentrations=concentrations,
        owm_index=round(owm_index) if owm_index is not None else None,
        us_aqi=us_aqi,
        us_aqi_category=category_for_aqi(us_aqi) if us_aqi is not None else None,
        dominant_pollutant=dominant,
    )


async def fetch_air_pollution_or_none() -> AirPollution | None:
    """Supplementary on /current - a missing key or an outage must not fail it."""
    try:
        return await fetch_air_pollution()
    except (ConfigurationError, UpstreamError) as exc:
        logger.info("Air pollution reading unavailable: %s", exc.code)
        return None
