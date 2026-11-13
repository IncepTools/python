"""Cron and intervals, singleton and overlap-safe.

Two decisions that are the whole value of this package:

* **A malformed cron expression fails the boot**, not the night it was meant
  to run. Jobs are validated when added.
* **A run still going when the next is due is skipped, not queued.** Queueing
  a slow job behind itself turns a slowdown into an unbounded backlog, and the
  backlog is discovered at 3am by the thing it starved.

``singleton=True`` takes a distributed lock, so a job runs on one replica
rather than on all of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import inspect
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from inceptools.core import ctxkit, errs
from inceptools.platform import lock as lock_mod
from inceptools.platform.scheduler.cron import CronError, Schedule, parse, parse_every

__all__ = ["CronError", "Job", "Scheduler", "job"]

_log = logging.getLogger("inceptools.scheduler")

#: A job with no timeout is a job that can hang forever. Zero is not unlimited.
DEFAULT_TIMEOUT = 300.0


@dataclass(slots=True)
class Job:
    """One recurring unit of work."""

    name: str
    run: Callable[[], Awaitable[None] | None]
    #: A five-field cron expression. Set exactly one of this or :attr:`every`.
    cron: str = ""
    #: An interval in seconds, or a duration string like ``"30s"``.
    every: float | str = 0
    #: Take a distributed lock so only one replica runs it.
    singleton: bool = False
    #: How long a single run may take. Defaults to five minutes.
    timeout: float = 0
    #: Lock TTL. Defaults to :attr:`timeout`.
    lock_ttl: float = 0
    #: Fire once at startup as well as on schedule.
    run_on_start: bool = False
    #: Sleep up to this many seconds before the first run, so N replicas
    #: starting together do not all hit the database on the same second.
    jitter: float = 0

    _schedule: Schedule | None = field(default=None, repr=False)
    _interval: float = field(default=0.0, repr=False)

    def describe(self) -> str:
        when = self.cron or f"every {self._interval or self.every}s"
        return f"{self.name} ({when}{', singleton' if self.singleton else ''})"


def job(
    name: str = "",
    *,
    cron: str = "",
    every: float | str = 0,
    singleton: bool = False,
    timeout: float = 0,
    run_on_start: bool = False,
    jitter: float = 0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a method as a scheduled job.

    @job(cron="0 3 * * *", singleton=True, timeout=600)
    async def expire_abandoned_carts(self) -> None: ...
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__inceptools_job__ = {  # type: ignore[attr-defined]
            "name": name or fn.__name__,
            "cron": cron,
            "every": every,
            "singleton": singleton,
            "timeout": timeout,
            "run_on_start": run_on_start,
            "jitter": jitter,
        }
        return fn

    return decorate


class Scheduler:
    """Runs jobs. Registered as a *ready* hook, not an init hook.

    A reconciler that starts before the listener races the boot: it can be
    querying a dependency that the init phase has not finished connecting.
    """

    def __init__(self, locker: lock_mod.Locker | None = None) -> None:
        self.locker = locker
        self._jobs: dict[str, Job] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._running: set[str] = set()
        self._started = False
        self._stopping = asyncio.Event()

    # -- registration ----------------------------------------------------

    def add(self, item: Job) -> Scheduler:
        """Validate and register a job. Raises rather than deferring the failure."""
        if self._started:
            raise errs.internal(f"scheduler: job {item.name!r} added after start")
        if not item.name:
            raise errs.internal("scheduler: job has no name")
        if item.name in self._jobs:
            raise errs.internal(f"scheduler: two jobs named {item.name!r}")
        if item.run is None:
            raise errs.internal(f"scheduler: job {item.name!r} has no run function")

        has_cron = bool(item.cron)
        has_every = bool(item.every)
        if has_cron == has_every:
            raise errs.internal(
                f"scheduler: job {item.name!r} must set exactly one of cron or every"
            )

        if has_cron:
            try:
                item._schedule = parse(item.cron)
            except CronError as exc:
                raise errs.internal(
                    f"scheduler: job {item.name!r} has an invalid cron expression "
                    f"{item.cron!r}: {exc}"
                ).with_cause(exc) from exc
        else:
            item._interval = (
                parse_every(item.every) if isinstance(item.every, str) else float(item.every)
            )
            if item._interval <= 0:
                raise errs.internal(f"scheduler: job {item.name!r} has a non-positive interval")

        if item.singleton and self.locker is None:
            raise errs.internal(
                f"scheduler: job {item.name!r} is singleton but the scheduler has no locker"
            )

        item.timeout = item.timeout if item.timeout > 0 else DEFAULT_TIMEOUT
        item.lock_ttl = item.lock_ttl if item.lock_ttl > 0 else item.timeout

        self._jobs[item.name] = item
        return self

    def add_from(self, obj: Any) -> Scheduler:
        """Register every ``@job``-marked method on *obj*."""
        for attr in dir(obj):
            member = getattr(obj, attr, None)
            spec = getattr(member, "__inceptools_job__", None)
            if spec is not None and callable(member):
                self.add(Job(run=member, **spec))
        return self

    @property
    def jobs(self) -> list[str]:
        return sorted(self._jobs)

    def describe(self) -> list[str]:
        return [self._jobs[n].describe() for n in self.jobs]

    # -- lifecycle -------------------------------------------------------

    async def on_application_ready(self) -> None:
        await self.start()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping.clear()
        loop = asyncio.get_running_loop()
        for item in self._jobs.values():
            self._tasks.append(loop.create_task(self._loop(item), name=f"job:{item.name}"))
        _log.info("scheduler started", extra={"jobs": len(self._jobs)})

    async def on_module_destroy(self) -> None:
        await self.stop()

    async def stop(self, timeout: float = 10.0) -> None:
        if not self._started:
            return
        self._started = False
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            _done, pending = await asyncio.wait(self._tasks, timeout=timeout)
            if pending:
                _log.warning("scheduler: jobs still running at the drain deadline")
        self._tasks.clear()

    # -- the per-job loop ------------------------------------------------

    async def _loop(self, item: Job) -> None:
        try:
            if item.jitter > 0:
                await self._sleep(random.uniform(0, item.jitter))
            if item.run_on_start:
                await self._fire(item)

            while not self._stopping.is_set():
                await self._sleep(self._delay_until_next(item))
                if self._stopping.is_set():
                    return
                await self._fire(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception("scheduled job loop failed", exc_info=exc, extra={"job": item.name})

    def _delay_until_next(self, item: Job) -> float:
        if item._schedule is not None:
            now = dt.datetime.now()
            delay = (item._schedule.next_after(now) - now).total_seconds()
            return max(delay, 1.0)
        return item._interval

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)

    async def _fire(self, item: Job) -> None:
        if item.name in self._running:
            # Skipped, not queued. See the module docstring.
            _log.warning(
                "scheduled job skipped: the previous run is still going",
                extra={"job": item.name},
            )
            return

        self._running.add(item.name)
        started = time.monotonic()
        stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%S")
        token = ctxkit.set_context(
            ctxkit.RequestContext(
                request_id=f"job-{item.name}-{stamp}",
                trace_id=f"job-{item.name}-{stamp}",
                route=f"job:{item.name}",
            )
        )
        try:
            if item.singleton:
                assert self.locker is not None
                ran, _ = await lock_mod.run_locked(
                    self.locker,
                    f"job:{item.name}",
                    lambda: self._run_once(item),
                    ttl=item.lock_ttl,
                )
                if not ran:
                    _log.debug(
                        "scheduled job is running on another replica", extra={"job": item.name}
                    )
                    return
            else:
                await self._run_once(item)
        finally:
            ctxkit.clear(token)
            self._running.discard(item.name)
            _log.debug(
                "scheduled job finished",
                extra={"job": item.name, "ms": int((time.monotonic() - started) * 1000)},
            )

    async def _run_once(self, item: Job) -> None:
        started = time.monotonic()
        try:
            result = item.run()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, item.timeout)
        except asyncio.CancelledError:
            # A routine deploy must not page anyone.
            _log.info("scheduled job stopped for shutdown", extra={"job": item.name})
            raise
        except TimeoutError:
            _log.warning(
                "scheduled job hit its timeout",
                extra={"job": item.name, "timeout_s": item.timeout},
            )
        except Exception as exc:
            _log.error(
                "scheduled job failed",
                exc_info=exc,
                extra={"job": item.name, "ms": int((time.monotonic() - started) * 1000)},
            )
        else:
            _log.info(
                "scheduled job finished",
                extra={"job": item.name, "ms": int((time.monotonic() - started) * 1000)},
            )
