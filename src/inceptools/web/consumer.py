"""Path-scoped middleware, declared by the module that needs it.

ASGI middleware is global: added to the app, it sees every request. That is
wrong for anything that belongs to one area — a legacy-header shim, a
tenant-subdomain parser, a body-size cap on uploads only.

A module declares what it wants and where::

    @Module(controllers=[UploadController])
    class UploadsModule:
        def configure(self, consumer: MiddlewareConsumer) -> None:
            consumer.apply(BodyLimitMiddleware, max_bytes=50_000_000) \\
                    .for_routes("/api/v1/uploads/*") \\
                    .exclude("/api/v1/uploads/status")

Patterns are glob-style: ``*`` matches within a segment, ``**`` across
segments, and a pattern with no wildcard must match the path exactly.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["MiddlewareBinding", "MiddlewareConsumer", "PathScoped", "compile_pattern"]


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """``/api/v1/uploads/*`` -> a regex anchored at both ends."""
    text = pattern.strip() or "/"
    if not text.startswith("/"):
        text = "/" + text

    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "*":
            if text.startswith("**", index):
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


@dataclass(slots=True)
class MiddlewareBinding:
    """One middleware and the paths it applies to."""

    middleware: Any
    kwargs: dict[str, Any] = field(default_factory=dict)
    includes: list[re.Pattern[str]] = field(default_factory=list)
    excludes: list[re.Pattern[str]] = field(default_factory=list)
    #: For diagnostics and the route table.
    source: str = ""

    def matches(self, path: str) -> bool:
        if any(pattern.match(path) for pattern in self.excludes):
            return False
        if not self.includes:
            return True
        return any(pattern.match(path) for pattern in self.includes)

    # -- fluent builders -------------------------------------------------

    def for_routes(self, *patterns: str) -> MiddlewareBinding:
        """Limit it to these paths. With none given, it applies everywhere."""
        self.includes.extend(compile_pattern(p) for p in patterns)
        return self

    def exclude(self, *patterns: str) -> MiddlewareBinding:
        """Skip these paths. Checked before the includes."""
        self.excludes.extend(compile_pattern(p) for p in patterns)
        return self

    def __repr__(self) -> str:
        name = getattr(self.middleware, "__name__", type(self.middleware).__name__)
        return f"<middleware {name} from {self.source or 'app'}>"


class MiddlewareConsumer:
    """What a module's ``configure`` is handed."""

    def __init__(self, source: str = "") -> None:
        self.source = source
        self.bindings: list[MiddlewareBinding] = []

    def apply(self, middleware: Any, **kwargs: Any) -> MiddlewareBinding:
        """Declare a middleware. Chain ``.for_routes()`` and ``.exclude()``."""
        binding = MiddlewareBinding(middleware=middleware, kwargs=kwargs, source=self.source)
        self.bindings.append(binding)
        return binding

    def __len__(self) -> int:
        return len(self.bindings)


class PathScoped:
    """Runs *binding*'s middleware only for the paths it claimed."""

    def __init__(self, app: ASGIApp, *, binding: MiddlewareBinding) -> None:
        self.app = app
        self.binding = binding
        # Built once: constructing the inner middleware per request would
        # rebuild whatever state it holds on every call.
        self.wrapped: ASGIApp = (
            binding.middleware(app, **binding.kwargs)
            if isinstance(binding.middleware, type)
            else binding.middleware
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.binding.matches(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        await self.wrapped(scope, receive, send)


def collect(modules: Sequence[Any]) -> list[MiddlewareBinding]:
    """Run every module's ``configure`` and gather what it asked for."""
    bindings: list[MiddlewareBinding] = []
    for item in modules:
        target = item.instance if getattr(item, "instance", None) is not None else None
        configure = getattr(target, "configure", None) or getattr(
            item.descriptor.cls, "configure", None
        )
        if configure is None:
            continue
        consumer = MiddlewareConsumer(source=item.qualified_name)
        if target is not None:
            configure(consumer)
        else:
            # An unconstructible module class: call it unbound.
            configure(item.descriptor.cls, consumer)
        bindings.extend(consumer.bindings)
    return bindings
