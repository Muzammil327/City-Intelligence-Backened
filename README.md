# City Intelligence — Backend

FastAPI service serving air quality and weather for **Lahore**, backed by WAQI,
OpenWeatherMap (with an Open-Meteo fallback), and DynamoDB.

## Running

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env          # then fill in the values
uvicorn app.main:app --reload --port 8000
```

Interactive docs: <http://localhost:8000/docs>

## Endpoints

| Method | Path         | Query                          | Returns |
| ------ | ------------ | ------------------------------ | ------- |
| GET    | `/health`    | —                              | Liveness plus which providers are configured |
| GET    | `/current`   | —                              | Live AQI, pollutants, and weather |
| GET    | `/history`   | `limit` 1–500 (24), `hours` 1–720, `includeArchive` (true) | Readings, newest first |
| GET    | `/forecast`  | `hours` 6–24 (12)              | Hourly predicted AQI |
| GET    | `/stations`  | —                              | Every reporting station in the city bounds |
| GET    | `/areas`     | —                              | Neighbourhood readings, plus a representative city summary |

JSON fields are **camelCase**. Timestamps are ISO-8601 UTC.

Errors always take one shape, and never carry an upstream message or stack trace:

```json
{ "error": { "code": "upstream_unavailable", "message": "..." } }
```

Codes: `not_configured` (503), `upstream_unavailable` (502), `not_found` (404),
`insufficient_data` (422), `invalid_request` (422), `internal_error` (500).

## DynamoDB

**This service never creates or alters the table.** It assumes:

| | | |
| --- | --- | --- |
| Table | `DYNAMO_TABLE_NAME` | default `city_intelligence_readings` |
| Partition key | `city` (S) | the city slug, e.g. `lahore` |
| Sort key | `observed_at` (S) | ISO-8601 UTC |

All other attributes are optional (`aqi`, `category`, `pm25`, `temperature_c`,
`humidity_pct`, `wind_speed_ms`, …), so a row written by an older version still
reads back. Reads are projected to only the attributes the caller needs.

The sort key is not optional for this design. Without it `city` would be the
whole identity and each write would overwrite the last reading, leaving one row
per city forever.

Access needs `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in `.env` (boto3
reads them from the environment directly, not via `app/config.py`), and
`AWS_REGION` must match the region the table was created in — otherwise boto3
looks in the wrong region and reports the table as missing.

**Where history comes from:** `GET /current` write-throughs a snapshot on every
call when `PERSIST_READINGS=true`. A failed write is logged and swallowed — it
must not take the live endpoint down. Calling `/current` on a schedule (cron,
EventBridge, or the frontend's own polling) is what populates `/history` and
therefore `/forecast`.

## Data sources

| Source | Used for | Key |
| --- | --- | --- |
| **Open-Meteo Air Quality** | **Live AQI + concentrations on `/current`** | none |
| WAQI | Supplementary station attribution, station map | `WAQI_TOKEN` |
| OpenWeatherMap Weather | Current conditions on `/current` | `OPENWEATHER_API_KEY` |
| OpenWeatherMap Air Pollution | Concentrations in µg/m³ on `/current` | `OPENWEATHER_API_KEY` |
| Open-Meteo Air Quality | Historical AQI backfill for `/history` and training | none |
| Open-Meteo Forecast | Hourly wind/humidity, past and future | none |

### Two AQI scales, never mixed

WAQI reports on the **US AQI 0–500** scale. OpenWeatherMap's Air Pollution API
reports an index of **1–5**, which is a different scale entirely — a 3 there is
not a 3 anywhere else. They are never written into the same field:

- `aqi` is always US AQI 0–500.
- `airPollution.owmIndex` is OWM's 1–5 index, under its own name.
- `airPollution.usAqi` is a US AQI derived from OWM's **concentrations** via the
  EPA breakpoints in `models/aqi.py`, so it is comparable with `aqi`.

Only PM2.5 and PM10 feed that conversion. The EPA's gas breakpoints are defined
in ppb/ppm while providers report µg/m³, and converting needs temperature and
pressure assumptions that would make the number less trustworthy than omitting
it.

### `dominantPollutant` is null unless it is known

Open-Meteo's `us_aqi` is the **full** EPA index — it folds in ozone, NO₂, SO₂
and CO over their own averaging windows. We only compute particulate
sub-indices, so we cannot say which pollutant drove it. The field is therefore
**null whenever the headline index came from the provider**, and populated only
when this service derived the index itself from particulates.

This is not hypothetical: on a live reading the provider's index was **207**
while particulates alone gave **166**. Those 41 points came from gases, so
reporting `pm25` would have named the wrong pollutant.

`airPollution.dominantPollutant` and `waqi.dominantPollutant` are still
populated — the first because its `usAqi` is particulate-derived by
construction, the second because WAQI names the pollutant itself.

## History: two sources, marked

`/history` merges stored DynamoDB readings with the Open-Meteo air quality
archive, and every point carries a `source` of `stored` or `open-meteo-archive`.
Stored readings win on any hour both cover. The response includes a `sources`
tally.

This means `/history` and `/forecast` work on a fresh deployment with no stored
data and no AWS account at all. Pass `includeArchive=false` to see only what this
service actually recorded — with no store configured, that correctly returns a
`not_configured` error rather than a misleading empty list.

## Areas: neighbourhood readings and the city summary

`/areas` reads Open-Meteo's gridded air-quality model at the real coordinates of
six Lahore neighbourhoods (`NEIGHBORHOODS` in `app/config.py`) plus their
current weather, and returns both the per-area list and an overall summary.

**No per-area AQI is fabricated, and none is claimed to come from a sensor.**
There is no official Lahore monitoring network (WAQI reports no stations for the
city), so every area carries `source: "open-meteo-model"` and
`basis: "gridded-model"` to make clear these are model-derived grid points, not
physical stations. The model's grid cells are ~9 km, so neighbouring areas can
report the same value — that is the real resolution of the data, not a bug.

**Overall summary — the documented calculation.** Averaging the PM2.5 and PM10
concentrations across every area that reported them, converting each mean to its
US EPA sub-index (EPA 2024 PM2.5 breakpoints, `models/aqi.py`), and taking the
worse sub-index as the overall AQI. `overall.pm25` is the mean concentration that
drove it. The result is one representative city number, never a point
measurement. Gases are excluded for the same reason as everywhere else: their
EPA breakpoints are defined in ppb/ppm while providers report µg/m³.

## Why Open-Meteo is the live source, not WAQI

Ridge regression, refit from stored history on every `/forecast` call, nothing
persisted to disk. Features: linear trend, cyclical hour-of-day (sin/cos), wind
speed, humidity. Needs at least 12 stored readings or it returns
`insufficient_data` rather than a fabricated line.

Future wind and humidity come from Open-Meteo's hourly **forecast**, so the model
projects onto real predicted weather. If that call fails, the last observed
values are carried forward instead and `weatherBasis` reports `persisted` rather
than `forecast` — the endpoint degrades instead of failing.

Two honest limits:

- `r2Score` is **in-sample fit**, not forecast accuracy. Do not show it to a user
  as a confidence score.
- A linear model on ~week-long history captures daily cycle and trend, not
  weather fronts or burning events. Treat the horizon as indicative.

## Why Open-Meteo is the live source, not WAQI

**WAQI has no live Pakistan coverage at all**, verified against the API directly:

- `search/?keyword=pakistan` returns exactly **four** stations — Islamabad,
  Karachi, Peshawar and Lahore, all US diplomatic posts — and **every one
  reports `aqi = "-"`**, meaning no current data. There is no Punjab EPA network
  in WAQI's index.
- `map/bounds/` returns **zero stations** for Lahore and for all of Punjab. The
  same call with WAQI's own Beijing example box returns 31, so the token and the
  endpoint both work; the data is not there. `/stations` returns an honest
  `404 not_found`.
- `feed/lahore` serves a cached "Lahore US Embassy" reading stamped
  **2025-02-18** — roughly 13,800 hours stale.
- `feed/geo:31.5204;74.3587` resolves to a station in **Delhi, India**, ~400 km
  away. Querying by coordinates does not help.

Open-Meteo reports `us_aqi` on the same 0-500 scale, needs no key, and is
current. It is therefore the headline source for `/current`, `/history` and the
forecast training set.

### What `/current` returns now

| Field | Source |
| --- | --- |
| `aqi`, `category`, `concentrations`, `observedAt` | Open-Meteo Air Quality |
| `source` | which provider supplied the headline number |
| `ageHours`, `isStale` | freshness of the headline reading |
| `weather` | OpenWeatherMap, falling back to Open-Meteo |
| `airPollution` | OpenWeatherMap Air Pollution, as a cross-check |
| `waqi` | WAQI's station reading, with its own `ageHours` / `isStale` |

**Nothing presents a stale number as live.** If Open-Meteo is unavailable,
`/current` falls back to WAQI rather than failing — but sets `source: "waqi"`
and `isStale: true`, and leaves `concentrations` empty, because WAQI reports
sub-indices rather than mass concentrations and filling that object would put
numbers on the wrong scale.

Readings flagged `isStale` are **not persisted**: a reading dated to whenever a
dead station last reported would sit in the training window as an outlier.
