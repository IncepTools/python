"""Init, ready, reload, shutdown — reverse order, with a real drain deadline.

Four phases, because they answer different questions:

``init``
    Construct and connect. Anything that can fail should fail here, before the
    socket opens.
``ready``
    Everything is connected; start accepting traffic and begin background work.
``reload``
    Re-read configuration without dropping connections. SIGHUP on a server, an
    explicit call on a serverless control plane, which has no signals.
``shutdown``
    Stop taking work, drain what is in flight, release in **reverse** order —
    a repository before the engine it borrows sessions from.

A shutdown hook that hangs cannot hold the process open past the drain
deadline; it is abandoned and reported, not awaited forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "BeforeApplicationShutdown",
    "Hook",
    "HookFailure",
    "Lifecycle",
    "OnApplicationBootstrap",
    "OnApplicationReady",
    "OnApplicationReload",
    "OnApplicationShutdown",
    "OnModuleDestroy",
    "OnModuleInit",
    "Phase",
]

_log = logging.getLogger("inceptools.lifecycle")

Hook = Callable[[], Any | Awaitable[Any]]


class Phase(str, Enum):
    INIT = "init"
    READY = "ready"
    RELOAD = "reload"
    #: Stop accepting new work. Runs before anything is disposed, so a queue
    #: can close its input while its handler's dependencies are all alive.
    BEFORE_SHUTDOWN = "before_shutdown"
    #: The last word, after every provider has been disposed.
    SHUTDOWN = "shutdown"


@runtime_checkable
class OnModuleInit(Protocol):
    """Implemented by a provider that needs to connect before traffic arrives."""

    async def on_module_init(self) -> None: ...


@runtime_checkable
class OnApplicationBootstrap(Protocol):
    """Every module has initialised. Runs before ``on_application_ready``."""

    async def on_application_bootstrap(self) -> None: ...


@runtime_checkable
class OnApplicationReady(Protocol):
    async def on_application_ready(self) -> None: ...


@runtime_checkable
class BeforeApplicationShutdown(Protocol):
    """The first thing drain does: stop accepting new work.

    Runs before anything is disposed, so a queue can stop taking messages
    while its handler's dependencies are all still alive.
    """

    async def before_application_shutdown(self) -> None: ...


@runtime_checkable
class OnApplicationReload(Protocol):
    async def on_application_reload(self) -> None: ...


@runtime_checkable
class OnModuleDestroy(Protocol):
    async def on_module_destroy(self) -> None: ...


@runtime_checkable
class OnApplicationShutdown(Protocol):
    async def on_application_shutdown(self) -> None: ...


@dataclass(frozen=True, slots=True)
class HookFailure:
    """One hook that raised or timed out."""

    phase: Phase
    name: str
    error: BaseException

    def __str__(self) -> str:
        return f"{self.phase.value}:{self.name}: {self.error!r}"


@dataclass
class _Registered:
    name: str
    fn: Hook


@dataclass
class Lifecycle:
    """The ordered hook registry for one application."""

    #: How long shutdown may take in total before remaining hooks are abandoned.
    drain_timeout: float = 25.0
    #: Per-hook ceiling during init/ready. ``None`` means no limit.
    hook_timeout: float | None = None

    _hooks: dict[Phase, list[_Registered]] = field(
        default_factory=lambda: {p: [] for p in Phase}, repr=False
    )
    _failures: list[HookFailure] = field(default_factory=list, repr=False)
    _phase: Phase | None = field(default=None, repr=False)
    _started: bool = field(default=False, repr=False)

    # -- registration ----------------------------------------------------

    def on(self, phase: Phase, fn: Hook, *, name: str = "") -> Lifecycle:
        self._hooks[phase].append(_Registered(name=name or _hook_name(fn), fn=fn))
        return self

    def on_init(self, fn: Hook, *, name: str = "") -> Lifecycle:
        return self.on(Phase.INIT, fn, name=name)

    def on_ready(self, fn: Hook, *, name: str = "") -> Lifecycle:
        return self.on(Phase.READY, fn, name=name)

    def on_reload(self, fn: Hook, *, name: str = "") -> Lifecycle:
        return self.on(Phase.RELOAD, fn, name=name)

    def on_shutdown(self, fn: Hook, *, name: str = "") -> Lifecycle:
        return self.on(Phase.SHUTDOWN, fn, name=name)

    def register_object(
        self, obj: Any, *, name: str = "", phases: tuple[Phase, ...] | None = None
    ) -> Lifecycle:
        """Wire whichever lifecycle protocols *obj* implements.

        *phases* narrows which ones are taken. The application passes
        ``(INIT, READY, RELOAD)`` for container-built providers, because the
        container already disposes those itself in reverse construction order
        — a stronger ordering than registration order can give.
        """
        label = name or type(obj).__name__
        # ``on_module_destroy`` is deliberately absent: the container calls
        # it, in reverse *construction* order, which is a stronger guarantee
        # than reverse registration order. Registering it here as well would
        # call it twice.
        pairs = (
            (Phase.INIT, "on_module_init"),
            (Phase.READY, "on_application_bootstrap"),
            (Phase.READY, "on_application_ready"),
            (Phase.RELOAD, "on_application_reload"),
            (Phase.BEFORE_SHUTDOWN, "before_application_shutdown"),
            (Phase.SHUTDOWN, "on_application_shutdown"),
        )
        for phase, attr in pairs:
            if phases is not None and phase not in phases:
                continue
            hook = getattr(obj, attr, None)
            if callable(hook):
                self.on(phase, hook, name=f"{label}.{attr}")
        return self

    def hooks(self, phase: Phase) -> list[str]:
        """The registered hook names for *phase*, in registration order."""
        return [h.name for h in self._hooks[phase]]

    @property
    def failures(self) -> list[HookFailure]:
        return list(self._failures)

    @property
    def phase(self) -> Phase | None:
        return self._phase

    # -- execution -------------------------------------------------------

    async def start(self) -> None:
        """Run ``init`` then ``ready``. The first failure aborts and propagates."""
        if self._started:
            return
        await self._run(Phase.INIT, fail_fast=True)
        await self._run(Phase.READY, fail_fast=True)
        self._started = True

    async def reload(self) -> list[HookFailure]:
        """Re-read configuration. Never fail-fast — a bad reload keeps the old
        configuration running rather than taking the process down."""
        return await self._run(Phase.RELOAD, fail_fast=False)

    async def before_shutdown(self, timeout: float | None = None) -> list[HookFailure]:
        """Stop accepting work, before anything is disposed."""
        if not self._hooks[Phase.BEFORE_SHUTDOWN]:
            self._phase = Phase.BEFORE_SHUTDOWN
            return []
        budget = self.drain_timeout if timeout is None else timeout
        return await self._run(Phase.BEFORE_SHUTDOWN, fail_fast=False, reverse=True, budget=budget)

    async def shutdown(self, timeout: float | None = None) -> list[HookFailure]:
        """Drain in reverse registration order, within the deadline."""
        if not self._hooks[Phase.SHUTDOWN]:
            self._phase = Phase.SHUTDOWN
            return []
        budget = self.drain_timeout if timeout is None else timeout
        return await self._run(Phase.SHUTDOWN, fail_fast=False, reverse=True, budget=budget)

    async def _run(
        self,
        phase: Phase,
        *,
        fail_fast: bool,
        reverse: bool = False,
        budget: float | None = None,
    ) -> list[HookFailure]:
        self._phase = phase
        hooks = list(self._hooks[phase])
        if reverse:
            hooks.reverse()

        deadline = (time.monotonic() + budget) if budget is not None else None
        failures: list[HookFailure] = []

        for hook in hooks:
            remaining: float | None = self.hook_timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = HookFailure(
                        phase, hook.name, TimeoutError("drain deadline exceeded before this hook")
                    )
                    failures.append(failure)
                    _log.warning("lifecycle hook abandoned: %s", failure)
                    continue

            try:
                await _invoke(hook.fn, remaining)
            except Exception as exc:
                failure = HookFailure(phase, hook.name, exc)
                failures.append(failure)
                if fail_fast:
                    self._failures.extend(failures)
                    raise
                _log.exception("lifecycle hook failed: %s", failure)

        self._failures.extend(failures)
        return failures


async def _invoke(fn: Hook, timeout: float | None) -> None:
    result = fn()
    if not inspect.isawaitable(result):
        return
    if timeout is None:
        await result
        return
    task = asyncio.ensure_future(result)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task
        raise TimeoutError(f"hook exceeded {timeout:.1f}s") from None


def _hook_name(fn: Hook) -> str:
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)
