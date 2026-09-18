"""One shared httpx client per event loop.

A new AsyncClient per request would open a new connection pool per request, so
the client is cached. It is keyed on the running event loop because a pooled
connection belongs to the loop that opened it: reusing one across loops raises
"Event loop is closed" on the first request through a stale connection. Under
uvicorn there is only ever one loop; the guard matters for scripts and tests
that call `asyncio.run` more than once.
"""

from __future__ import annotations

import asyncio

import httpx

_TIMEOUT_SECONDS = 10.0

_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def get_client() -> httpx.AsyncClient:
    global _client, _client_loop

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS,
            headers={"User-Agent": "city-intelligence/0.1"},
        )
        _client_loop = loop
    return _client


async def close_client() -> None:
    global _client, _client_loop
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
    _client_loop = None
