"""World Air Quality Index (WAQI) API wrapper.

Everything WAQI-shaped stops here: routes and other services only ever see our
own models.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import (
    CITY_BOUNDS,
    CITY_SLUG,
    READING_STALE_AFTER_HOURS,
    get_settings,
)
from app.errors import AppError, ConfigurationError, UpstreamError
from app.models.aqi import category_for_aqi
from app.models.schemas import Pollutants, Station, WaqiReading
from app.services.http_client import get_client
from app.services.timestamps import parse_provider_timestamp, utc_now

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.waqi.info"

# WAQI keys -> our field names. Anything not listed is dropped rather than
# guessed at.
_POLLUTANT_FIELDS = {
    "pm25": "pm25",
    "pm10": "pm10",
    "o3": "o3",
    "no2": "no2",
    "so2": "so2",
    "co": "co",
}


def _require_token() -> str:
    token = get_settings().waqi_token
    if not token:
        raise ConfigurationError("Air quality data is unavailable: WAQI_TOKEN is not set.")
    return token


async def _get(path: str, params: dict[str, str]) -> Any:
    """Call WAQI and return the `data` payload, or raise a typed error.

    The payload is a dict for a feed and a list for a bounds query, and an empty
    list is a valid answer meaning "no stations here" - so it is returned as-is
    rather than coalesced into a default.
    """
    try:
        response = await get_client().get(f"{_BASE_URL}{path}", params=params)
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        logger.warning("WAQI request failed for %s: %s", path, exc)
        raise UpstreamError("The air quality provider is unavailable.") from exc
    except ValueError as exc:  # non-JSON body
        logger.warning("WAQI returned a non-JSON body for %s", path)
        raise UpstreamError("The air quality provider returned an unreadable response.") from exc

    status = body.get("status")
    if status != "ok":
        # WAQI puts the reason in `data` as a string (e.g. "Invalid key").
        logger.warning("WAQI rejected %s: %s", path, body.get("data"))
        if isinstance(body.get("data"), str) and "key" in body["data"].lower():
            raise ConfigurationError("Air quality data is unavailable: the WAQI token was rejected.")
        raise UpstreamError("The air quality provider returned an error.")

    return body.get("data")


def _to_float(value: object) -> float | None:
    """WAQI sends numbers, numeric strings, and '-' for 'no reading'."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_pollutants(iaqi: object) -> Pollutants:
    if not isinstance(iaqi, dict):
        return Pollutants()
    values: dict[str, float | None] = {}
    for source_key, field_name in _POLLUTANT_FIELDS.items():
        entry = iaqi.get(source_key)
        if isinstance(entry, dict):
            values[field_name] = _to_float(entry.get("v"))
    return Pollutants(**values)


async def fetch_waqi_reading() -> WaqiReading:
    """WAQI's station reading for the city.

    Supplementary, not the headline number: WAQI's Pakistan stations stopped
    reporting in early 2025, so this carries its own age and staleness flag.
    """
    data = await _get(f"/feed/{CITY_SLUG}/", {"token": _require_token()})
    if not isinstance(data, dict):
        raise UpstreamError("The air quality provider returned an unexpected reading.")

    aqi = _to_float(data.get("aqi"))
    if aqi is None:
        raise UpstreamError("The air quality provider reported no index for this city.")

    city = data.get("city") if isinstance(data.get("city"), dict) else {}
    time_block = data.get("time") if isinstance(data.get("time"), dict) else {}
    observed_at = parse_provider_timestamp(time_block.get("iso") or time_block.get("s"))
    if observed_at is None:
        raise UpstreamError("The air quality provider reported no observation time.")

    age_hours = max(0.0, (utc_now() - observed_at).total_seconds() / 3600.0)

    return WaqiReading(
        aqi=round(aqi),
        category=category_for_aqi(aqi),
        observed_at=observed_at,
        age_hours=round(age_hours, 2),
        is_stale=age_hours > READING_STALE_AFTER_HOURS,
        station_name=city.get("name") or None,
        dominant_pollutant=data.get("dominentpol") or None,
        pollutants=_parse_pollutants(data.get("iaqi")),
    )


async def fetch_waqi_reading_or_none() -> WaqiReading | None:
    """Supplementary on /current - a missing token or an outage must not fail it."""
    try:
        return await fetch_waqi_reading()
    except AppError as exc:
        logger.info("WAQI reading unavailable: %s", exc.code)
        return None


async def fetch_stations() -> list[Station]:
    """Every station WAQI reports inside the city bounding box."""
    lat_min, lon_min, lat_max, lon_max = CITY_BOUNDS
    data = await _get(
        "/map/bounds/",
        {
            "token": _require_token(),
            "latlng": f"{lat_min},{lon_min},{lat_max},{lon_max}",
        },
    )

    if not isinstance(data, list):
        raise UpstreamError("The air quality provider returned an unexpected station list.")

    stations: list[Station] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        latitude = _to_float(entry.get("lat"))
        longitude = _to_float(entry.get("lon"))
        uid = entry.get("uid")
        if latitude is None or longitude is None or uid is None:
            continue

        station_block = entry.get("station") if isinstance(entry.get("station"), dict) else {}
        aqi = _to_float(entry.get("aqi"))  # '-' when the station is offline
        stations.append(
            Station(
                uid=str(uid),
                name=station_block.get("name") or f"Station {uid}",
                latitude=latitude,
                longitude=longitude,
                aqi=round(aqi) if aqi is not None else None,
                category=category_for_aqi(aqi) if aqi is not None else None,
                observed_at=parse_provider_timestamp(station_block.get("time")),
            )
        )

    # An empty list is a valid answer, not a failure: WAQI reports no active
    # station inside the city bounds, which is precisely why /areas reads a
    # gridded model instead. Raising here made a normal condition look like an
    # outage to every caller. A provider that is genuinely unreachable or
    # returns something unreadable still raises, above.

    # Deterministic order: worst air first, then by name so ties do not shuffle.
    stations.sort(key=lambda s: (-(s.aqi or -1), s.name))
    return stations
