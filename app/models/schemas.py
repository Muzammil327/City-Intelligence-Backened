"""Response models. Every route returns one of these — nothing hand-shaped.

Field names are camelCase on the wire (the frontend is TypeScript) and
snake_case in Python; the alias generator maps between them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


# Provenance markers for a stored or reconstructed reading.
SOURCE_STORED = "stored"
SOURCE_ARCHIVE = "open-meteo-archive"

# Providers that can supply the headline AQI on /current.
SOURCE_OPEN_METEO_AQ = "open-meteo-air-quality"
SOURCE_WAQI = "waqi"

# How the predictor obtained the weather it projected onto.
WEATHER_BASIS_FORECAST = "forecast"
WEATHER_BASIS_PERSISTED = "persisted"


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Pollutants(ApiModel):
    """Individual pollutant readings. Any station may report only a subset."""

    pm25: float | None = None
    pm10: float | None = None
    o3: float | None = None
    no2: float | None = None
    so2: float | None = None
    co: float | None = None


class Weather(ApiModel):
    source: str = Field(description="Which provider supplied this reading.")
    temperature_c: float | None = None
    feels_like_c: float | None = None
    humidity_pct: float | None = None
    pressure_hpa: float | None = None
    wind_speed_ms: float | None = None
    wind_direction_deg: float | None = None
    conditions: str | None = None


class Concentrations(ApiModel):
    """Measured mass concentrations in ug/m3 - not index values."""

    pm25: float | None = None
    pm10: float | None = None
    o3: float | None = None
    no2: float | None = None
    so2: float | None = None
    co: float | None = None
    nh3: float | None = None


class AirPollution(ApiModel):
    """OpenWeatherMap's air pollution reading, kept distinct from WAQI's index."""

    source: str
    measured_at: datetime
    concentrations: Concentrations
    owm_index: int | None = Field(
        default=None,
        description="OpenWeatherMap's own 1-5 index. NOT the US AQI scale.",
    )
    us_aqi: int | None = Field(
        default=None,
        description="US AQI derived from the particulate concentrations above.",
    )
    us_aqi_category: str | None = None
    dominant_pollutant: str | None = None


class WaqiReading(ApiModel):
    """WAQI's station reading, kept as supplementary attribution.

    Carries its own age because WAQI's Pakistan stations stopped reporting in
    early 2025 and it must never be mistaken for a live number.
    """

    aqi: int
    category: str
    observed_at: datetime
    age_hours: float
    is_stale: bool
    station_name: str | None = None
    dominant_pollutant: str | None = None
    pollutants: Pollutants = Field(
        description="WAQI sub-indices on the AQI scale - not concentrations."
    )


class CurrentReading(ApiModel):
    city: str
    source: str = Field(description="Which provider supplied the headline AQI.")
    aqi: int
    category: str
    dominant_pollutant: str | None = Field(
        default=None,
        description=(
            "The pollutant driving `aqi`, or null when it cannot be attributed. "
            "Null whenever the headline index came from a provider that folds in "
            "gases we do not compute sub-indices for - naming a particulate there "
            "would be a guess."
        ),
    )
    observed_at: datetime
    age_hours: float
    is_stale: bool
    latitude: float
    longitude: float
    concentrations: Concentrations = Field(
        description="Mass concentrations in ug/m3 from the headline source."
    )
    weather: Weather | None = None
    air_pollution: AirPollution | None = None
    waqi: WaqiReading | None = None


class HistoryPoint(ApiModel):
    observed_at: datetime
    aqi: int
    source: str = Field(
        default=SOURCE_STORED,
        description="Where this point came from: stored readings, or the archive.",
    )
    temperature_c: float | None = None
    humidity_pct: float | None = None
    wind_speed_ms: float | None = None


class HistoryResponse(ApiModel):
    city: str
    count: int
    sources: dict[str, int] = Field(
        default_factory=dict,
        description="How many returned points came from each source.",
    )
    readings: list[HistoryPoint]


class ForecastPoint(ApiModel):
    predicted_for: datetime
    aqi: int
    category: str


class ForecastResponse(ApiModel):
    city: str
    generated_at: datetime
    horizon_hours: int
    model: str = Field(description="Estimator used for this forecast.")
    training_samples: int
    weather_basis: str = Field(
        description=(
            "'forecast' when future wind/humidity came from Open-Meteo, "
            "'persisted' when they were carried forward from the last reading."
        )
    )
    r2_score: float | None = Field(
        default=None,
        description="In-sample fit quality. Not a measure of forecast accuracy.",
    )
    points: list[ForecastPoint]


class ForecastAccuracyPoint(ApiModel):
    """One predicted hour set against the observation that actually followed."""

    predicted_for: datetime
    hours_ahead: int = Field(description="How far past the cut this hour sat.")
    predicted_aqi: int
    observed_aqi: int
    error: int = Field(description="predicted - observed. Positive means over.")
    predicted_category: str
    observed_category: str


class ForecastAccuracy(ApiModel):
    """Hindcast skill: the model refit without the most recent hours, then
    scored against them.

    This is out-of-sample, unlike `ForecastResponse.r2_score`. Weather over the
    scored window is carried forward rather than taken from the observations,
    so the figures do not assume perfect knowledge of the future.
    """

    city: str
    model: str
    horizon_hours: int = Field(description="Hours actually scored, not requested.")
    training_samples: int
    evaluated_from: datetime
    mean_absolute_error: float = Field(description="Average miss, in AQI points.")
    root_mean_square_error: float
    band_accuracy_pct: float = Field(
        description="Share of hours landing in the correct EPA category."
    )
    points: list[ForecastAccuracyPoint]


# How an accuracy figure was arrived at.
ACCURACY_BASIS_HINDCAST = "hindcast"
ACCURACY_BASIS_VERIFIED = "verified"


class StoredForecastPoint(ApiModel):
    """One prediction, recorded at the moment it was made.

    Kept so the service can answer the one question a hindcast cannot: how
    close was the forecast actually served? `generated_at` is when the
    prediction was made, `predicted_for` is the hour it describes, and the gap
    between them is the lead time the number should be judged on.
    """

    predicted_for: datetime
    generated_at: datetime
    hours_ahead: int
    predicted_aqi: int
    predicted_category: str
    model: str
    weather_basis: str


class AccuracySnapshot(ApiModel):
    """One accuracy measurement, dated, so the figure can be trended.

    `basis` says which question it answers:

    * `hindcast` - the model refit without the most recent hours and scored
      against them. Available immediately and backfillable over stored history,
      but it measures a re-run rather than a forecast anyone was shown.
    * `verified` - predictions this service actually published, scored against
      the observations that later arrived. The honest number, and the one that
      only accrues forward from the day recording started.

    Both are reported separately and never averaged together.
    """

    recorded_at: datetime
    basis: str = Field(description="'hindcast' or 'verified'.")
    city: str
    model: str
    horizon_hours: int
    training_samples: int
    scored_points: int = Field(
        description="Predicted hours that had an observation to score against."
    )
    mean_absolute_error: float = Field(description="Average miss, in AQI points.")
    root_mean_square_error: float
    band_accuracy_pct: float = Field(
        description="Share of hours landing in the correct EPA category."
    )


class AccuracyHistoryResponse(ApiModel):
    """How the model's skill has moved over time, newest first."""

    city: str
    count: int
    bases: dict[str, int] = Field(
        default_factory=dict,
        description="How many returned snapshots came from each basis.",
    )
    snapshots: list[AccuracySnapshot]


class Station(ApiModel):
    uid: str
    name: str
    latitude: float
    longitude: float
    aqi: int | None = None
    category: str | None = None
    observed_at: datetime | None = None


class StationsResponse(ApiModel):
    city: str
    count: int
    stations: list[Station]


class AreaReading(ApiModel):
    """One neighbourhood's current air quality.

    These are model-derived grid points from Open-Meteo - real readings for
    real places, but not physical monitoring stations. `source` says so, and
    `basis` distinguishes the two.
    """

    uid: str
    name: str
    latitude: float
    longitude: float
    source: str = Field(
        description="'open-meteo-model' — derived from a gridded model, not a station."
    )
    basis: str = Field(
        default="gridded-model",
        description="'gridded-model' when model-derived, 'station' for a physical sensor.",
    )
    weather_source: str | None = Field(
        default=None,
        description="Which provider supplied the weather block, if any.",
    )
    observed_at: datetime
    age_hours: float
    is_stale: bool
    aqi: int
    pm25: float | None = None
    pm10: float | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None
    wind_speed_ms: float | None = None
    wind_direction_deg: float | None = None


class OverallSummary(ApiModel):
    """One representative number for the city, from the area points.

    The calculation is documented in `services/areas.py` and the backend README;
    it is a deliberate, named method (mean pollutant concentrations across the
    areas, then the worst EPA sub-index), not an off-the-cuff average.
    """

    aqi: int
    category: str
    pm25: float | None
    areas_with_data: int
    area_count: int
    highest_name: str | None
    highest_aqi: int | None
    lowest_name: str | None
    lowest_aqi: int | None
    observed_at: datetime | None


class AreasResponse(ApiModel):
    city: str
    count: int
    overall: OverallSummary
    areas: list[AreaReading]


class ErrorBody(ApiModel):
    code: str
    message: str


class ErrorResponse(ApiModel):
    error: ErrorBody
