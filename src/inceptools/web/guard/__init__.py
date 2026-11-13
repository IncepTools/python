"""Authorization, per route, visible in the route table.

A guard answers one question: *may this caller run this handler?* It returns
``None`` to allow and raises to refuse — and the raised error's
:class:`~inceptools.core.errs.ErrorKind` picks the status, so a guard can
legitimately answer 402.

Guards are attached **per route**, never to a group. A group-level guard
applies to every route registered after it, so adding a public route under a
protected one silently exposes it; and a route table that cannot show a route's
guards cannot be tested for unguarded writes.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from inceptools.core import ctxkit, errs
from inceptools.web.execution import ExecutionContext

__all__ = [
    "Authenticated",
    "Guard",
    "GuardFunc",
    "Operator",
    "Public",
    "Tenanted",
    "all_of",
    "any_of",
    "has_role",
    "has_scope",
    "of",
    "run_guards",
]

Check = Callable[[ExecutionContext], Awaitable[None] | None]


@runtime_checkable
class Guard(Protocol):
    """Anything with a name that can refuse an execution."""

    @property
    def name(self) -> str: ...

    async def can_activate(self, ctx: ExecutionContext) -> None: ...


class GuardFunc:
    """A guard built from a function."""

    __slots__ = ("_check", "_name")

    def __init__(self, name: str, check: Check) -> None:
        self._name = name
        self._check = check

    @property
    def name(self) -> str:
        return self._name

    async def can_activate(self, ctx: ExecutionContext) -> None:
        result = self._check(ctx)
        if inspect.isawaitable(result):
            await result

    def __repr__(self) -> str:
        return f"<guard {self._name}>"


def of(name: str, check: Check) -> Guard:
    """Build a guard from a function.

    admin = guard.of("admin", lambda ctx: ctxkit.has_role("admin") or raise_())
    """
    return GuardFunc(name, check)


def all_of(name: str, *guards: Guard) -> Guard:
    """Every guard must pass. The first refusal wins."""

    async def check(ctx: ExecutionContext) -> None:
        for g in guards:
            await g.can_activate(ctx)

    return GuardFunc(name, check)


def any_of(name: str, *guards: Guard) -> Guard:
    """At least one guard must pass.

    With no guard passing, the **last** refusal is reported — it is the most
    specific thing we can say, and reporting the first would describe a path
    the caller may not even have been trying.
    """

    async def check(ctx: ExecutionContext) -> None:
        last: errs.AppError | None = None
        for g in guards:
            try:
                await g.can_activate(ctx)
                return
            except errs.AppError as exc:
                last = exc
            except Exception as exc:
                last = errs.from_exception(exc)
        raise last or errs.forbidden("not permitted").with_code("FORBIDDEN")

    return GuardFunc(name, check)


# -- built-ins -----------------------------------------------------------


def _authenticated(ctx: ExecutionContext) -> None:
    if not ctxkit.current().actor_id:
        raise errs.unauthorized("authentication required").with_code("UNAUTHENTICATED")


def _tenanted(ctx: ExecutionContext) -> None:
    # 401, not 403: there is no account on the request at all, which means we
    # do not know who is asking — not that we know and are refusing.
    if not ctxkit.current().account_id:
        raise errs.unauthorized("no account on this request").with_code(ctxkit.NO_TENANT)


def _operator(ctx: ExecutionContext) -> None:
    # Deliberately vague prose: an operator-only route should not advertise
    # itself to a caller who is not one.
    if not ctxkit.current().attributes.get("operator"):
        raise errs.forbidden("not permitted").with_code("OPERATOR_ONLY")


def _public(ctx: ExecutionContext) -> None:
    return None


#: The caller is a known user.
Authenticated: Guard = GuardFunc("authenticated", _authenticated)
#: The caller has an account, so a scoped repository can filter on it.
Tenanted: Guard = GuardFunc("tenanted", _tenanted)
#: The caller is staff. Does **not** widen tenant scope by itself.
Operator: Guard = GuardFunc("operator", _operator)
#: Allows everything. Marks a route deliberately open, rather than forgotten.
Public: Guard = GuardFunc("public", _public)


def has_role(*roles: str) -> Guard:
    """The actor holds at least one of *roles*."""
    listed = ", ".join(roles)

    def check(ctx: ExecutionContext) -> None:
        if not ctxkit.current().has_role(*roles):
            raise errs.forbidden(f"this action requires one of: {listed}").with_code(
                "ROLE_REQUIRED"
            )

    return GuardFunc(f"role:<{listed}>", check)


def has_scope(*scopes: str) -> Guard:
    """The token carries at least one of *scopes*."""
    listed = ", ".join(scopes)

    def check(ctx: ExecutionContext) -> None:
        if not ctxkit.current().has_scope(*scopes):
            raise errs.forbidden(f"this action requires one of: {listed}").with_code(
                "SCOPE_REQUIRED"
            )

    return GuardFunc(f"scope:<{listed}>", check)


async def run_guards(guards: list[Guard], ctx: ExecutionContext) -> None:
    """Run *guards* in order, stopping at the first refusal.

    Order is the author's: put the cheap checks first. The refusal is tagged
    with the guard that produced it, so a 403 in a log says which rule fired.
    """
    for g in guards:
        try:
            await g.can_activate(ctx)
        except errs.AppError as exc:
            raise (exc if "guard" in exc.meta else exc.with_meta(guard=g.name)) from None
