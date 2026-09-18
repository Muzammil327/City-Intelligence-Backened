"""Per-client request limiting.

Hand-rolled rather than pulled from a library: this runs as a single process,
the policy is four numbers, and a dependency that brings a Redis backend along
for a local service is cost without benefit.

The window is fixed, not sliding — a client gets `limit` requests per window and
the counter resets when the window rolls. That is coarser than a sliding window
but it is also obvious, which matters more here than precision.

Limits are per client address and per route, so a burst on `/areas` never
exhausts a visitor's budget for `/current`.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from app.config import get_settings
from app.models.schemas import ErrorBody, ErrorResponse

# Expensive routes get a tighter budget: one `/areas` call is six upstream
# requests, and one `/forecast` refits the model.
ROUTE_LIMITS: dict[str, int] = {
    "/areas": 20,
    "/forecast": 20,
}


@dataclass
class _Window:
    started_at: float
    count: int


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Reject a client that exceeds its per-route budget for the window."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._windows: dict[tuple[str, str], _Window] = defaultdict(
            lambda: _Window(started_at=0.0, count=0)
        )

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> JSONResponse:
        settings = get_settings()

        # An unlimited setting is the documented way to switch this off while
        # developing, rather than commenting the middleware out.
        if settings.rate_limit_per_minute <= 0:
            return await call_next(request)

        path = request.url.path

        # Liveness must never be rate limited — it is what a probe calls.
        if path == "/health":
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        limit = ROUTE_LIMITS.get(path, settings.rate_limit_per_minute)

        now = time.monotonic()
        window = self._windows[(client, path)]
        if now - window.started_at >= 60.0:
            window.started_at = now
            window.count = 0

        window.count += 1
        if window.count > limit:
            retry_after = max(1, int(60.0 - (now - window.started_at)))
            body = ErrorResponse(
                error=ErrorBody(
                    code="rate_limited",
                    message=(
                        "Too many requests. Please wait a moment and try again."
                    ),
                )
            )
            return JSONResponse(
                status_code=429,
                content=body.model_dump(by_alias=True),
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
