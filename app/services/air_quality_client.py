"""Open-Meteo Air Quality API - live and historical AQI for the city.

Chosen as the history source because it needs no API key, reaches back years,
and reports `us_aqi` directly, so archive points land on the same 0-500 scale as
the live WAQI reading with no conversion in between.

Timestamps are requested as unix epochs (`timeformat=unixtime`) so there is no
naive-local-time ambiguity to resolve.
"""

from __future__ import annotations

import logging
import math

import httpx

from app.config import (
    CITY_LATITUDE,
    CITY_LONGITUDE,
    CITY_NAME,
    READING_STALE_AFTER_HOURS,
)
from app.errors import UpstreamError
from app.models.aqi import category_for_aqi, overall_aqi
from app.models.schemas import (
    SOURCE_ARCHIVE,
    SOURCE_OPEN_METEO_AQ,
    Concentrations,
    CurrentReading,
    HistoryPoint,
)
from app.services.http_client import get_client
from app.services.timestamps import parse_provider_timestamp, utc_now

logger = logging.getLogger(__name__)

_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# The API caps how far back a request may reach.
MAX_PAST_DAYS = 92
_HOURLY_FIELDS = "us_aqi,pm2_5,pm10"


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return None if math.isnan(numeric) else numeric
    return None


async def fetch_archive_history(hours: int) -> list[HistoryPoint]:
    """Hourly archive AQI for the last `hours`, newest first.

    Weather features are not filled in here - the caller joins them on, because
    they come from a different Open-Meteo endpoint.
    """
    past_days = min(MAX_PAST_DAYS, max(1, math.ceil(hours / 24)))
    try:
        response = await get_client().get(
            _URL,
            params={
                "latitude": str(CITY_LATITUDE),
                "longitude": str(CITY_LONGITUDE),
                "hourly": _HOURLY_FIELDS,
                "past_days": str(past_days),
                "forecast_days": "0",
                "timeformat": "unixtime",
            },
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Open-Meteo air quality request failed: %s", exc)
        raise UpstreamError("The air quality archive is unavailable.") from exc
    except ValueError as exc:
        raise UpstreamError("The air quality archive returned an unreadable response.") from exc

    hourly = body.get("hourly") if isinstance(body, dict) else None
    if not isinstance(hourly, dict):
        raise UpstreamError("The air quality archive returned no hourly series.")

    times = hourly.get("time") or []
    indices = hourly.get("us_aqi") or []
    pm25_series = hourly.get("pm2_5") or []
    pm10_series = hourly.get("pm10") or []
    if not isinstance(times, list) or not isinstance(indices, list):
        raise UpstreamError("The air quality archive returned an unexpected series.")

    points: list[HistoryPoint] = []
    for position, raw_time in enumerate(times):
        observed_at = parse_provider_timestamp(raw_time)
        aqi = _to_float(indices[position]) if position < len(indices) else None
        if observed_at is None:
            continue
        if aqi is None:
            # Fall back to deriving the index from particulates when the archive
            # has a gap in `us_aqi` but still reported concentrations.
            pm25 = _to_float(pm25_series[position]) if position < len(pm25_series) else None
            pm10 = _to_float(pm10_series[position]) if position < len(pm10_series) else None
            derived = overall_aqi(pm25, pm10)
            if derived is None:
                continue
            aqi = float(derived[0])

        points.append(
            HistoryPoint(
                observed_at=observed_at,
                aqi=round(aqi),
                source=SOURCE_ARCHIVE,
            )
        )

    points.sort(key=lambda point: point.observed_at, reverse=True)
    return points[:hours]


# Open-Meteo component keys -> our concentration field names.
_COMPONENT_FIELDS = {
    "pm2_5": "pm25",
    "pm10": "pm10",
    "ozone": "o3",
    "nitrogen_dioxide": "no2",
    "sulphur_dioxide": "so2",
    "carbon_monoxide": "co",
    "ammonia": "nh3",
}

_CURRENT_FIELDS = "us_aqi," + ",".join(_COMPONENT_FIELDS)


async def fetch_current_reading() -> CurrentReading:
    """Live AQI and concentrations for the city.

    This is the headline source: it reports `us_aqi` on the same 0-500 scale as
    WAQI, needs no key, and - unlike WAQI's Pakistan stations - is actually
    current.
    """
    try:
        response = await get_client().get(
            _URL,
            params={
                "latitude": str(CITY_LATITUDE),
                "longitude": str(CITY_LONGITUDE),
                "current": _CURRENT_FIELDS,
                "timeformat": "unixtime",
            },
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Open-Meteo current air quality request failed: %s", exc)
        raise UpstreamError("The air quality provider is unavailable.") from exc
    except ValueError as exc:
        raise UpstreamError("The air quality provider returned an unreadable response.") from exc

    current = body.get("current") if isinstance(body, dict) else None
    if not isinstance(current, dict):
        raise UpstreamError("The air quality provider returned no current reading.")

    observed_at = parse_provider_timestamp(current.get("time"))
    if observed_at is None:
        raise UpstreamError("The air quality provider reported no observation time.")

    concentrations = Concentrations(
        **{
            field_name: _to_float(current.get(source_key))
            for source_key, field_name in _COMPONENT_FIELDS.items()
        }
    )

    # Prefer the provider's own index; fall back to deriving one so a gap in the
    # series does not take the endpoint down.
    provider_aqi = _to_float(current.get("us_aqi"))
    derived = overall_aqi(concentrations.pm25, concentrations.pm10)

    if provider_aqi is not None:
        aqi = provider_aqi
        # Open-Meteo's us_aqi is the full EPA index: it folds in ozone, NO2, SO2
        # and CO over their own averaging windows. We only compute particulate
        # sub-indices, so on a day when ozone is driving the number, naming pm25
        # would be a guess dressed as a fact. Report nothing instead.
        dominant_pollutant = None
    elif derived is not None:
        # We computed this index ourselves from particulates alone, so the
        # pollutant that produced it is known.
        aqi, dominant_pollutant = float(derived[0]), derived[1]
    else:
        raise UpstreamError("The air quality provider reported no usable index.")

    age_hours = max(0.0, (utc_now() - observed_at).total_seconds() / 3600.0)

    return CurrentReading(
        city=CITY_NAME,
        source=SOURCE_OPEN_METEO_AQ,
        aqi=round(aqi),
        category=category_for_aqi(aqi),
        dominant_pollutant=dominant_pollutant,
        observed_at=observed_at,
        age_hours=round(age_hours, 2),
        is_stale=age_hours > READING_STALE_AFTER_HOURS,
        latitude=CITY_LATITUDE,
        longitude=CITY_LONGITUDE,
        concentrations=concentrations,
    )
