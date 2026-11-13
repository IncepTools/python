"""Typed in-process events; exceptions contained, contexts detached.

Decouples a side effect from its cause. Dispatch is keyed on the event's
**class**, so a handler declares what it listens for by type annotation rather
than by a string nobody checks.

Two rules worth stating:

* An **async** handler runs on a detached context: it keeps the trace id and
  the account, and drops the cancellation. Handing a handler the request's task
  means the audit write is cancelled the moment the client disconnects, and
  nothing tells you.
* A **sync** handler cannot veto the event. Async handlers registered before it
  have already started. A prerequisite belongs in the service *before*
  ``publish``, not in a subscriber.

Not durable. When the effect must survive a crash, use
:mod:`inceptools.reliability.outbox`.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import inspect
import logging
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from inceptools.core import ctxkit, errs

__all__ = ["Bus", "Mode", "Subscriber"]

_log = logging.getLogger("inceptools.events")
E = TypeVar("E")

Handler = Callable[[Any], Awaitable[None] | None]


class Mode(str, enum.Enum):
    #: Own task, detached context. ``publish`` neither waits nor sees the error.
    ASYNC = "async"
    #: Inline. ``publish`` waits and collects the error.
    SYNC = "sync"


@dataclass(slots=True)
class Subscriber:
    name: str
    event_type: type
    mode: Mode
    handler: Handler


@dataclass
class Bus:
    """An in-process publish/subscribe bus."""

    _subs: dict[type, list[Subscriber]] = field(default_factory=dict, repr=False)
    _running: set[asyncio.Task[None]] = field(default_factory=set, repr=False)

    # -- registration ----------------------------------------------------

    def on(
        self,
        event_type: type[E],
        handler: Callable[[E], Awaitable[None] | None],
        *,
        name: str = "",
        mode: Mode = Mode.ASYNC,
    ) -> Bus:
        """Register *handler* for *event_type*.

        Dispatch is by exact class, not by subclass: a handler for ``Order``
        does not receive an ``ExpressOrder``. Explicit beats surprising.
        """
        resolved = name or getattr(handler, "__qualname__", None) or repr(handler)
        self._subs.setdefault(event_type, []).append(
            Subscriber(name=resolved, event_type=event_type, mode=mode, handler=handler)
        )
        return self

    def subscribe(
        self, event_type: type[E] | None = None, *, name: str = "", mode: Mode = Mode.ASYNC
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator form. The event type is read from the annotation.

        @bus.subscribe(mode=Mode.SYNC)
        async def record(event: OrderPlaced) -> None: ...
        """

        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            resolved = event_type or _event_type_of(handler)
            self.on(resolved, handler, name=name, mode=mode)
            return handler

        return decorate

    def subscribers(self) -> dict[str, list[str]]:
        """``{"OrderPlaced": ["audit", "email"]}`` — who listens to what."""
        return {t.__name__: [s.name for s in subs] for t, subs in self._subs.items()}

    # -- publishing ------------------------------------------------------

    async def publish(self, event: Any) -> None:
        """Deliver *event* to its subscribers, in registration order.

        Raises the combined failure of the **sync** subscribers only. An async
        subscriber's failure is logged and reaches nobody — which is the trade
        you accepted by making it async.
        """
        subs = list(self._subs.get(type(event), ()))
        if not subs:
            return

        failures: list[BaseException] = []
        for sub in subs:
            if sub.mode is Mode.SYNC:
                try:
                    await self._invoke(sub, event)
                except Exception as exc:
                    failures.append(exc)
            else:
                self._spawn(sub, event)

        if not failures:
            return
        if len(failures) == 1 and isinstance(failures[0], errs.AppError):
            raise failures[0]
        joined = "; ".join(str(f) for f in failures)
        raise errs.internal(f"events: {joined}").with_cause(failures[0])

    def _spawn(self, sub: Subscriber, event: Any) -> None:
        ctx = ctxkit.detach()

        async def run() -> None:
            token = ctxkit.set_context(ctx)
            try:
                await self._invoke(sub, event)
            except Exception as exc:
                _log.error(
                    "event handler failed",
                    exc_info=exc,
                    extra={"handler": sub.name, "event": type(event).__name__},
                )
            finally:
                ctxkit.clear(token)

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            # No loop: the publisher is synchronous. Running inline is the only
            # honest option, and better than dropping the event silently.
            _log.debug("no running loop; delivering %s inline", sub.name)
            asyncio.run(run())
            return

        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _invoke(self, sub: Subscriber, event: Any) -> None:
        try:
            result = sub.handler(event)
            if inspect.isawaitable(result):
                await result
        except errs.AppError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Contained: one bad subscriber must not take down the publisher
            # or its siblings.
            raise errs.internal(f"events: handler {sub.name} failed").with_cause(exc) from exc

    # -- lifecycle -------------------------------------------------------

    async def wait(self, timeout: float | None = None) -> None:
        """Block until every async handler started so far has finished."""
        pending = list(self._running)
        if not pending:
            return
        _done, still_running = await asyncio.wait(pending, timeout=timeout)
        if still_running:
            raise errs.internal("events: handlers still running at the drain deadline")

    async def on_module_destroy(self) -> None:
        with contextlib.suppress(errs.AppError):
            await self.wait(timeout=10.0)
        for task in list(self._running):
            task.cancel()


def _event_type_of(handler: Callable[..., Any]) -> type:
    """The first parameter's annotation."""
    try:
        hints = typing.get_type_hints(handler)
    except Exception as exc:
        raise errs.internal(
            f"cannot read the event type of {handler!r}; pass it explicitly"
        ).with_cause(exc) from exc

    params = [p for p in inspect.signature(handler).parameters if p not in ("self", "cls")]
    if not params:
        raise errs.internal(f"{handler!r} takes no event parameter")
    found = hints.get(params[0])
    if not isinstance(found, type):
        raise errs.internal(f"{handler!r} must annotate its event parameter with the event class")
    return found
