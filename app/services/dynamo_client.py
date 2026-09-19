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

from app.config import CITY_NAME, CITY_SLUG, get_settings
from app.errors import ConfigurationError, StaleReadingError, UpstreamError
from app.models.schemas import (
    AccuracySnapshot,
    CurrentReading,
    ForecastResponse,
    HistoryPoint,
    StoredForecastPoint,
    Weather,
)
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


# --- Forecast records and accuracy snapshots ---------------------------------
#
# Both live in the readings table, each under its own partition key value.
#
# The isolation is the point. `list_readings` queries `city = CITY_SLUG` with a
# `Limit`, so a forecast or accuracy row sharing that partition would be counted
# against that limit and then dropped for having no `aqi` - silently thinning
# the model training window with no error raised anywhere. A distinct partition
# value can never be returned by that query.
#
# The table sort key attribute is named `observed_at` and cannot be renamed, so
# each partition stores a different kind of timestamp in it. The meaningful
# fields travel as ordinary attributes alongside.

_FORECAST_PARTITION = f"forecast#{CITY_SLUG}"
_ACCURACY_PARTITION = f"accuracy#{CITY_SLUG}"

# Separator inside the forecast sort key, and a sentinel that sorts above
# anything which can follow it - so a range over target hours stays inclusive.
_KEY_SEPARATOR = "#"
_SORT_KEY_MAX = "\uffff"

_FORECAST_ATTRIBUTES = (
    "observed_at",
    "predicted_for",
    "generated_at",
    "hours_ahead",
    "predicted_aqi",
    "predicted_category",
    "model",
    "weather_basis",
)

_ACCURACY_ATTRIBUTES = (
    "observed_at",
    "basis",
    "model",
    "horizon_hours",
    "training_samples",
    "scored_points",
    "mean_absolute_error",
    "root_mean_square_error",
    "band_accuracy_pct",
)


def _hour_bucket(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _forecast_sort_key(predicted_for: datetime, generated_at: datetime) -> str:
    """Target hour first, so a range over target hours stays a key query.

    `generated_at` is bucketed to the hour: a forecast refreshed several times
    within one hour overwrites its own row rather than accumulating a near
    duplicate per call, while a genuinely later run is kept as its own record.
    """
    return (
        f"{predicted_for.isoformat()}"
        f"{_KEY_SEPARATOR}"
        f"{_hour_bucket(generated_at).isoformat()}"
    )


def _query_partition(
    partition: str, attributes: tuple[str, ...], condition: Any, limit: int
) -> list[dict[str, Any]]:
    """One projected, bounded, newest-first key query. Reserved-word safe."""
    names = {f"#{name}": name for name in attributes}
    response = _get_table().query(
        KeyConditionExpression=Key("city").eq(partition) & condition,
        ProjectionExpression=", ".join(names.keys()),
        ExpressionAttributeNames=names,
        ScanIndexForward=False,  # newest first
        Limit=limit,
    )
    return response.get("Items", [])


def _write_forecast_points(forecast: ForecastResponse) -> None:
    table = _get_table()
    with table.batch_writer() as batch:
        for offset, point in enumerate(forecast.points, start=1):
            batch.put_item(
                Item={
                    "city": _FORECAST_PARTITION,
                    "observed_at": _forecast_sort_key(
                        point.predicted_for, forecast.generated_at
                    ),
                    "predicted_for": point.predicted_for.isoformat(),
                    "generated_at": forecast.generated_at.isoformat(),
                    "hours_ahead": offset,
                    "predicted_aqi": _to_decimal(point.aqi),
                    "predicted_category": point.category,
                    "model": forecast.model,
                    "weather_basis": forecast.weather_basis,
                }
            )


async def put_forecast_points(forecast: ForecastResponse) -> None:
    """Record what was predicted, as it was predicted.

    Without this there is no way to score a forecast that was actually served -
    only to re-run the model over history and score that instead.
    """
    if not forecast.points:
        return
    try:
        await anyio.to_thread.run_sync(lambda: _write_forecast_points(forecast))
    except (BotoCoreError, ClientError, NoCredentialsError) as exc:
        raise _wrap_boto_error(exc, "forecast put") from exc


def _item_to_forecast_point(item: dict[str, Any]) -> StoredForecastPoint | None:
    predicted_for = parse_provider_timestamp(item.get("predicted_for"))
    generated_at = parse_provider_timestamp(item.get("generated_at"))
    predicted_aqi = _to_float(item.get("predicted_aqi"))
    if predicted_for is None or generated_at is None or predicted_aqi is None:
        return None
    hours_ahead = _to_float(item.get("hours_ahead"))
    return StoredForecastPoint(
        predicted_for=predicted_for,
        generated_at=generated_at,
        hours_ahead=int(hours_ahead) if hours_ahead is not None else 0,
        predicted_aqi=round(predicted_aqi),
        predicted_category=str(item.get("predicted_category") or ""),
        model=str(item.get("model") or ""),
        weather_basis=str(item.get("weather_basis") or ""),
    )


async def list_forecast_points(
    since: datetime, until: datetime, limit: int
) -> list[StoredForecastPoint]:
    """Recorded predictions whose target hour falls within [since, until]."""
    condition = Key("observed_at").between(
        since.isoformat(),
        f"{until.isoformat()}{_KEY_SEPARATOR}{_SORT_KEY_MAX}",
    )
    try:
        items = await anyio.to_thread.run_sync(
            lambda: _query_partition(
                _FORECAST_PARTITION, _FORECAST_ATTRIBUTES, condition, limit
            )
        )
    except (BotoCoreError, ClientError, NoCredentialsError) as exc:
        raise _wrap_boto_error(exc, "forecast query") from exc

    return [point for point in map(_item_to_forecast_point, items) if point is not None]


async def put_accuracy_snapshot(snapshot: AccuracySnapshot) -> None:
    """Store one dated accuracy measurement. Overwrites the same timestamp."""
    item = {
        "city": _ACCURACY_PARTITION,
        "observed_at": snapshot.recorded_at.isoformat(),
        "basis": snapshot.basis,
        "model": snapshot.model,
        "horizon_hours": snapshot.horizon_hours,
        "training_samples": snapshot.training_samples,
        "scored_points": snapshot.scored_points,
        "mean_absolute_error": _to_decimal(snapshot.mean_absolute_error),
        "root_mean_square_error": _to_decimal(snapshot.root_mean_square_error),
        "band_accuracy_pct": _to_decimal(snapshot.band_accuracy_pct),
    }
    try:
        await anyio.to_thread.run_sync(lambda: _get_table().put_item(Item=item))
    except (BotoCoreError, ClientError, NoCredentialsError) as exc:
        raise _wrap_boto_error(exc, "accuracy put") from exc


def _item_to_accuracy_snapshot(item: dict[str, Any]) -> AccuracySnapshot | None:
    recorded_at = parse_provider_timestamp(item.get("observed_at"))
    mean_absolute_error = _to_float(item.get("mean_absolute_error"))
    root_mean_square_error = _to_float(item.get("root_mean_square_error"))
    band_accuracy_pct = _to_float(item.get("band_accuracy_pct"))
    if (
        recorded_at is None
        or mean_absolute_error is None
        or root_mean_square_error is None
        or band_accuracy_pct is None
    ):
        return None
    horizon_hours = _to_float(item.get("horizon_hours")) or 0.0
    training_samples = _to_float(item.get("training_samples")) or 0.0
    scored_points = _to_float(item.get("scored_points")) or 0.0
    return AccuracySnapshot(
        recorded_at=recorded_at,
        basis=str(item.get("basis") or ""),
        city=CITY_NAME,
        model=str(item.get("model") or ""),
        horizon_hours=int(horizon_hours),
        training_samples=int(training_samples),
        scored_points=int(scored_points),
        mean_absolute_error=mean_absolute_error,
        root_mean_square_error=root_mean_square_error,
        band_accuracy_pct=band_accuracy_pct,
    )


async def list_accuracy_snapshots(
    limit: int, since: datetime | None = None
) -> list[AccuracySnapshot]:
    """Stored accuracy measurements, newest first. Always bounded by `limit`."""
    condition = (
        Key("observed_at").gte(since.isoformat())
        if since is not None
        else Key("observed_at").gt("")
    )
    try:
        items = await anyio.to_thread.run_sync(
            lambda: _query_partition(
                _ACCURACY_PARTITION, _ACCURACY_ATTRIBUTES, condition, limit
            )
        )
    except (BotoCoreError, ClientError, NoCredentialsError) as exc:
        raise _wrap_boto_error(exc, "accuracy query") from exc

    return [
        snapshot
        for snapshot in map(_item_to_accuracy_snapshot, items)
        if snapshot is not None
    ]
