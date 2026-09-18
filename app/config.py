"""Single source of truth for environment-derived settings.

Nothing outside this module reads the environment directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

# Lahore. The whole service is scoped to one city for now; when a second city is
# added these become a lookup rather than module constants.
CITY_NAME = "Lahore"
CITY_SLUG = "lahore"
CITY_LATITUDE = 31.5204
CITY_LONGITUDE = 74.3587

# Bounding box used for the map feed: roughly Lahore district plus its ring.
CITY_BOUNDS = (31.30, 74.10, 31.75, 74.65)  # lat_min, lon_min, lat_max, lon_max

# Real Lahore neighbourhood coordinates for the area feed (/areas).
#
# There is no official per-area monitoring network in Lahore, so each point is
# read from Open-Meteo's gridded air-quality model at real district
# coordinates. This is real data for a real place - not invented readings -
# but it is model-derived, and responses say so via `source: "open-meteo-model"`.
NEIGHBORHOODS: tuple[tuple[str, float, float], ...] = (
    ("Gulberg", 31.519, 74.357),
    ("Model Town", 31.487, 74.322),
    ("Johar Town", 31.470, 74.273),
    ("DHA", 31.470, 74.410),
    ("Shahdara", 31.612, 74.310),
    ("Wagah", 31.604, 74.573),
)

# A reading older than this is not "live" any more. WAQI's Pakistan feed is
# years stale, so this is what stops it being presented as current.
READING_STALE_AFTER_HOURS = 6

# Pakistan Standard Time. WAQI returns some timestamps without an offset; they are
# station-local, which for every station in CITY_BOUNDS means this.
CITY_TZ = timezone(timedelta(hours=5))


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _read_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    waqi_token: str
    openweather_api_key: str
    aws_region: str
    dynamo_table_name: str
    persist_readings: bool
    cors_origins: list[str] = field(default_factory=list)

    @property
    def has_waqi(self) -> bool:
        return bool(self.waqi_token)

    @property
    def has_openweather(self) -> bool:
        return bool(self.openweather_api_key)


# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are deliberately absent from
# Settings. boto3 reads them from the process environment itself, and load_dotenv()
# above has already put .env there by the time a client is built - so carrying
# them through an application object would hold credentials in memory for no gain.


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        waqi_token=os.getenv("WAQI_TOKEN", "").strip(),
        openweather_api_key=os.getenv("OPENWEATHER_API_KEY", "").strip(),
        aws_region=os.getenv("AWS_REGION", "ap-south-1").strip(),
        dynamo_table_name=os.getenv(
            "DYNAMO_TABLE_NAME", "city_intelligence_readings"
        ).strip(),
        persist_readings=_read_bool("PERSIST_READINGS", True),
        cors_origins=_read_list("CORS_ORIGINS", ["http://localhost:3000"]),
    )
