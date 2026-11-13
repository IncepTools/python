"""Fail fast on a dependency that is down.

A breaker is per **dependency**, never per service: one breaker in front of
everything means a sick payment provider stops your product catalogue.

Only genuine unavailability counts. A 404 from a dependency is a healthy
answer — counting it opens the breaker on a workload that is merely asking for
things that do not exist.

In half-open, exactly **one** probe is allowed through at a time. Letting
everyone through on the cooldown boundary is a stampede onto a dependency that
has just come back, which usually puts it straight back down.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from inceptools.core import errs

__all__ = ["Breaker", "State", "counts_as_failure"]

_log = logging.getLogger("inceptools.breaker")
T = TypeVar("T")


class State(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


_COUNTING_KINDS = frozenset(
    {errs.ErrorKind.UNAVAILABLE, errs.ErrorKind.TIMEOUT, errs.ErrorKind.INTERNAL}
)


def counts_as_failure(exc: BaseException) -> bool:
    """True when *exc* says the dependency is unwell, not that the request was."""
    if isinstance(exc, TimeoutError):
        return True
    if not isinstance(exc, errs.AppError):
        # An unclassified exception out of a client call is a real failure.
        return True
    return exc.kind in _COUNTING_KINDS


class Breaker:
    """One dependency's health."""

    def __init__(
        self,
        name: str = "dependency",
        *,
        threshold: int = 5,
        cooldown: float = 30.0,
        counts: Callable[[BaseException], bool] = counts_as_failure,
        on_state_change: Callable[[State, State], None] | None = None,
    ) -> None:
        self.name = name
        self.threshold = max(threshold, 1)
        self.cooldown = cooldown if cooldown > 0 else 30.0
        self.counts = counts
        self.on_state_change = on_state_change

        self._state = State.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probing = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> State:
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run *fn*, or fail fast when the circuit is open."""
        allowed, probe = await self._allow()
        if not allowed:
            raise (
                errs.unavailable(f"{self.name} is unavailable")
                .with_code("CIRCUIT_OPEN")
                .with_meta(breaker=self.name)
            )

        try:
            result = await fn()
        except asyncio.CancelledError:
            await self._record(None, probe)
            raise
        except Exception as exc:
            await self._record(exc, probe)
            raise
        await self._record(None, probe)
        return result

    async def _allow(self) -> tuple[bool, bool]:
        async with self._lock:
            if self._state is State.CLOSED:
                return True, False

            if self._state is State.OPEN:
                if time.monotonic() - self._opened_at < self.cooldown:
                    return False, False
                self._transition(State.HALF_OPEN)
                self._probing = True
                return True, True

            # Half-open: one probe at a time.
            if self._probing:
                return False, False
            self._probing = True
            return True, True

    async def _record(self, exc: BaseException | None, probe: bool) -> None:
        async with self._lock:
            if probe:
                self._probing = False

            if exc is None or not self.counts(exc):
                if self._state is not State.CLOSED:
                    self._transition(State.CLOSED)
                self._failures = 0
                return

            self._failures += 1

            if self._state is State.HALF_OPEN:
                self._opened_at = time.monotonic()
                self._transition(State.OPEN)
                _log.warning(
                    "circuit re-opened: the probe failed",
                    extra={"breaker": self.name, "cooldown_s": self.cooldown},
                )
                return

            if self._state is State.CLOSED and self._failures >= self.threshold:
                self._opened_at = time.monotonic()
                self._transition(State.OPEN)
                _log.error(
                    "circuit opened",
                    extra={
                        "breaker": self.name,
                        "failures": self._failures,
                        "cooldown_s": self.cooldown,
                        "error": str(exc),
                    },
                )

    def _transition(self, to: State) -> None:
        """Called under the lock. ``on_state_change`` must be cheap and must
        not re-enter the breaker."""
        previous, self._state = self._state, to
        if previous is to or self.on_state_change is None:
            return
        try:
            self.on_state_change(previous, to)
        except Exception:
            _log.exception("breaker state-change callback failed", extra={"breaker": self.name})
