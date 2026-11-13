"""Exception filters; nothing reaches a client unclassified.

Filters sit **outside** everything — middleware, guards, interceptors and the
handler have all unwound by the time one runs. That is what makes "every reply
has been through the envelope" true rather than aspirational.

Resolution order: your filters in order, first to handle it wins; then the
built-in mapping; then :func:`~inceptools.web.response.fail`, which redacts
anything unclassified into a generic 500.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from inceptools.core import errs
from inceptools.web import response as response_mod
from inceptools.web.binding import details_from_validation_error

__all__ = [
    "CATCHES",
    "Catch",
    "CatchFn",
    "Filter",
    "FilterFunc",
    "FilterOptions",
    "catches_of",
    "classify",
    "install",
    "not_found_handler",
    "of",
    "resolve_filters",
]

_log = logging.getLogger("inceptools.filter")

CatchFn = Callable[[Request, BaseException], Response | Awaitable[Response | None] | None]


@runtime_checkable
class Filter(Protocol):
    @property
    def name(self) -> str: ...

    async def catch(self, request: Request, exc: BaseException) -> Response | None: ...


class FilterFunc:
    __slots__ = ("_catch", "_name")

    def __init__(self, name: str, catch: CatchFn) -> None:
        self._name = name
        self._catch = catch

    @property
    def name(self) -> str:
        return self._name

    async def catch(self, request: Request, exc: BaseException) -> Response | None:
        result = self._catch(request, exc)
        if inspect.isawaitable(result):
            return await result
        return result


def of(name: str, catch: CatchFn) -> Filter:
    """Build a filter from a function. Return ``None`` to decline the error."""
    return FilterFunc(name, catch)


CATCHES = "inceptools:catches"


def Catch(*exceptions: type[BaseException]) -> Callable[[type], type]:
    """Declare which exception types a filter class handles.

        @Catch(stripe.error.CardError)
        class StripeFilter:
            async def catch(self, request, exc):
                return response_mod.fail(
                    errs.payment_required(exc.user_message).with_code("CARD_DECLINED")
                )

    With no arguments the filter sees everything, which is what you want for a
    last-resort filter and almost never otherwise.
    """

    def decorate(cls: type) -> type:
        setattr(cls, CATCHES, tuple(exceptions))
        if not hasattr(cls, "name"):
            # A filter is identified by name in logs; default it to the class
            # name so a filter author does not have to write one.
            setattr(cls, "name", property(lambda self: type(self).__name__))  # noqa: B010
        return cls

    return decorate


def catches_of(filter_obj: Any) -> tuple[type[BaseException], ...]:
    """The exception types *filter_obj* declared, or ``()`` for all of them."""
    return tuple(getattr(filter_obj, CATCHES, ()) or ())


def _handles(filter_obj: Any, exc: BaseException) -> bool:
    declared = catches_of(filter_obj)
    return not declared or isinstance(exc, declared)


async def resolve_filters(items: Sequence[Any], container: Any) -> list[Any]:
    """Build filter classes from the container, leaving instances alone."""
    resolved: list[Any] = []
    for item in items:
        if isinstance(item, type):
            resolved.append(await container.resolve(item) if container is not None else item())
        else:
            resolved.append(item)
    return resolved


@dataclass(slots=True)
class FilterOptions:
    """How failures are rendered and logged."""

    filters: list[Filter] = field(default_factory=list)
    #: 4xx are the caller's problem, and logging them turns a scanner into a
    #: log bill. Turn on while debugging a client integration.
    log_client_errors: bool = False
    options: response_mod.ResponseOptions | None = None


#: HTTP status -> the framework error it means, for errors raised by Starlette
#: itself rather than by a service.
_STATUS_MAP: dict[int, tuple[Callable[..., errs.AppError], str, str]] = {
    404: (errs.not_found, "no route matches this path", "ROUTE_NOT_FOUND"),
    405: (errs.invalid, "this method is not allowed on this path", "METHOD_NOT_ALLOWED"),
    413: (errs.invalid, "the request body is too large", "BODY_TOO_LARGE"),
    415: (errs.invalid, "unsupported content type", "UNSUPPORTED_MEDIA_TYPE"),
    429: (errs.exhausted, "too many requests", "RATE_LIMITED"),
    401: (errs.unauthorized, "authentication required", "UNAUTHENTICATED"),
    403: (errs.forbidden, "not permitted", "FORBIDDEN"),
}


def classify(exc: BaseException) -> errs.AppError:
    """Map any exception onto the framework's vocabulary."""
    if isinstance(exc, errs.AppError):
        return exc

    if isinstance(exc, RequestValidationError | ValidationError):
        return (
            errs.invalid("validation failed")
            .with_code("VALIDATION_FAILED")
            .with_details(*details_from_validation_error(exc))
            .with_cause(exc)
        )

    if isinstance(exc, ResponseValidationError):
        # The handler returned something its own contract forbids. That is our
        # bug, and the shape of it must not be described to a client.
        return errs.internal().with_code("RESPONSE_INVALID").with_cause(exc)

    if isinstance(exc, StarletteHTTPException):
        status = exc.status_code
        mapped = _STATUS_MAP.get(status)
        if mapped is not None:
            build, message, code = mapped
            return build(_detail_or(exc.detail, message)).with_code(code).with_cause(exc)
        kind = errs.ErrorKind.INTERNAL if status >= 500 else errs.ErrorKind.INVALID
        built = errs.AppError(
            kind=kind,
            message=str(exc.detail) if exc.detail else "",
            redact=kind is errs.ErrorKind.INTERNAL,
        )
        return built.with_cause(exc)

    if isinstance(exc, asyncio.CancelledError):
        # An impatient user is not an outage.
        return errs.invalid("the request was cancelled").with_code("REQUEST_CANCELLED")

    if isinstance(exc, TimeoutError):
        return errs.timeout("the request timed out").with_code("REQUEST_TIMEOUT").with_cause(exc)

    return errs.from_exception(exc)


#: Starlette fills ``detail`` with the HTTP reason phrase when no one supplied
#: one. "Not Found" says less than "no route matches this path", so a generic
#: phrase defers to the framework's message; a real one always wins.
_GENERIC_DETAILS = frozenset(
    {
        "bad request",
        "unauthorized",
        "forbidden",
        "not found",
        "method not allowed",
        "request entity too large",
        "unsupported media type",
        "too many requests",
        "internal server error",
    }
)


def _detail_or(detail: Any, fallback: str) -> str:
    if not isinstance(detail, str) or not detail.strip():
        return fallback
    return fallback if detail.strip().lower() in _GENERIC_DETAILS else detail


_QUIET_CODES = frozenset({"REQUEST_CANCELLED"})
_ALWAYS_LOGGED = frozenset({errs.ErrorKind.INTERNAL, errs.ErrorKind.UNAVAILABLE})


def _should_log(error: errs.AppError, opt: FilterOptions) -> bool:
    if error.code in _QUIET_CODES:
        return False
    return opt.log_client_errors or error.kind in _ALWAYS_LOGGED or error.status_code >= 500


def _log_failure(request: Request, error: errs.AppError) -> None:
    level = logging.ERROR if error.status_code >= 500 else logging.WARNING
    _log.log(
        level,
        "request failed",
        exc_info=error.cause or (error if error.status_code >= 500 else None),
        extra={
            "method": request.method,
            "route": request.url.path,
            "status": error.status_code,
            "code": error.code,
            "kind": error.kind.value,
            **{f"meta_{k}": v for k, v in error.meta.items()},
        },
    )


async def handle(request: Request, exc: BaseException, opt: FilterOptions) -> Response:
    """The terminal handler: custom filters, then the built-in mapping."""
    for custom in opt.filters:
        if not _handles(custom, exc):
            continue
        try:
            handled = await custom.catch(request, exc)
        except Exception as inner:
            _log.exception("exception filter %s raised", getattr(custom, "name", custom))
            exc = inner
            continue
        if handled is not None:
            return response_mod.mark_enveloped(handled)

    error = classify(exc)
    if _should_log(error, opt):
        _log_failure(request, error)
    return response_mod.fail(error, options=opt.options)


def not_found_handler(opt: FilterOptions | None = None) -> Callable[..., Any]:
    """The terminal 404. Register it after every module, never before."""
    options = opt or FilterOptions()

    async def handler(request: Request, exc: BaseException) -> Response:
        return await handle(
            request,
            errs.not_found("no route matches this path").with_code("ROUTE_NOT_FOUND"),
            options,
        )

    return handler


def install(app: FastAPI, opt: FilterOptions | None = None) -> None:
    """Route every exception class FastAPI can raise into one funnel."""
    options = opt or FilterOptions()

    async def on_error(request: Request, exc: BaseException) -> Response:
        return await handle(request, exc, options)

    for exc_type in (
        errs.AppError,
        RequestValidationError,
        ResponseValidationError,
        ValidationError,
        StarletteHTTPException,
        Exception,
    ):
        app.add_exception_handler(exc_type, on_error)
