"""Conditional HTTP response-cache contracts for ingestion adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ResponseCacheEntry:
    requested_url: str
    final_url: str
    etag: str | None
    last_modified: str | None
    content_type: str | None
    raw_sha256: str
    raw_size_bytes: int
    private_raw_path: str | None
    stored_at: datetime


class ResponseCache(Protocol):
    async def get(self, requested_url: str) -> ResponseCacheEntry | None: ...

    async def put(self, entry: ResponseCacheEntry) -> None: ...


class InMemoryResponseCache:
    """Process-local cache useful for rules-only and deterministic test profiles."""

    def __init__(self) -> None:
        self._entries: dict[str, ResponseCacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, requested_url: str) -> ResponseCacheEntry | None:
        async with self._lock:
            return self._entries.get(requested_url)

    async def put(self, entry: ResponseCacheEntry) -> None:
        async with self._lock:
            self._entries[entry.requested_url] = entry
