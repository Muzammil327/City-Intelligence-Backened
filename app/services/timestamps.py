"""Timestamp parsing for third-party payloads.

Providers disagree about offsets: some send ISO-8601 with one, some send a naive
local time. Everything this service stores or returns is timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.config import CITY_TZ


def parse_provider_timestamp(value: object) -> datetime | None:
    """Parse a provider timestamp into an aware UTC datetime, or None."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A naive provider timestamp is station-local, and every station we read is
    # inside the city bounds.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CITY_TZ)
    return parsed.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)
