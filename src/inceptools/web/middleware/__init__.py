"""Request id, recover, timeout, CORS, security headers, rate limit.

Every middleware here is **pure ASGI**, not a ``BaseHTTPMiddleware`` subclass.
That is a correctness decision, not a performance one: ``BaseHTTPMiddleware``
runs the rest of the application in a separate task, and a
:mod:`contextvars` value set in such a middleware is therefore invisible to the
endpoint. Request identity set that way would be silently absent from every
log line and every scoped query.

The chain order is fixed, and it is correctness rather than taste:

1. :class:`RequestContextMiddleware` — first, so everything below logs the id.
2. :class:`RecoverMiddleware` — second, so a panic anywhere below carries it.
3. :class:`SecureHeadersMiddleware`
4. :class:`AccessLogMiddleware`
5. :class:`IdentityMiddleware` — **before** the timeout, so an auth failure is
   not reported as a timeout.
6. :class:`TimeoutMiddleware`
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from inceptools.core import ctxkit, errs
from inceptools.core import logx as logx_mod
from inceptools.web import response as response_mod

__all__ = [
    "HEADER_REQUEST_ID",
    "HEADER_TRACEPARENT",
    "HEADER_TRACE_ID",
    "AccessLogMiddleware",
    "AccessLogOptions",
    "CORSOptions",
    "ChainOptions",
    "IdentityMiddleware",
    "IdentityResolver",
    "MemoryRateLimitStore",
    "RateLimitMiddleware",
    "RateLimitOptions",
    "RateLimitStore",
    "RecoverMiddleware",
    "RequestContextMiddleware",
    "SecureHeadersMiddleware",
    "SecureHeadersOptions",
    "TimeoutMiddleware",
    "build_chain",
]

_log = logging.getLogger("inceptools.http")

HEADER_REQUEST_ID = "x-request-id"
HEADER_TRACE_ID = "x-trace-id"
HEADER_TRACEPARENT = "traceparent"

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16


def _trace_from_traceparent(value: str) -> str:
    """The trace id from a W3C ``traceparent``, or ``""`` if it is not valid.

    An all-zero trace or span id is explicitly invalid in the spec, and
    accepting one would group every such request into a single useless trace.
    """
    match = _TRACEPARENT.match(value.strip().lower())
    if match is None:
        return ""
    trace, span = match.group(1), match.group(2)
    if trace == _ZERO_TRACE or span == _ZERO_SPAN:
        return ""
    return trace


async def _send_error(send: Send, response: Any, scope: Scope) -> None:
    await response(scope, _empty_receive, send)


async def _empty_receive() -> Message:  # pragma: no cover - never awaited in practice
    return {"type": "http.disconnect"}


class RequestContextMiddleware:
    """Install request identity, and echo it back.

    An inbound ``X-Request-Id`` is trusted (it is how a caller correlates its
    own logs with ours) but regenerated when absent or implausibly long, so a
    client cannot push 8KB through every log line.
    """

    MAX_ID_LENGTH = 200

    def __init__(self, app: ASGIApp, *, trust_inbound_id: bool = True) -> None:
        self.app = app
        self.trust_inbound_id = trust_inbound_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        inbound = headers.get(HEADER_REQUEST_ID, "") if self.trust_inbound_id else ""
        request_id = inbound if inbound and len(inbound) <= self.MAX_ID_LENGTH else uuid.uuid4().hex
        trace_id = (
            _trace_from_traceparent(headers.get(HEADER_TRACEPARENT, ""))
            or headers.get(HEADER_TRACE_ID, "")
            or request_id
        )

        ctx = ctxkit.RequestContext(
            request_id=request_id,
            trace_id=trace_id,
            route=scope.get("path", ""),
            attributes={"start_time": time.monotonic()},
        )
        token = ctxkit.set_context(ctx)

        async def send_with_ids(message: Message) -> None:
            if message["type"] == "http.response.start":
                mutable = MutableHeaders(scope=message)
                mutable.setdefault(HEADER_REQUEST_ID, request_id)
                mutable.setdefault(HEADER_TRACE_ID, trace_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_ids)
        finally:
            ctxkit.clear(token)


class RecoverMiddleware:
    """Turn an unhandled exception into the envelope rather than a bare 500.

    FastAPI's own handler covers routed requests; this covers everything
    before routing — a middleware below it, a malformed scope, a path that
    never matched.
    """

    def __init__(self, app: ASGIApp, *, options: Any = None) -> None:
        self.app = app
        self.options = options

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except errs.AppError:
            raise
        except Exception as exc:
            _log.exception(
                "unhandled exception",
                extra={"method": scope.get("method", ""), "route": scope.get("path", "")},
            )
            if started:
                # The response is already on the wire; there is nothing honest
                # left to send, and writing a second status would corrupt it.
                raise
            await _send_error(
                send, response_mod.fail(errs.from_exception(exc), options=self.options), scope
            )


class TimeoutMiddleware:
    """Refuse to hold a connection open forever.

    Answers 504 rather than letting a client wait on a handler that is never
    going to finish.
    """

    def __init__(self, app: ASGIApp, *, seconds: float, options: Any = None) -> None:
        self.app = app
        self.seconds = seconds
        self.options = options

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.seconds <= 0:
            await self.app(scope, receive, send)
            return

        started = False

        async def watched_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await asyncio.wait_for(self.app(scope, receive, watched_send), self.seconds)
        except TimeoutError:
            if started:
                raise
            error = (
                errs.timeout("the request took too long")
                .with_code("REQUEST_TIMEOUT")
                .with_meta(timeoutSeconds=self.seconds)
            )
            _log.warning(
                "request timed out",
                extra={
                    "method": scope.get("method", ""),
                    "route": scope.get("path", ""),
                    "timeout_s": self.seconds,
                },
            )
            await _send_error(send, response_mod.fail(error, options=self.options), scope)


@dataclass(slots=True)
class AccessLogOptions:
    """What the access log says, and about which requests."""

    #: Exact paths never logged — probes, mostly.
    skip_paths: tuple[str, ...] = ("/livez", "/readyz", "/healthz", "/metrics")
    slow_threshold: float = 1.0
    #: Off by default: one line per successful request is the reverse proxy's
    #: job, and duplicating it buries the failures.
    log_success: bool = False


class AccessLogMiddleware:
    """Log failures, slow requests, and — if asked — everything."""

    def __init__(self, app: ASGIApp, *, options: AccessLogOptions | None = None) -> None:
        self.app = app
        self.options = options or AccessLogOptions()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in self.options.skip_paths:
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        status = 500

        async def recording_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        failure: BaseException | None = None
        try:
            await self.app(scope, receive, recording_send)
        except BaseException as exc:
            failure = exc
            status = errs.status_for_error(exc) if isinstance(exc, Exception) else 500
            raise
        finally:
            elapsed = time.monotonic() - started
            slow = elapsed >= self.options.slow_threshold
            if self.options.log_success or status >= 400 or failure is not None or slow:
                if status >= 500 or failure is not None:
                    level = logging.ERROR
                elif status >= 400 or slow:
                    level = logging.WARNING
                else:
                    level = logging.INFO
                client = scope.get("client")
                _log.log(
                    level,
                    "request",
                    extra={
                        "method": scope.get("method", ""),
                        "route": scope.get("path", ""),
                        "status": status,
                        "ms": int(elapsed * 1000),
                        "ip": client[0] if client else "",
                    },
                )


#: Resolve the caller's identity from the request. Raise to refuse outright.
IdentityResolver = Callable[[Request], Any]


class IdentityMiddleware:
    """Put *who is asking* on the context.

    It never authorizes — that is a guard's job, and keeping them separate is
    what lets one route be public and the next one not.
    """

    def __init__(self, app: ASGIApp, *, resolve: IdentityResolver, options: Any = None) -> None:
        self.app = app
        self.resolve = resolve
        self.options = options

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            resolved = self.resolve(Request(scope, receive))
            if inspect.isawaitable(resolved):
                resolved = await resolved
        except errs.AppError as exc:
            await _send_error(send, response_mod.fail(exc, options=self.options), scope)
            return
        except Exception as exc:
            _log.exception("identity resolution failed")
            await _send_error(
                send,
                response_mod.fail(
                    errs.unauthorized("could not establish who you are")
                    .with_code("IDENTITY_FAILED")
                    .with_cause(exc),
                    options=self.options,
                ),
                scope,
            )
            return

        if resolved:
            ctxkit.bind(**dict(resolved))
        await self.app(scope, receive, send)


@dataclass(slots=True)
class SecureHeadersOptions:
    hsts_max_age: int = 0
    content_security_policy: str = "default-src 'none'; frame-ancestors 'none'"
    frame_options: str = "DENY"
    referrer_policy: str = "no-referrer"
    permissions_policy: str = "geolocation=(), microphone=(), camera=()"


class SecureHeadersMiddleware:
    """The headers a browser needs to refuse to do something stupid."""

    def __init__(self, app: ASGIApp, *, options: SecureHeadersOptions | None = None) -> None:
        self.app = app
        self.options = options or SecureHeadersOptions()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        opt = self.options

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("x-frame-options", opt.frame_options)
                headers.setdefault("referrer-policy", opt.referrer_policy)
                headers.setdefault("content-security-policy", opt.content_security_policy)
                headers.setdefault("cross-origin-opener-policy", "same-origin")
                headers.setdefault("cross-origin-resource-policy", "same-origin")
                headers.setdefault("permissions-policy", opt.permissions_policy)
                if opt.hsts_max_age > 0:
                    headers.setdefault(
                        "strict-transport-security",
                        f"max-age={opt.hsts_max_age}; includeSubDomains",
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


@dataclass(slots=True)
class CORSOptions:
    """Which browsers may call this service, and with what.

    A wildcard origin is honoured **only** when credentials are off — the
    combination is forbidden by the spec and silently ignored by browsers,
    which makes it look configured when it is not.
    """

    allow_origins: tuple[str, ...] = ()
    allow_origin_regex: str = ""
    allow_methods: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
    allow_headers: tuple[str, ...] = (
        "Origin",
        "Content-Type",
        "Accept",
        "Authorization",
        "X-Request-Id",
        "X-Idempotency-Key",
    )
    expose_headers: tuple[str, ...] = ("X-Request-Id", "X-Trace-Id")
    allow_credentials: bool = False
    max_age: int = 600


# -- rate limiting -------------------------------------------------------


class RateLimitStore:
    """Counts requests in a window."""

    async def incr(self, key: str, window: float) -> tuple[int, float]:
        """Return ``(count_in_window, seconds_until_reset)``."""
        raise NotImplementedError


class MemoryRateLimitStore(RateLimitStore):
    """A fixed window, per process.

    N replicas therefore allow N x the limit. That is a deliberate trade for a
    default with no dependencies; a real limit uses a shared store.
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}
        self._lock = asyncio.Lock()
        self._last_sweep = time.monotonic()

    async def incr(self, key: str, window: float) -> tuple[int, float]:
        now = time.monotonic()
        async with self._lock:
            if now - self._last_sweep > window:
                self._counts = {k: v for k, v in self._counts.items() if v[1] > now}
                self._last_sweep = now

            count, reset_at = self._counts.get(key, (0, 0.0))
            if reset_at <= now:
                count, reset_at = 0, now + window
            count += 1
            self._counts[key] = (count, reset_at)
            return count, max(reset_at - now, 0.0)


@dataclass(slots=True)
class RateLimitOptions:
    max_requests: int = 0
    window: float = 60.0
    key_func: Callable[[Request], str] | None = None
    skip: Callable[[Request], bool] | None = None
    store: RateLimitStore | None = None
    #: On a store failure, let the request through. A broken limiter should not
    #: become an outage — but a limiter protecting something expensive should
    #: set this to False deliberately.
    fail_open: bool = True


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, *, options: RateLimitOptions, response_options: Any = None):
        self.app = app
        self.options = options
        self.store = options.store or MemoryRateLimitStore()
        self.response_options = response_options

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        opt = self.options
        if scope["type"] != "http" or opt.max_requests <= 0 or opt.window <= 0:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        if opt.skip is not None and opt.skip(request):
            await self.app(scope, receive, send)
            return

        key = opt.key_func(request) if opt.key_func else _client_key(request)

        try:
            count, reset_in = await self.store.incr(key, opt.window)
        except Exception as exc:
            _log.warning("rate limit store failed", exc_info=exc)
            if opt.fail_open:
                await self.app(scope, receive, send)
                return
            await _send_error(
                send,
                response_mod.fail(
                    errs.unavailable("rate limiting is unavailable").with_code(
                        "RATE_LIMIT_UNAVAILABLE"
                    ),
                    options=self.response_options,
                ),
                scope,
            )
            return

        retry_after = max(int(reset_in + 0.999), 1)
        if count > opt.max_requests:
            error = (
                errs.exhausted(f"too many requests; retry after {retry_after}s")
                .with_code("RATE_LIMITED")
                .with_meta(retryAfter=retry_after)
            )
            await _send_error(
                send,
                response_mod.fail(
                    error,
                    options=self.response_options,
                    headers={
                        "Retry-After": str(retry_after),
                        "RateLimit-Limit": str(opt.max_requests),
                        "RateLimit-Remaining": "0",
                        "RateLimit-Reset": str(retry_after),
                    },
                ),
                scope,
            )
            return

        remaining = max(opt.max_requests - count, 0)

        async def send_with_budget(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("ratelimit-limit", str(opt.max_requests))
                headers.setdefault("ratelimit-remaining", str(remaining))
                headers.setdefault("ratelimit-reset", str(retry_after))
            await send(message)

        await self.app(scope, receive, send_with_budget)


def _client_key(request: Request) -> str:
    """The peer address.

    Deliberately not ``X-Forwarded-For``: that header is caller-supplied, and
    trusting it by default turns a rate limit into a suggestion.
    """
    return request.client.host if request.client else "unknown"


@dataclass(slots=True)
class ChainOptions:
    """The standard chain, in the order described at the top of this module."""

    timeout: float = 0.0
    access_log: AccessLogOptions = field(default_factory=AccessLogOptions)
    secure_headers: SecureHeadersOptions | None = None
    identity: IdentityResolver | None = None
    trust_inbound_request_id: bool = True


def build_chain(opt: ChainOptions, *, response_options: Any = None) -> list[tuple[type, dict]]:
    """The middleware classes and their arguments, outermost first."""
    chain: list[tuple[type, dict[str, Any]]] = [
        (RequestContextMiddleware, {"trust_inbound_id": opt.trust_inbound_request_id}),
        (RecoverMiddleware, {"options": response_options}),
    ]
    if opt.secure_headers is not None:
        chain.append((SecureHeadersMiddleware, {"options": opt.secure_headers}))
    chain.append((AccessLogMiddleware, {"options": opt.access_log}))
    if opt.identity is not None:
        chain.append((IdentityMiddleware, {"resolve": opt.identity, "options": response_options}))
    if opt.timeout > 0:
        chain.append((TimeoutMiddleware, {"seconds": opt.timeout, "options": response_options}))
    return chain


def _unused(*_: Any) -> None:  # pragma: no cover
    _ = (logx_mod, Sequence)
