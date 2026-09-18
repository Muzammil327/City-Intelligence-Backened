"""DynamoDB access for stored readings.

Table shape this module assumes (see README — nothing here creates or alters it):

    table         DYNAMO_TABLE_NAME
    partition key city         (S)  the city slug, e.g. "lahore"
    sort key      observed_at  (S)  ISO-8601 UTC, e.g. "2026-09-17T10:00:00+00:00"

Every other attribute is optional, so a reading written by an older version of
this service still reads back.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

import anyio
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from app.config import CITY_SLUG, get_settings
from app.errors import ConfigurationError, StaleReadingError, UpstreamError
from app.models.schemas import CurrentReading, HistoryPoint, Weather
from app.services.timestamps import parse_provider_timestamp

logger = logging.getLogger(__name__)

# Only the attributes a caller actually needs — a read that selects everything
# grows silently with the schema.
_HISTORY_ATTRIBUTES = (
    "observed_at",
    "aqi",
    "temperature_c",
    "humidity_pct",
    "wind_speed_ms",
)

_table: Any | None = None


def _get_table() -> Any:
    """One DynamoDB resource per process, reused across requests and reloads."""
    global _table
    if _table is None:
        settings = get_settings()
        resource = boto3.resource("dynamodb", region_name=settings.aws_region)
        _table = resource.Table(settings.dynamo_table_name)
    return _table


def _wrap_boto_error(exc: Exception, action: str) -> Exception:
    """Map a boto failure to one of our errors. Never surfaces the AWS message."""
    if isinstance(exc, NoCredentialsError):
        logger.warning("DynamoDB %s failed: no AWS credentials available", action)
        return ConfigurationError("Stored readings are unavailable: AWS credentials are not configured.")
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        logger.warning("DynamoDB %s failed with %s", action, code)
        if code in {"ResourceNotFoundException", "AccessDeniedException", "UnrecognizedClientException"}:
            return ConfigurationError("Stored readings are unavailable: the readings table is not reachable.")
        return UpstreamError("The readings store is unavailable.")
    logger.warning("DynamoDB %s failed: %s", action, type(exc).__name__)
    return UpstreamError("The readings store is unavailable.")


def _to_decimal(value: float | int | None) -> Decimal | None:
    """DynamoDB has no float type."""
    if value is None:
        return None
    return Decimal(str(round(float(value), 4)))


def _to_float(value: object) -> float | None:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _build_item(reading: CurrentReading, weather: Weather | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "city": CITY_SLUG,
        "observed_at": reading.observed_at.isoformat(),
        "aqi": _to_decimal(reading.aqi),
        "category": reading.category,
    }
    # A stale reading is not worth storing: it would sit in the training window
    # as an outlier dated to whenever the station last reported.
    if reading.is_stale:
        raise StaleReadingError("Refusing to persist a reading that is not current.")
    optional: dict[str, Any] = {
        "source": reading.source,
        "dominant_pollutant": reading.dominant_pollutant,
        "latitude": _to_decimal(reading.latitude),
        "longitude": _to_decimal(reading.longitude),
        "pm25": _to_decimal(reading.concentrations.pm25),
        "pm10": _to_decimal(reading.concentrations.pm10),
        "o3": _to_decimal(reading.concentrations.o3),
        "no2": _to_decimal(reading.concentrations.no2),
        "so2": _to_decimal(reading.concentrations.so2),
        "co": _to_decimal(reading.concentrations.co),
        "nh3": _to_decimal(reading.concentrations.nh3),
        # Kept for provenance: which station, if any, corroborated this hour.
        "waqi_station_name": reading.waqi.station_name if reading.waqi else None,
    }
    if weather is not None:
        optional.update(
            {
                "weather_source": weather.source,
                "temperature_c": _to_decimal(weather.temperature_c),
                "humidity_pct": _to_decimal(weather.humidity_pct),
                "pressure_hpa": _to_decimal(weather.pressure_hpa),
                "wind_speed_ms": _to_decimal(weather.wind_speed_ms),
                "wind_direction_deg": _to_decimal(weather.wind_direction_deg),
            }
        )
    item.update({key: value for key, value in optional.items() if value is not None})
    return item


def _query_readings(limit: int, since: datetime | None) -> list[dict[str, Any]]:
    condition = Key("city").eq(CITY_SLUG)
    if since is not None:
        condition = condition & Key("observed_at").gte(since.isoformat())

    # Reserved-word safe: every projected attribute goes through a placeholder.
    names = {f"#{name}": name for name in _HISTORY_ATTRIBUTES}
    response = _get_table().query(
        KeyConditionExpression=condition,
        ProjectionExpression=", ".join(names.keys()),
        ExpressionAttributeNames=names,
        ScanIndexForward=False,  # newest first
        Limit=limit,
    )
    return response.get("Items", [])


def _item_to_history_point(item: dict[str, Any]) -> HistoryPoint | None:
    observed_at = parse_provider_timestamp(item.get("observed_at"))
    aqi = _to_float(item.get("aqi"))
    if observed_at is None or aqi is None:
        return None
    return HistoryPoint(
        observed_at=observed_at,
        aqi=round(aqi),
        temperature_c=_to_float(item.get("temperature_c")),
        humidity_pct=_to_float(item.get("humidity_pct")),
        wind_speed_ms=_to_float(item.get("wind_speed_ms")),
    )


async def list_readings(limit: int, since: datetime | None = None) -> list[HistoryPoint]:
    """Stored readings for the city, newest first. Always bounded by `limit`."""
    try:
        items = await anyio.to_thread.run_sync(lambda: _query_readings(limit, since))
    except (BotoCoreError, ClientError, NoCredentialsError) as exc:
        raise _wrap_boto_error(exc, "query") from exc

    points = [point for point in map(_item_to_history_point, items) if point is not None]
    return points


async def put_reading(reading: CurrentReading, weather: Weather | None) -> None:
    """Persist one snapshot. Overwrites an existing row for the same timestamp."""
    item = _build_item(reading, weather)
    try:
        await anyio.to_thread.run_sync(lambda: _get_table().put_item(Item=item))
    except (BotoCoreError, ClientError, NoCredentialsError) as exc:
        raise _wrap_boto_error(exc, "put") from exc
