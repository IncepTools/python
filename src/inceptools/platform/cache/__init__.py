"""Memory or Redis; a cache failure is never a request failure.

One invariant, and everything here follows from it: **the cache may not take
the service down**. A read error degrades to a miss, a write error is
swallowed, and a value that no longer decodes is deleted and treated as a miss
so a changed shape self-heals across a deploy instead of needing a flush.

:meth:`Cache.delete` is the deliberate exception — its error *is* raised. A
silently failed invalidation leaves the cache ahead of the database, and the
caller may well want to refuse the write rather than serve a lie.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

__all__ = [
    "Cache",
    "Memory",
    "Noop",
    "Redis",
    "get",
    "get_or_set",
    "key",
    "set",
]

_log = logging.getLogger("inceptools.cache")
T = TypeVar("T")

#: Returned by a backend when nothing is stored.
MISS = object()


def key(*parts: str) -> str:
    """``key("catalog", "product", pid)`` -> ``"catalog:product:<pid>"``.

    Always namespace by module. An un-namespaced key collides across modules,
    and the collision looks like data corruption rather than a cache bug.
    """
    return ":".join(str(p) for p in parts if p != "")


@runtime_checkable
class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl: float = 0) -> None: ...
    async def delete(self, *keys: str) -> None: ...
    async def delete_prefix(self, prefix: str) -> None: ...
    async def ping(self) -> None: ...


class Memory:
    """A per-process cache with a background sweeper.

    With N replicas you have N independent caches: a delete on one leaves the
    other N-1 serving stale data until the TTL expires. Fine for a hot
    read-mostly lookup, wrong for anything that must be consistent after a
    write.
    """

    def __init__(self, sweep_interval: float = 60.0) -> None:
        self._entries: dict[str, tuple[bytes, float]] = {}
        self._interval = sweep_interval if sweep_interval > 0 else 60.0
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False

    async def on_application_ready(self) -> None:
        self.start_sweeper()

    def start_sweeper(self) -> None:
        if self._sweeper is None and not self._closed:
            with contextlib.suppress(RuntimeError):  # no running loop yet
                self._sweeper = asyncio.get_running_loop().create_task(self._sweep())

    async def _sweep(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._interval)
            now = time.monotonic()
            for k, (_, expires) in list(self._entries.items()):
                if expires and expires <= now:
                    self._entries.pop(k, None)

    async def get(self, key: str) -> bytes | None:
        found = self._entries.get(key)
        if found is None:
            return None
        value, expires = found
        if expires and expires <= time.monotonic():
            self._entries.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: bytes, ttl: float = 0) -> None:
        self._entries[key] = (value, time.monotonic() + ttl if ttl > 0 else 0.0)

    async def delete(self, *keys: str) -> None:
        for k in keys:
            self._entries.pop(k, None)

    async def delete_prefix(self, prefix: str) -> None:
        for k in [k for k in self._entries if k.startswith(prefix)]:
            self._entries.pop(k, None)

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sweeper
            self._sweeper = None
        self._entries.clear()


class Redis:
    """A shared cache, degrading to a miss on any read failure.

    ``delete_prefix`` uses ``SCAN``, never ``KEYS``: ``KEYS`` blocks the whole
    server for the length of the keyspace, which turns a cache invalidation
    into an outage of everything else using that Redis.
    """

    SCAN_COUNT = 256

    def __init__(self, client: Any, prefix: str = "") -> None:
        self.client = client
        self.prefix = (prefix + ":") if prefix and not prefix.endswith(":") else prefix

    def _full(self, key: str) -> str:
        return f"{self.prefix}{key}"

    async def get(self, key: str) -> bytes | None:
        try:
            return await self.client.get(self._full(key))
        except Exception as exc:
            _log.warning("cache read failed; treating as a miss", exc_info=exc)
            return None

    async def set(self, key: str, value: bytes, ttl: float = 0) -> None:
        try:
            if ttl > 0:
                await self.client.set(self._full(key), value, ex=int(ttl))
            else:
                await self.client.set(self._full(key), value)
        except Exception as exc:
            _log.warning("cache write failed", exc_info=exc)

    async def delete(self, *keys: str) -> None:
        # Deliberately not swallowed: see the module docstring.
        if keys:
            await self.client.delete(*(self._full(k) for k in keys))

    async def delete_prefix(self, prefix: str) -> None:
        pattern = f"{self._full(prefix)}*"
        cursor = 0
        while True:
            cursor, keys = await self.client.scan(cursor, match=pattern, count=self.SCAN_COUNT)
            if keys:
                await self.client.delete(*keys)
            if cursor == 0:
                return

    async def ping(self) -> None:
        await self.client.ping()

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None) or getattr(self.client, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


class Noop:
    """Stores nothing. Inject it to turn caching off without nil checks."""

    async def get(self, key: str) -> bytes | None:
        return None

    async def set(self, key: str, value: bytes, ttl: float = 0) -> None:
        return None

    async def delete(self, *keys: str) -> None:
        return None

    async def delete_prefix(self, prefix: str) -> None:
        return None

    async def ping(self) -> None:
        return None


# -- typed helpers -------------------------------------------------------


async def get(cache: Cache | None, key: str, default: Any = None) -> Any:
    """Read and JSON-decode. Anything that goes wrong is a miss."""
    if cache is None:
        return default
    raw = await cache.get(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A value written by an older deploy with a different shape. Delete it
        # so the next read repopulates, rather than failing forever.
        _log.debug("cache value could not be decoded; treating as a miss", extra={"cache_key": key})
        with contextlib.suppress(Exception):
            await cache.delete(key)
        return default


async def set(cache: Cache | None, key: str, value: Any, ttl: float = 0) -> None:
    """JSON-encode and write. A failure to encode is logged, never raised."""
    if cache is None:
        return
    try:
        raw = json.dumps(value, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _log.warning("cache value could not be encoded", exc_info=exc, extra={"cache_key": key})
        return
    await cache.set(key, raw, ttl)


async def get_or_set(
    cache: Cache | None,
    key: str,
    ttl: float,
    load: Callable[[], Awaitable[T]],
) -> T:
    """Return the cached value, or call *load* and cache what it returns.

    ``load``'s exception propagates **uncached**. Caching a failure turns one
    bad minute into a bad TTL.
    """
    hit = await get(cache, key, MISS)
    if hit is not MISS:
        return cast(T, hit)
    value = await load()
    await set(cache, key, value, ttl)
    return value
