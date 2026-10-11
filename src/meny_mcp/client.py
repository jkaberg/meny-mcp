"""Async HTTP client for the FWC platform.

Responsibilities:
  * one shared httpx.AsyncClient (connection reuse)
  * polite rate limiting + bounded concurrency
  * retry with exponential backoff on 429/5xx/network errors
  * a small in-process TTL cache so repeated lookups during menu planning
    (which hits the same staples over and over) do not hammer the API
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Iterable

import httpx

from .config import Settings, settings as default_settings

log = logging.getLogger("meny_mcp.client")

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class MenyAPIError(RuntimeError):
    """Raised when the platform returns an unrecoverable error."""

    def __init__(self, status: int, message: str, url: str) -> None:
        super().__init__(f"{status} {message} ({url})")
        self.status = status
        self.message = message
        self.url = url


class _TTLCache:
    """Tiny TTL cache. Not LRU-bounded by size on purpose: a menu-planning
    session touches a few hundred products at most, and the process is
    short-lived."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._data[key] = (time.monotonic() + ttl, value)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class MenyClient:
    """Thin, well-behaved wrapper around the platform REST API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or default_settings
        self._client: httpx.AsyncClient | None = None
        self._cache = _TTLCache()
        self._sem = asyncio.Semaphore(self.settings.max_concurrency)
        self._last_request = 0.0
        self._pace_lock = asyncio.Lock()

    # ---------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "MenyClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.timeout,
                headers=self.settings.headers,
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=self.settings.max_concurrency,
                    max_keepalive_connections=self.settings.max_concurrency,
                ),
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ pacing
    async def _pace(self) -> None:
        """Enforce a minimum gap between requests, globally."""
        async with self._pace_lock:
            gap = self.settings.min_request_interval
            elapsed = time.monotonic() - self._last_request
            if elapsed < gap:
                await asyncio.sleep(gap - elapsed)
            self._last_request = time.monotonic()

    # ----------------------------------------------------------------- requests
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        cache_ttl: int | None = None,
    ) -> Any:
        """Perform a request, with retry and optional caching.

        Only GETs are cached; a calculator PUT must always be live.
        """
        await self.start()
        assert self._client is not None

        cache_key = ""
        if method == "GET" and cache_ttl:
            cache_key = f"{url}?{sorted((params or {}).items())}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                log.debug("cache hit %s", url)
                return cached

        last_exc: Exception | None = None
        for attempt in range(self.settings.max_retries):
            try:
                async with self._sem:
                    await self._pace()
                    resp = await self._client.request(
                        method, url, params=params, json=json_body
                    )

                if resp.status_code in RETRY_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )

                if resp.status_code >= 400:
                    detail = _extract_error(resp)
                    raise MenyAPIError(resp.status_code, detail, str(resp.url))

                data = resp.json() if resp.content else None
                if cache_key and cache_ttl:
                    self._cache.set(cache_key, data, cache_ttl)
                return data

            except MenyAPIError:
                raise  # 4xx that is not retryable - surface immediately
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == self.settings.max_retries - 1:
                    break
                backoff = _retry_after(exc)
                if backoff is None:
                    backoff = (2 ** attempt) * 0.5 + random.uniform(0, 0.3)
                log.warning(
                    "request failed (%s), retrying in %.2fs [%d/%d]",
                    exc.__class__.__name__, backoff,
                    attempt + 1, self.settings.max_retries,
                )
                await asyncio.sleep(backoff)

        raise MenyAPIError(0, f"request failed after retries: {last_exc}", url)

    async def get(
        self, url: str, *, params: dict[str, Any] | None = None,
        cache_ttl: int | None = None,
    ) -> Any:
        return await self.request("GET", url, params=params, cache_ttl=cache_ttl)

    async def put(self, url: str, *, params: dict[str, Any] | None = None,
                  json_body: Any = None) -> Any:
        return await self.request("PUT", url, params=params, json_body=json_body)

    # ------------------------------------------------------------------ helpers
    async def gather(self, coros: Iterable[Any]) -> list[Any]:
        """Run coroutines concurrently, returning exceptions instead of raising
        so a single dead EAN cannot sink a whole menu resolution."""
        return await asyncio.gather(*coros, return_exceptions=True)

    def clear_cache(self) -> None:
        self._cache.clear()

    @property
    def cache_size(self) -> int:
        return len(self._cache)


def _retry_after(exc: Exception) -> float | None:
    """Seconds the server asked us to wait, if it said so.

    A 429 that names its own cool-off is worth obeying exactly: guessing
    shorter gets another 429, and guessing longer wastes a menu's wall time
    that the client is timing out against. Capped, because a header asking for
    a five-minute wait is not something to honour inside a tool call.
    """
    response = getattr(exc, "response", None)
    raw = getattr(response, "headers", {}).get("retry-after") if response else None
    if not raw:
        return None
    try:
        return min(max(float(raw), 0.0), 30.0)
    except ValueError:
        return None  # HTTP-date form; the exponential backoff covers it


def _extract_error(resp: httpx.Response) -> str:
    """The platform returns {"message": ..., "code": ...} on errors."""
    try:
        payload = resp.json()
    except Exception:
        return resp.text[:200] or resp.reason_phrase
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)[:200]
    return str(payload)[:200]


# A module-level client so tools share connections and cache within a session.
client = MenyClient()
