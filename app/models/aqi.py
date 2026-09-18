"""US EPA AQI: the band labels, and conversion from concentration to index.

This is the single source of truth for what an AQI number means here. Every
provider is normalised onto this 0-500 scale before it reaches a response, so a
value from one source is comparable with a value from another.
"""

from __future__ import annotations

from typing import Final

AQI_MIN: Final = 0
AQI_MAX: Final = 500

# (inclusive upper bound, label). Ordered; the first match wins.
_BANDS: Final[tuple[tuple[int, str], ...]] = (
    (50, "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for Sensitive Groups"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
    (AQI_MAX, "Hazardous"),
)

# (concentration low, concentration high, index low, index high).
# PM2.5 uses the 2024 revised breakpoints; both tables are in ug/m3, which is
# what the providers report, so no unit conversion is involved.
_PM25_BREAKPOINTS: Final[tuple[tuple[float, float, int, int], ...]] = (
    (0.0, 9.0, 0, 50),
    (9.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 125.4, 151, 200),
    (125.5, 225.4, 201, 300),
    (225.5, 325.4, 301, 500),
)

_PM10_BREAKPOINTS: Final[tuple[tuple[float, float, int, int], ...]] = (
    (0.0, 54.0, 0, 50),
    (55.0, 154.0, 51, 100),
    (155.0, 254.0, 101, 150),
    (255.0, 354.0, 151, 200),
    (355.0, 424.0, 201, 300),
    (425.0, 604.0, 301, 500),
)

POLLUTANT_PM25: Final = "pm25"
POLLUTANT_PM10: Final = "pm10"


def category_for_aqi(aqi: float) -> str:
    """Return the EPA category label for an AQI value."""
    for upper_bound, label in _BANDS:
        if aqi <= upper_bound:
            return label
    return _BANDS[-1][1]


def clamp_aqi(aqi: float) -> float:
    """Keep a value inside the scale - a regression can predict outside it."""
    return max(float(AQI_MIN), min(float(AQI_MAX), float(aqi)))


def _index_from_breakpoints(
    concentration: float,
    breakpoints: tuple[tuple[float, float, int, int], ...],
    decimals: int,
) -> int | None:
    # The EPA truncates the concentration before the lookup rather than rounding.
    factor = 10**decimals
    truncated = int(concentration * factor) / factor
    if truncated < breakpoints[0][0]:
        return None
    for low_c, high_c, low_i, high_i in breakpoints:
        if truncated <= high_c:
            span = high_c - low_c
            if span <= 0:
                return low_i
            return round((high_i - low_i) / span * (truncated - low_c) + low_i)
    # Above the top of the table: the scale is capped, not extrapolated.
    return AQI_MAX


def aqi_from_pm25(concentration: float | None) -> int | None:
    """US AQI for a PM2.5 concentration in ug/m3."""
    if concentration is None or concentration < 0:
        return None
    return _index_from_breakpoints(concentration, _PM25_BREAKPOINTS, decimals=1)


def aqi_from_pm10(concentration: float | None) -> int | None:
    """US AQI for a PM10 concentration in ug/m3."""
    if concentration is None or concentration < 0:
        return None
    return _index_from_breakpoints(concentration, _PM10_BREAKPOINTS, decimals=0)


def overall_aqi(pm25: float | None, pm10: float | None) -> tuple[int, str] | None:
    """Overall US AQI from particulates: the worst sub-index wins.

    Gases (O3, NO2, SO2, CO) are deliberately excluded. Their EPA breakpoints are
    defined in ppb/ppm while providers report ug/m3, and converting needs
    temperature and pressure assumptions that would make the number less
    trustworthy than leaving it out.
    """
    candidates = [
        (aqi_from_pm25(pm25), POLLUTANT_PM25),
        (aqi_from_pm10(pm10), POLLUTANT_PM10),
    ]
    scored = [(value, name) for value, name in candidates if value is not None]
    if not scored:
        return None
    return max(scored, key=lambda pair: pair[0])
