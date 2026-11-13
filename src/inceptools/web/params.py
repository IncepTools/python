"""Custom parameter decorators.

A parameter decorator is a FastAPI dependency underneath, so these compose
with ``Depends``, appear in OpenAPI, and work in a plain FastAPI route too.

The value they add over reading the request in every handler: the extraction
is written once and named, so "where does the account come from" has one
answer instead of one per endpoint.

    CurrentAccount = create_param_decorator(lambda ctx: ctxkit.require_account())

    @Get("/me")
    async def me(self, account: str = CurrentAccount()): ...
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from fastapi import Body, Cookie, Depends, File, Form, Header, Path, Query
from starlette.requests import Request

from inceptools.core import ctxkit
from inceptools.web.execution import ExecutionContext, Kind

__all__ = [
    "Body",
    "Cookie",
    "CurrentAccount",
    "CurrentActor",
    "Depends",
    "File",
    "Form",
    "Header",
    "Ip",
    "Path",
    "Query",
    "Req",
    "RequestContext",
    "RequestId",
    "Roles",
    "TraceId",
    "UserAgent",
    "create_param_decorator",
]


def create_param_decorator(
    extract: Callable[..., Any],
) -> Callable[..., Any]:
    """Turn an extractor into a parameter decorator.

    *extract* takes an :class:`ExecutionContext`, and may be sync or async. The
    returned callable is used as a parameter default::

        TenantId = create_param_decorator(lambda ctx: ctx.header("x-tenant"))

        async def index(self, tenant: str = TenantId()): ...
    """

    def decorator(*args: Any, **kwargs: Any) -> Any:
        async def dependency(request: Request) -> Any:
            ctx = ExecutionContext(kind=Kind.HTTP, request=request)
            result = extract(ctx, *args, **kwargs) if (args or kwargs) else extract(ctx)
            if inspect.isawaitable(result):
                result = await result
            return result

        return Depends(dependency)

    return decorator


# -- built-ins -----------------------------------------------------------

#: The raw request. Prefer a typed parameter; this is for the awkward cases.
Req = create_param_decorator(lambda ctx: ctx.request)

#: The whole request context — trace, account, actor, roles.
RequestContext = create_param_decorator(lambda ctx: ctxkit.current())

#: The account, or 401 ``NO_TENANT``. The usual way a handler learns its tenant.
CurrentAccount = create_param_decorator(lambda ctx: ctxkit.require_account())

#: The actor, or 401. ``CurrentActor()`` for required, ``ctxkit.actor_id()``
#: directly when absence is acceptable.
CurrentActor = create_param_decorator(lambda ctx: ctxkit.require_actor())

Roles = create_param_decorator(lambda ctx: ctxkit.current().roles)
TraceId = create_param_decorator(lambda ctx: ctxkit.trace_id())
RequestId = create_param_decorator(lambda ctx: ctxkit.request_id())

#: The peer address. Deliberately not ``X-Forwarded-For`` — see
#: :mod:`inceptools.web.middleware`.
Ip = create_param_decorator(lambda ctx: ctx.client_ip)

UserAgent = create_param_decorator(lambda ctx: ctx.header("user-agent"))
