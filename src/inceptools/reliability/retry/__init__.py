"""Backoff with jitter, retrying only what is safe to repeat.

The package **defaults to not retrying**. An unclassified error is not retried,
because retrying something you have not reasoned about turns one failure into
three — and if the operation was not idempotent, into three side effects.

What is retried by default: :data:`~inceptools.core.errs.ErrorKind.UNAVAILABLE`
and ``TIMEOUT``, plus a ``CONFLICT`` whose code is
``CONCURRENT_MODIFICATION``. A uniqueness conflict is *also* a conflict and
will fail identically forever, so it is not retried.

A cancelled or expired caller is never retried: there is no time left to retry
*in*.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from inceptools.core import errs

__all__ = ["Policy", "call", "is_retryable"]

_log = logging.getLogger("inceptools.retry")
T = TypeVar("T")

_RETRYABLE_KINDS = frozenset({errs.ErrorKind.UNAVAILABLE, errs.ErrorKind.TIMEOUT})
#: A conflict is retryable only when it is an optimistic-concurrency clash.
CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"


def is_retryable(exc: BaseException | None) -> bool:
    """Should this failure be tried again?"""
    if exc is None:
        return False
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, TimeoutError):
        return True
    if not isinstance(exc, errs.AppError):
        return False
    if exc.kind in _RETRYABLE_KINDS:
        return True
    return exc.kind is errs.ErrorKind.CONFLICT and exc.code == CONCURRENT_MODIFICATION


@dataclass(slots=True)
class Policy:
    """How many times, how long between, and what counts."""

    #: Counts the first try. ``3`` means one try and two retries; ``1`` disables.
    max_attempts: int = 3
    #: The first wait. Doubles each attempt.
    base_delay: float = 0.1
    #: Caps the growth — before jitter, so an actual wait may reach 1.5x this.
    max_delay: float = 2.0
    #: Jitter is **on** by default: without it, N callers that failed together
    #: retry together, and the dependency you are backing off from receives a
    #: synchronised stampede at every interval.
    jitter: bool = True
    retryable: Callable[[BaseException], bool] = field(default=is_retryable)
    on_retry: Callable[[int, BaseException, float], None] | None = None

    def wait_for(self, attempt: int) -> float:
        """The delay before *attempt* + 1, in seconds."""
        base = self.base_delay if self.base_delay > 0 else 0.1
        delay = base * (2 ** (attempt - 1))
        if self.max_delay > 0:
            delay = min(delay, self.max_delay)
        if not self.jitter:
            return delay
        half = delay / 2
        # Uniform in [delay/2, 3*delay/2] — full jitter around the target.
        return half + random.uniform(0, 2 * half)


async def call(fn: Callable[[], Awaitable[T]], policy: Policy | None = None) -> T:
    """Run *fn*, retrying what :attr:`Policy.retryable` accepts.

    Raises the **last** failure, not the first: it is the one that describes
    the state you ended in.
    """
    opt = policy or Policy()
    attempts = max(opt.max_attempts, 1)
    last: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last = exc
            if attempt == attempts or not opt.retryable(exc):
                raise
            wait = opt.wait_for(attempt)
            if opt.on_retry is not None:
                opt.on_retry(attempt, exc, wait)
            else:
                _log.debug(
                    "retrying",
                    extra={
                        "attempt": attempt,
                        "wait_ms": int(wait * 1000),
                        "error": str(exc),
                    },
                )
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                # The caller gave up mid-backoff. Report what actually failed,
                # not the cancellation.
                raise last from None

    assert last is not None
    raise last
