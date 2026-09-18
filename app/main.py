"""FastAPI application: wiring, error mapping, and route registration."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import CITY_NAME, get_settings
from app.errors import AppError
from app.models.schemas import ErrorBody, ErrorResponse
from app.rate_limit import RateLimitMiddleware
from app.routes import accuracy, areas, current, forecast, history, stations
from app.services.http_client import close_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# httpx logs every request at INFO including the full URL — which for these
# providers means the API key lands in stdout, and in whatever collects it.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    await close_client()


app = FastAPI(
    title="City Intelligence API",
    description=f"Air quality and weather intelligence for {CITY_NAME}.",
    version="0.1.0",
    lifespan=lifespan,
)

# Added before CORS so it runs after it: a rejected request still carries the
# headers the browser needs to read the 429 rather than reporting a network
# error.
app.add_middleware(RateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump(by_alias=True))


@app.exception_handler(AppError)
async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    return _error_response(int(exc.status_code), exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "query")
    detail = first.get("msg", "Invalid request.")
    message = f"{location}: {detail}" if location else detail
    return _error_response(422, "invalid_request", message)


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Log the cause; return nothing about it.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _error_response(500, "internal_error", "Something went wrong.")


@app.get("/health", tags=["meta"], summary="Liveness and configuration check")
async def get_health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "city": CITY_NAME,
        "configured": {
            "waqi": settings.has_waqi,
            "openweather": settings.has_openweather,
            "dynamoTable": settings.dynamo_table_name,
        },
    }


app.include_router(areas.router)
app.include_router(current.router)
app.include_router(history.router)
app.include_router(forecast.router)
app.include_router(accuracy.router)
app.include_router(stations.router)
