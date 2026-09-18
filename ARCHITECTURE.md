# Backend architecture

FastAPI service for **Lahore** air quality. It reads open city data from four
providers, stores hourly readings, forecasts the next 6–24 hours with a Ridge
model, and measures how wrong that forecast usually is.

Six GET endpoints, no writes exposed. The frontend in `../frontend` is the only
client.

---

## Request flow

```
                        BROWSER  (localhost:3000 / :3001)
                                │  axios, one shared client
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  main.py — FastAPI app                                               │
│                                                                      │
│   RateLimitMiddleware ── 60/min · 20/min on /areas + /forecast       │
│   CORSMiddleware ─────── allow_origins from CORS_ORIGINS             │
│   exception handlers ─── AppError · ValidationError · Exception      │
│                          → {error:{code,message}}  never a trace     │
└──────────────────────────────────────────────────────────────────────┘
                                │
    ┌───────────┬───────────┬───┴────────┬─────────────┬────────────┐
    ▼           ▼           ▼            ▼             ▼            ▼
/current    /history    /forecast   /forecast/     /areas      /stations
                                     accuracy
    │           │           │            │             │            │
 ┌──┴───────────┴───────────┴────────────┴─────────────┴────────────┴──┐
 │  services/cache.py — TTL + per-key lock (no thundering herd)        │
 │  5min        10min       30min        30min        10min      10min │
 └──┬───────────┬───────────┬────────────┬─────────────┬────────────┬──┘
    ▼           ▼           ▼            ▼             ▼            ▼
┌───────────┐ ┌────────┐ ┌─────────┐ ┌──────────────┐ ┌───────┐ ┌──────────┐
│air_quality│ │readings│ │predictor│ │ verification │ │ areas │ │   waqi   │
│  weather  │ │        │ │  Ridge  │ │   hindcast   │ │  ×6   │ │  client  │
│air_pollut.│ │        │ │+Scaler  │ │ MAE·RMSE·band│ │ points│ │          │
│   waqi    │ └───┬────┘ └────┬────┘ └──────┬───────┘ └───┬───┘ └────┬─────┘
│  dynamo   │     │           │             │             │          │
└─────┬─────┘     │           └──── fit_and_predict() ────┘          │
      │           └────── load_readings() ─┘                         │
      │                                                              │
┌─────┴──────────────── shared ───────────────────────────────┐      │
│  http_client (one pooled httpx)  ·  timestamps  ·  errors   │      │
│  models/aqi.py — EPA PM2.5/PM10 → AQI, one breakpoint table │      │
│  models/schemas.py — Pydantic, to_camel alias ⇄ types.ts    │      │
│  config.py — @lru_cache Settings, NEIGHBORHOODS, CITY_*     │      │
└─────────────────────────────────────────────────────────────┘      │
      │                                                              ▼
      ▼
┌──────────────┐  ┌────────────────┐  ┌──────────────┐  ┌──────────────────┐
│  DynamoDB    │  │   Open-Meteo   │  │OpenWeatherMap│  │       WAQI       │
│ stored hourly│  │ air-quality    │  │ weather      │  │ /feed/lahore     │
│   readings   │  │ forecast       │  │ air_pollution│  │ /map/bounds      │
│              │  │ archive        │  │              │  │ (no live station)│
└──────────────┘  └────────────────┘  └──────────────┘  └──────────────────┘
```

---

## Layers

| Layer | Owns | Rule |
| --- | --- | --- |
| `main.py` | Middleware, error mapping, router registration | No business logic |
| `routes/` | Query validation, cache key, one service call | Thin — validate, delegate, return |
| `services/` | Provider calls, persistence, the model | Where the work happens |
| `models/` | `schemas.py` (wire) and `aqi.py` (EPA conversion) | The two contracts |
| `config.py` | `Settings`, coordinates, thresholds | Nothing reads `os.getenv` elsewhere |
| `errors.py` | `AppError` subclasses, each with a status and a code | One error shape |

---

## Endpoints

| Endpoint | Returns | Services | Cache |
| --- | --- | --- | --- |
| `GET /health` | Liveness + which providers are configured | — | none, never rate limited |
| `GET /current` | Headline AQI, concentrations, weather | `air_quality_client`, `weather_client`, `air_pollution_client`, `waqi_client`, `dynamo_client` | 5 min |
| `GET /history` | Stored readings, newest first | `readings` | 10 min, keyed by query |
| `GET /forecast` | Hourly predicted AQI, 6–24 h | `readings`, `predictor` | 30 min |
| `GET /forecast/accuracy` | Out-of-sample skill of that model | `readings`, `verification` → `predictor` | 30 min |
| `GET /areas` | Six neighbourhood points + city summary | `areas` | 10 min |
| `GET /stations` | Physical monitoring stations | `waqi_client` | 10 min |

---

## The one write path

**`/current` is the only endpoint that stores anything.** It persists each
reading to DynamoDB, and `/history`, `/forecast` and `/forecast/accuracy` all
read from that store afterwards. Persistence failures are logged and swallowed —
they must never take the live endpoint down.

Because `/current` is cached for 5 minutes, the store now fills once per window
rather than once per request.

---

## The model

**`services/predictor.py`** — refit on every call, nothing persisted.

- `Pipeline(StandardScaler → Ridge(alpha=1.0))`
- Features: `trend_hours`, `hour_sin`, `hour_cos`, `wind_speed_ms`, `humidity_pct`
- Time of day is encoded cyclically, so 23:00 is adjacent to 00:00
- Future wind and humidity come from Open-Meteo's hourly forecast; when that
  call fails the last observed values are carried forward, and the response says
  which happened via `weatherBasis`
- Refuses below `MIN_TRAINING_SAMPLES = 12` with `InsufficientDataError`

Deliberately simple: the dataset is small, so interpretability and speed beat
accuracy, and the response reports `weatherBasis` and `r2Score` so the caller
can judge it.

**`services/verification.py`** — how wrong it usually is.

`r2Score` is *in-sample* fit. It says nothing about predicting an unseen hour.
This module answers the other question by hindcasting: hold out the most recent
N hours, refit on everything before them, predict forward, score against what
actually happened.

Two conservative choices:

- **Weather is carried forward, not read from the held-out rows.** The real wind
  and humidity for those hours exist in the data; feeding them in would hand the
  model perfect foresight.
- **Band accuracy counts a hit only in the same EPA category.** Being 15 points
  out inside "Unhealthy" changes no advice; crossing a boundary does.

It calls `predictor.fit_and_predict()` — the same function the live endpoint
uses — so the figure measures what is actually served, not a copy that can drift.

---

## Caching and rate limiting

**`services/cache.py`** — in-process, `dict` + monotonic expiry, one
`asyncio.Lock` per key. The lock matters: a cold `/areas` key hit by several
requests at once would otherwise start several six-call fan-outs.

In-process on purpose — one uvicorn worker, no extra infrastructure, and a
restart is a deliberate way to clear it. A multi-worker deployment would need a
shared store; each worker keeps its own copy here.

**`rate_limit.py`** — per client address, per route, fixed 60-second window.
Hand-rolled rather than `slowapi`: single process, the policy is four numbers,
and a Redis-capable dependency would be cost without benefit. Registered
*before* CORS so it runs *after* it, which means a 429 still carries the headers
the browser needs to read the message instead of reporting a network error.

`RATE_LIMIT_PER_MINUTE=0` disables it — the documented way to switch it off
while developing.

---

## Contracts

**`models/aqi.py`** — the only place a mass concentration becomes an index. The
EPA PM2.5 and PM10 breakpoint tables live here and nowhere else. Moving to a
different national AQI scale means editing this file alone.

**`models/schemas.py`** — every response model, with
`ConfigDict(alias_generator=to_camel)`. Field names are snake_case in Python and
camelCase on the wire, which is why `frontend/lib/aqi/types.ts` mirrors these
shapes field for field and the frontend needs **no mapping layer**.

**`errors.py`** — one envelope, `{error: {code, message}}`, for every failure.
Handlers in `main.py` map `AppError` subclasses to their status codes; the
catch-all logs the cause and returns "Something went wrong." Nothing internal
reaches a client.

---

## Known limitations

These are properties of the data, not bugs. They are written down so nobody
mistakes them for either.

**The six neighbourhood points may return identical readings.** `/areas` reads
Open-Meteo's gridded air-quality model at six real district coordinates. That
model's global resolution is roughly 0.4° (~40 km); Lahore's districts span
about 30 km. All six can therefore fall in one grid cell. `services/areas.py`
correctly issues six separate requests — the resolution is upstream. When this
happens, hotspot ranking and area comparison have nothing real to show.

**WAQI reports no active station in Lahore.** Its Pakistan feed has been stale
since early 2025. `/stations` returns an empty list, which is a valid answer
rather than an error — and it is the reason `/areas` reads a model at all.

**Forecasts are not persisted.** The model refits per call and stores nothing,
so there is no record of "at 14:00 we predicted 156 for 18:00". Accuracy is
therefore measured by hindcast rather than by verifying live predictions after
the fact. Storing forecast points would allow true out-of-sample verification
over time.

**The store starts empty.** `/forecast` needs 12 readings, `/forecast/accuracy`
needs 12 + the scored horizon. Both fail with `insufficient_data` until
`/current` has run enough times to fill it.

---

## Running

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env            # then fill in the values
uvicorn app.main:app --reload --port 8000
```

Interactive docs: <http://localhost:8000/docs>

Settings are `@lru_cache`d and `.env` is read at startup, so **a `.env` change
needs a restart** — `--reload` will not pick it up.
