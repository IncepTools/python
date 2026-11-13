"""Distributed locks that only release their own.

Four properties, and all four are load-bearing:

1. **A TTL**, so a replica that dies holding the lock does not hold it forever.
2. **A unique token** per acquisition.
3. **Compare-and-delete on release**, in one atomic script. Releasing by key
   alone means a slow holder whose TTL expired deletes the *next* holder's
   lock, and now two replicas run at once — the exact thing the lock exists to
   prevent.
4. **Refresh**, for work that may outlive its TTL.

A documented limitation: this is not safe across a Redis failover. Use it to
prevent duplicate *work*, not to protect an invariant. An invariant wants a
database row lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from inceptools.core import errs

__all__ = ["Lock", "Locker", "Memory", "Noop", "NotAcquired", "NotHeld", "Redis", "run_locked"]

T = TypeVar("T")

#: Another holder has it. Normally not an error — it means "not my turn".
NOT_ACQUIRED = "LOCK_NOT_ACQUIRED"
#: Our TTL expired mid-work. Somebody else may already be running.
NOT_HELD = "LOCK_NOT_HELD"

DEFAULT_TTL = 30.0


class NotAcquired(errs.AppError):
    def __init__(self, key: str) -> None:
        super().__init__(
            kind=errs.ErrorKind.CONFLICT, message=f"lock {key!r} is already held", code=NOT_ACQUIRED
        )


class NotHeld(errs.AppError):
    def __init__(self, key: str) -> None:
        super().__init__(
            kind=errs.ErrorKind.CONFLICT,
            message=f"lock {key!r} is no longer held; the TTL is too short",
            code=NOT_HELD,
        )


@dataclass(slots=True)
class Lock:
    """One acquisition. The token is what makes release safe."""

    key: str
    token: str
    _release: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    _refresh: Callable[[float], Awaitable[None]] | None = field(default=None, repr=False)

    async def release(self) -> None:
        if self._release is not None:
            await self._release()

    async def refresh(self, ttl: float = DEFAULT_TTL) -> None:
        if self._refresh is None:
            raise NotHeld(self.key)
        await self._refresh(ttl)

    async def __aenter__(self) -> Lock:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.release()


@runtime_checkable
class Locker(Protocol):
    async def acquire(self, key: str, ttl: float = DEFAULT_TTL) -> Lock: ...


class Memory:
    """Correct for exactly one process. The default when nothing else is wired."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, ttl: float = DEFAULT_TTL) -> Lock:
        ttl = ttl if ttl > 0 else DEFAULT_TTL
        token = uuid.uuid4().hex
        async with self._lock:
            found = self._held.get(key)
            if found is not None and found[1] > time.monotonic():
                raise NotAcquired(key)
            self._held[key] = (token, time.monotonic() + ttl)

        async def release() -> None:
            async with self._lock:
                current = self._held.get(key)
                # Compare-and-delete: only release what is still ours.
                if current is not None and current[0] == token:
                    self._held.pop(key, None)

        async def refresh(new_ttl: float) -> None:
            async with self._lock:
                current = self._held.get(key)
                if current is None or current[0] != token:
                    raise NotHeld(key)
                self._held[key] = (token, time.monotonic() + (new_ttl or DEFAULT_TTL))

        return Lock(key=key, token=token, _release=release, _refresh=refresh)


_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then return redis.call("DEL", KEYS[1]) else return 0 end
"""

_REFRESH_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("PEXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""


class Redis:
    """A lock shared across replicas."""

    def __init__(self, client: Any, prefix: str = "") -> None:
        self.client = client
        self.prefix = (prefix + ":") if prefix and not prefix.endswith(":") else prefix

    def _full(self, key: str) -> str:
        return f"{self.prefix}lock:{key}"

    async def acquire(self, key: str, ttl: float = DEFAULT_TTL) -> Lock:
        ttl = ttl if ttl > 0 else DEFAULT_TTL
        full = self._full(key)
        token = uuid.uuid4().hex

        acquired = await self.client.set(full, token, nx=True, ex=int(ttl) or 1)
        if not acquired:
            raise NotAcquired(key)

        async def release() -> None:
            # One atomic script: a GET-then-DEL from the client would release
            # the next holder's lock if our TTL expired in between.
            result = await self.client.eval(_RELEASE_LUA, 1, full, token)
            if not result:
                raise NotHeld(key)

        async def refresh(new_ttl: float) -> None:
            millis = int((new_ttl or DEFAULT_TTL) * 1000)
            result = await self.client.eval(_REFRESH_LUA, 1, full, token, millis)
            if not result:
                raise NotHeld(key)

        return Lock(key=key, token=token, _release=release, _refresh=refresh)


class Noop:
    """Always succeeds. For when one-replica-only is guaranteed by deployment."""

    async def acquire(self, key: str, ttl: float = DEFAULT_TTL) -> Lock:
        return Lock(key="noop", token="noop")


async def run_locked(
    locker: Locker,
    key: str,
    fn: Callable[[], Awaitable[T]],
    *,
    ttl: float = DEFAULT_TTL,
) -> tuple[bool, T | None]:
    """Run *fn* if we get the lock. Returns ``(ran, result)``.

    Not getting the lock is **not** an error: it means another replica has it,
    which is the whole point. Release runs in a shielded task so a cancelled
    request still releases — otherwise the lock sits until its TTL and the
    next run is skipped for no reason.
    """
    try:
        lock = await locker.acquire(key, ttl)
    except NotAcquired:
        return False, None

    try:
        return True, await fn()
    finally:
        with contextlib.suppress(Exception):
            await asyncio.shield(asyncio.ensure_future(lock.release()))
