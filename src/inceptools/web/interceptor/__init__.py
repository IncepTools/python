"""Around-the-handler concerns: timing, headers, audit on success.

An interceptor wraps the handler. It must call ``next`` exactly once on any
path where the handler should run, and return what ``next`` returned unless it
is deliberately replacing the outcome.

Interceptors run **inside** guards: there is no point timing a request that
authorization is about to refuse, and an audit-on-success interceptor must not
record an action that never happened.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from inceptools.web.execution import ExecutionContext

__all__ = [
    "Interceptor",
    "InterceptorFunc",
    "after",
    "header",
    "of",
    "on_success",
    "run_interceptors",
    "timing",
]

_log = logging.getLogger("inceptools.interceptor")

Next = Callable[[], Awaitable[Any]]
Wrap = Callable[[ExecutionContext, Next], Awaitable[Any]]


@runtime_checkable
class Interceptor(Protocol):
    @property
    def name(self) -> str: ...

    async def intercept(self, ctx: ExecutionContext, call_next: Next) -> Any: ...


class InterceptorFunc:
    __slots__ = ("_name", "_wrap")

    def __init__(self, name: str, wrap: Wrap) -> None:
        self._name = name
        self._wrap = wrap

    @property
    def name(self) -> str:
        return self._name

    async def intercept(self, ctx: ExecutionContext, call_next: Next) -> Any:
        return await self._wrap(ctx, call_next)

    def __repr__(self) -> str:
        return f"<interceptor {self._name}>"


def of(name: str, wrap: Wrap) -> Interceptor:
    return InterceptorFunc(name, wrap)


async def run_interceptors(
    interceptors: list[Interceptor], ctx: ExecutionContext, handler: Next
) -> Any:
    """Compose inside-out: the first listed sees the request first and the
    response last."""
    call = handler
    for item in reversed(interceptors):
        call = _bind(item, ctx, call)
    return await call()


def _bind(item: Interceptor, ctx: ExecutionContext, call_next: Next) -> Next:
    async def run() -> Any:
        return await item.intercept(ctx, call_next)

    return run


# -- built-ins -----------------------------------------------------------


def timing(threshold_seconds: float = 1.0, *, logger: logging.Logger | None = None) -> Interceptor:
    """Warn when a handler takes longer than *threshold_seconds*.

    Only over the threshold: a line per request is the access log's job, and
    duplicating it here buries the slow ones.
    """
    log = logger or _log

    async def wrap(ctx: ExecutionContext, call_next: Next) -> Any:
        started = time.monotonic()
        try:
            return await call_next()
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= threshold_seconds:
                log.warning(
                    "slow request",
                    extra={
                        "method": ctx.method,
                        "route": ctx.full_path or ctx.path,
                        "ms": int(elapsed * 1000),
                        "threshold_ms": int(threshold_seconds * 1000),
                    },
                )

    return InterceptorFunc("timing", wrap)


def header(key: str, value: str) -> Interceptor:
    """Set a response header *after* the handler returns."""

    async def wrap(ctx: ExecutionContext, call_next: Next) -> Any:
        result = await call_next()
        headers = getattr(result, "headers", None)
        if headers is not None:
            headers[key] = value
        return result

    return InterceptorFunc(f"header:{key}", wrap)


def after(name: str, fn: Callable[[ExecutionContext, BaseException | None], Any]) -> Interceptor:
    """Run *fn* once the handler finishes, successfully or not.

    *fn*'s own failure is logged and swallowed — an audit hook must not turn a
    successful request into a 500.
    """

    async def wrap(ctx: ExecutionContext, call_next: Next) -> Any:
        error: BaseException | None = None
        try:
            return await call_next()
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                result = fn(ctx, error)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                _log.exception("after-interceptor %s failed", name)

    return InterceptorFunc(name, wrap)


def on_success(name: str, fn: Callable[[ExecutionContext], Any]) -> Interceptor:
    """Run *fn* only when the handler returned without raising."""

    async def run(ctx: ExecutionContext, error: BaseException | None) -> None:
        if error is None:
            result = fn(ctx)
            if inspect.isawaitable(result):
                await result

    return after(name, run)
