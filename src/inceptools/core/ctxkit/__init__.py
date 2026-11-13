"""Request identity on a context, and ``detach`` for background work.

Python's request-scoped state is a :mod:`contextvars` variable, and its
*cancellation* is the enclosing :class:`asyncio.Task`. Those are two different
things, and conflating them is the bug this package exists to prevent: handing
a background handler the request's task means the audit write is cancelled the
moment the client disconnects, and nothing tells you.

:func:`detach` keeps the identity — trace id, account, actor — and drops the
cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

__all__ = [
    "NO_TENANT",
    "RequestContext",
    "account_id",
    "actor_id",
    "bind",
    "clear",
    "current",
    "detach",
    "has_role",
    "new_request_id",
    "request_id",
    "require_account",
    "require_actor",
    "set_context",
    "spawn",
    "trace_id",
    "use",
]

T = TypeVar("T")

#: The error code returned when a tenanted operation has no account.
NO_TENANT = "NO_TENANT"


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who is asking, and under which trace.

    Every field is optional because this same type carries identity for a
    scheduled job and a queue worker, neither of which has a request.
    """

    request_id: str = ""
    trace_id: str = ""
    account_id: str | None = None
    actor_id: str | None = None
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    locale: str = ""
    route: str = ""
    #: Free-form values a guard or middleware attached for later stages.
    attributes: dict[str, Any] = field(default_factory=dict)

    def with_values(self, **changes: Any) -> RequestContext:
        if "roles" in changes and changes["roles"] is not None:
            changes["roles"] = frozenset(changes["roles"])
        if "scopes" in changes and changes["scopes"] is not None:
            changes["scopes"] = frozenset(changes["scopes"])
        if "attributes" in changes:
            merged = dict(self.attributes)
            merged.update(changes["attributes"] or {})
            changes["attributes"] = merged
        return replace(self, **changes)

    def has_role(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)

    def has_scope(self, *scopes: str) -> bool:
        return any(s in self.scopes for s in scopes)

    def log_fields(self) -> dict[str, Any]:
        """The subset worth putting on every log line."""
        out: dict[str, Any] = {}
        for key in ("request_id", "trace_id", "account_id", "actor_id", "route"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


_EMPTY = RequestContext()
_ctx: contextvars.ContextVar[RequestContext] = contextvars.ContextVar(
    "inceptools_request_context", default=_EMPTY
)


def new_request_id() -> str:
    """A fresh opaque request id."""
    return uuid.uuid4().hex


def current() -> RequestContext:
    """The context for the current task. Never ``None``; empty if unset."""
    return _ctx.get()


def set_context(ctx: RequestContext) -> contextvars.Token[RequestContext]:
    """Replace the context. Reset with the returned token."""
    return _ctx.set(ctx)


def clear(token: contextvars.Token[RequestContext]) -> None:
    _ctx.reset(token)


@contextlib.contextmanager
def use(ctx: RequestContext | None = None, **changes: Any) -> Iterator[RequestContext]:
    """Install a context for the duration of the block.

    with ctxkit.use(account_id="acc_1"):
        ...
    """
    base = ctx if ctx is not None else current()
    resolved = base.with_values(**changes) if changes else base
    token = _ctx.set(resolved)
    try:
        yield resolved
    finally:
        _ctx.reset(token)


def bind(**changes: Any) -> RequestContext:
    """Merge *changes* into the current context in place, and return it.

    Used by middleware and guards that enrich identity as a request descends
    the chain — unlike :func:`use`, this is not scoped to a block.
    """
    resolved = current().with_values(**changes)
    _ctx.set(resolved)
    return resolved


# -- accessors -----------------------------------------------------------


def request_id() -> str:
    return current().request_id


def trace_id() -> str:
    return current().trace_id


def account_id() -> str | None:
    return current().account_id


def actor_id() -> str | None:
    return current().actor_id


def has_role(*roles: str) -> bool:
    return current().has_role(*roles)


def require_account() -> str:
    """The account, or an error.

    A tenanted operation with no account is a failure, never "all accounts" —
    the whole point of the scoped repository.
    """
    from inceptools.core import errs

    account = current().account_id
    if not account:
        raise errs.unauthenticated("no account on this request").with_code(NO_TENANT)
    return account


def require_actor() -> str:
    from inceptools.core import errs

    actor = current().actor_id
    if not actor:
        raise errs.unauthenticated("no actor on this request").with_code("NO_ACTOR")
    return actor


# -- background work -----------------------------------------------------


def detach() -> RequestContext:
    """The identity to carry into work that outlives the request.

    Returns a copy with the request-scoped ``route`` dropped, keeping the trace
    so the background span still joins the originating trace.
    """
    return current().with_values(route="")


def spawn(
    coro_fn: Callable[[], Awaitable[T]],
    *,
    name: str | None = None,
) -> asyncio.Task[T]:
    """Run *coro_fn* in a task that survives the caller's cancellation.

    The identity travels; the cancellation does not. The task is a real
    :class:`asyncio.Task`, so the caller may still await it — it is simply not
    cancelled when the request's task is.
    """
    ctx = detach()

    async def runner() -> T:
        token = _ctx.set(ctx)
        try:
            return await coro_fn()
        finally:
            _ctx.reset(token)

    return asyncio.get_running_loop().create_task(_shielded(runner()), name=name)


async def _shielded(awaitable: Awaitable[T]) -> T:
    return await asyncio.shield(_ensure_task(awaitable))


def _ensure_task(awaitable: Awaitable[T]) -> asyncio.Task[T]:
    if isinstance(awaitable, asyncio.Task):
        return awaitable
    return asyncio.ensure_future(awaitable)
