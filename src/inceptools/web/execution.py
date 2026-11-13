"""What a guard, an interceptor and a filter are handed.

One type for every execution boundary, so the same guard protects an HTTP
route, a queue consumer and a scheduled job. An HTTP request is only the most
common kind of execution — the one where :attr:`ExecutionContext.request` is
set.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from starlette.requests import Request

if TYPE_CHECKING:  # pragma: no cover
    from inceptools.core.di import Container
    from inceptools.web.router import Route

__all__ = ["ExecutionContext", "Kind"]


class Kind:
    HTTP = "http"
    JOB = "job"
    QUEUE = "queue"
    EVENT = "event"


@dataclass(slots=True)
class ExecutionContext:
    """The request, the route it matched, and the container serving it."""

    kind: str = Kind.HTTP
    request: Request | None = None
    route: Route | None = None
    #: ``"<app>.<module>"``, e.g. ``"catalog.products"``.
    module: str = ""
    full_path: str = ""
    handler: Callable[..., Any] | None = None
    #: The controller class, for reading class-level metadata.
    controller: type | None = None
    container: Container | None = None
    #: Scratch space for a guard to hand something to an interceptor.
    state: dict[str, Any] = field(default_factory=dict)

    # -- request conveniences, each safe when there is no request ---------

    @property
    def method(self) -> str:
        return self.request.method if self.request else ""

    @property
    def path(self) -> str:
        return self.request.url.path if self.request else self.full_path

    def header(self, name: str, default: str = "") -> str:
        if self.request is None:
            return default
        return self.request.headers.get(name, default)

    def query(self, name: str, default: str = "") -> str:
        if self.request is None:
            return default
        return self.request.query_params.get(name, default)

    def path_param(self, name: str, default: Any = None) -> Any:
        if self.request is None:
            return default
        return self.request.path_params.get(name, default)

    @property
    def client_ip(self) -> str:
        """The peer address.

        Deliberately **not** read from ``X-Forwarded-For``: that header is
        caller-supplied, and trusting it by default turns a rate limit into a
        suggestion. A deployment behind a known proxy installs
        ``ProxyHeadersMiddleware`` explicitly.
        """
        if self.request is None or self.request.client is None:
            return ""
        return self.request.client.host

    def describe(self) -> str:
        return f"{self.method} {self.path}".strip()
