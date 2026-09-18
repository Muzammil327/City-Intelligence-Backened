"""A small in-process response cache.

Every endpoint here fans out to upstream providers — `/areas` makes one
Open-Meteo call per neighbourhood, `/forecast` refits its model from scratch.
Those providers publish hourly, so serving two identical requests a few seconds
apart by calling them twice spends quota and latency for a byte-identical
answer.

In-process on purpose: one uvicorn worker, no extra infrastructure, and a
restart is a deliberate way to clear it. A multi-worker deployment would want
this in a shared store instead — each worker keeps its own copy here.

Concurrency note: entries are only ever whole values replacing whole values, and
the lock covers the fetch so a cold key does not start N identical upstream
calls when N requests arrive together (the "thundering herd" this is meant to
prevent in the first place).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class _Entry:
    value: Any
    expires_at: float


_entries: dict[str, _Entry] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def cached(key: str, ttl_seconds: float, load: Callable[[], Awaitable[T]]) -> T:
    """Return the cached value for `key`, calling `load()` only when it is cold.

    `load` is awaited under a per-key lock, so concurrent callers for the same
    key wait for one upstream round trip rather than starting their own.
    """
    now = time.monotonic()
    entry = _entries.get(key)
    if entry is not None and entry.expires_at > now:
        return entry.value  # type: ignore[no-any-return]

    async with _lock_for(key):
        # Re-check: another caller may have filled it while we waited.
        entry = _entries.get(key)
        now = time.monotonic()
        if entry is not None and entry.expires_at > now:
            return entry.value  # type: ignore[no-any-return]

        value = await load()
        _entries[key] = _Entry(value=value, expires_at=now + ttl_seconds)
        logger.debug("cache miss for %s, cached for %ss", key, ttl_seconds)
        return value


def clear() -> None:
    """Drop everything. For tests, and for a manual refresh hook if one is added."""
    _entries.clear()
