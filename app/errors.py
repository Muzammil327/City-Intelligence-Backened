"""Typed application errors and the single error response shape.

Services raise these; `main` maps them to responses. Nothing upstream — no
provider message, no stack trace, no SQL — reaches the client.
"""

from __future__ import annotations

from http import HTTPStatus


class AppError(Exception):
    """Base for every error this service turns into a response."""

    code = "internal_error"
    status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    message = "Something went wrong."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message


class ConfigurationError(AppError):
    """A required credential or setting is missing."""

    code = "not_configured"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    message = "This endpoint is not configured on the server."


class UpstreamError(AppError):
    """A third-party provider failed, timed out, or returned an unusable body."""

    code = "upstream_unavailable"
    status_code = HTTPStatus.BAD_GATEWAY
    message = "An upstream data provider is unavailable."


class NotFoundError(AppError):
    code = "not_found"
    status_code = HTTPStatus.NOT_FOUND
    message = "The requested resource was not found."


class InsufficientDataError(AppError):
    """Not enough stored history to do the thing that was asked."""

    code = "insufficient_data"
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    message = "Not enough stored history to produce a result."


class StaleReadingError(AppError):
    """A reading is too old to be treated as current."""

    code = "stale_reading"
    status_code = HTTPStatus.CONFLICT
    message = "The reading is not current enough to store."
