"""Assembles the reading history that /history and /forecast both read.

Two sources, in priority order:

1. **Stored** readings in DynamoDB, written by /current.
2. The **Open-Meteo air quality archive**, used to fill whatever the store does
   not cover - which, on a fresh deployment, is everything.

Stored readings win on any hour both sources cover, because those are what this
service actually observed. Every returned point carries its `source`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.errors import AppError
from app.models.schemas import SOURCE_STORED, HistoryPoint
from app.services import air_quality_client, dynamo_client, weather_client
from app.services.timestamps import utc_now

logger = logging.getLogger(__name__)


def _hour_bucket(moment: datetime) -> datetime:
    """Two sources never agree on the minute; they do agree on the hour."""
    return moment.replace(minute=0, second=0, microsecond=0)


def _merge(stored: list[HistoryPoint], archive: list[HistoryPoint]) -> list[HistoryPoint]:
    by_hour: dict[datetime, HistoryPoint] = {
        _hour_bucket(point.observed_at): point for point in archive
    }
    # Stored second, so it overwrites the archive on a shared hour.
    by_hour.update({_hour_bucket(point.observed_at): point for point in stored})
    return sorted(by_hour.values(), key=lambda point: point.observed_at, reverse=True)


def _attach_weather(
    points: list[HistoryPoint],
    conditions: dict[datetime, weather_client.HourlyConditions],
) -> list[HistoryPoint]:
    """Fill missing wind/humidity from the hourly series, keeping what exists."""
    if not conditions:
        return points
    filled: list[HistoryPoint] = []
    for point in points:
        hourly = conditions.get(_hour_bucket(point.observed_at))
        if hourly is None:
            filled.append(point)
            continue
        filled.append(
            point.model_copy(
                update={
                    "wind_speed_ms": point.wind_speed_ms
                    if point.wind_speed_ms is not None
                    else hourly.wind_speed_ms,
                    "humidity_pct": point.humidity_pct
                    if point.humidity_pct is not None
                    else hourly.humidity_pct,
                }
            )
        )
    return filled


async def load_readings(
    limit: int,
    hours: int | None = None,
    include_archive: bool = True,
    include_weather: bool = True,
) -> list[HistoryPoint]:
    """Readings for the city, newest first, bounded by `limit`."""
    window_hours = hours if hours is not None else limit
    since = utc_now() - timedelta(hours=window_hours)

    stored: list[HistoryPoint] = []
    stored_error: AppError | None = None
    try:
        stored = await dynamo_client.list_readings(limit=limit, since=since)
    except AppError as exc:
        # A missing table or absent credentials is a reason to fall back to the
        # archive, not a reason to fail - but it is remembered in case the
        # archive cannot cover for it either.
        stored_error = exc
        logger.info("Stored readings unavailable (%s); relying on the archive", exc.code)

    archive: list[HistoryPoint] = []
    if include_archive and len(stored) < limit:
        try:
            archive = await air_quality_client.fetch_archive_history(window_hours)
        except AppError as exc:
            logger.warning("Archive history unavailable: %s", exc.code)

    # Neither source produced anything and the store is the reason: surface that
    # rather than an empty list, because an empty list looks like "no readings
    # yet" when the real problem is configuration the operator can fix.
    if not stored and not archive and stored_error is not None:
        raise stored_error

    merged = _merge(stored, archive)[:limit]

    if include_weather and merged:
        conditions = await weather_client.fetch_hourly_conditions_or_empty(
            past_hours=window_hours + 1
        )
        merged = _attach_weather(merged, conditions)

    return merged


def count_by_source(points: list[HistoryPoint]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for point in points:
        tally[point.source] = tally.get(point.source, 0) + 1
    return tally


__all__ = ["SOURCE_STORED", "count_by_source", "load_readings"]
