"""The envelope, and the one place a message is scrubbed.

Every reply — success, handled failure, unhandled crash — leaves through this
module. That is the point: "did a driver error just reach a client" becomes a
question you answer once, not at four hundred call sites.

The contract::

    {"data": <payload|null>, "meta": {"success", "statusCode", "message",
                                      "traceId", "requestTime", "route",
                                      "code"?, "errors"?, page/pageSize/total/pages?}}

Only ``204`` escapes it. A second, *flat* style is provided for fleets that
already return ``{data, errors, success, statusCode, requestTime, message,
sessionId}`` and cannot change their wire contract on adoption day.
"""

from __future__ import annotations

import datetime as dt
import enum
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.responses import Response

from inceptools.core import ctxkit, errs

__all__ = [
    "Envelope",
    "EnvelopeStyle",
    "Meta",
    "ResponseOptions",
    "Sanitizer",
    "accepted",
    "bad_request",
    "conflict",
    "created",
    "default_options",
    "fail",
    "fail_envelope",
    "fail_validation",
    "forbidden",
    "internal_error",
    "is_enveloped",
    "listing",
    "mark_enveloped",
    "no_content",
    "not_found",
    "ok",
    "pages_for",
    "send",
    "service_unavailable",
    "set_default_options",
    "set_sanitizer",
    "too_many_requests",
    "unauthorized",
    "unprocessable_entity",
]

#: Rewrites an outbound message. Return the message unchanged to allow it.
#: ``scope`` is ``"http"``; ``route`` is ``"METHOD /path"``.
Sanitizer = Callable[[str, str, str], str]

_sanitizer: Sanitizer | None = None


def set_sanitizer(fn: Sanitizer | None) -> None:
    """Install the last-chance message filter. ``None`` removes it.

    Process-global and set once at boot, like the logging configuration: a
    per-request sanitizer would be a per-request chance to forget one.
    """
    global _sanitizer
    _sanitizer = fn


def _scrub(message: str, route: str) -> str:
    if _sanitizer is None or not message:
        return message
    try:
        return _sanitizer(message, "http", route)
    except Exception:
        return errs.OPAQUE_MESSAGE


class EnvelopeStyle(str, enum.Enum):
    #: ``{"data": …, "meta": {…}}`` — the framework contract.
    NESTED = "nested"
    #: ``{"data", "errors", "success", "statusCode", "requestTime", "message",
    #: "sessionId"}`` — the shape the existing services already return.
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class ResponseOptions:
    """How this service renders replies."""

    style: EnvelopeStyle = EnvelopeStyle.NESTED
    include_trace_id: bool = True
    include_route: bool = True
    include_request_time: bool = True
    success_message: str = "Success"
    #: ``requestTime`` as milliseconds served (the framework contract) rather
    #: than an ISO timestamp (what the flat style's consumers expect).
    request_time_as_millis: bool = True


_default = ResponseOptions()


def default_options() -> ResponseOptions:
    return _default


def set_default_options(options: ResponseOptions) -> None:
    """Set the process-wide style. The application factory calls this at boot."""
    global _default
    _default = options


@dataclass(slots=True)
class Meta:
    """Everything about the reply that is not the payload."""

    success: bool = True
    status_code: int = 200
    message: str = "Success"
    trace_id: str = ""
    request_time: int | str = 0
    route: str = ""
    code: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    page: int | None = None
    page_size: int | None = None
    total: int | None = None
    pages: int | None = None
    session_id: str | None = None

    def as_dict(self, options: ResponseOptions) -> dict[str, Any]:
        out: dict[str, Any] = {
            "success": self.success,
            "statusCode": self.status_code,
            "message": self.message,
        }
        if options.include_trace_id and self.trace_id:
            out["traceId"] = self.trace_id
        if options.include_request_time:
            out["requestTime"] = self.request_time
        if options.include_route and self.route:
            out["route"] = self.route
        if self.code:
            out["code"] = self.code
        if self.errors:
            out["errors"] = self.errors
        for key, value in (
            ("page", self.page),
            ("pageSize", self.page_size),
            ("total", self.total),
            ("pages", self.pages),
        ):
            if value is not None:
                out[key] = value
        if self.session_id:
            out["sessionId"] = self.session_id
        return out


@dataclass(slots=True)
class Envelope:
    """The rendered body before it becomes a response.

    Kept as a value so an interceptor can amend it and a test can assert on it
    without parsing JSON.
    """

    data: Any = None
    meta: Meta = field(default_factory=Meta)

    def as_dict(self, options: ResponseOptions | None = None) -> dict[str, Any]:
        opts = options or _default
        if opts.style is EnvelopeStyle.FLAT:
            return {
                "data": self.data,
                "errors": self.meta.errors or None,
                "success": self.meta.success,
                "statusCode": self.meta.status_code,
                "requestTime": self.meta.request_time,
                "message": self.meta.message,
                "sessionId": self.meta.session_id,
            }
        return {"data": self.data, "meta": self.meta.as_dict(opts)}

    def to_response(
        self,
        options: ResponseOptions | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        response = JSONResponse(
            status_code=self.meta.status_code,
            content=jsonable_encoder(self.as_dict(options)),
            headers=headers,
        )
        mark_enveloped(response)
        return response


def _elapsed(options: ResponseOptions) -> int | str:
    """Milliseconds served, or an ISO timestamp for the flat style."""
    if not options.request_time_as_millis:
        return dt.datetime.now(tz=dt.UTC).isoformat().replace("+00:00", "Z")
    started = ctxkit.current().attributes.get("start_time")
    if not isinstance(started, float):
        return 0
    return int((time.monotonic() - started) * 1000)


def build_meta(
    status_code: int,
    message: str,
    *,
    code: str = "",
    options: ResponseOptions | None = None,
) -> Meta:
    """Assemble the meta block from the current request's identity."""
    opts = options or _default
    ctx = ctxkit.current()
    route = ctx.route
    return Meta(
        success=status_code < 400,
        status_code=status_code,
        message=_scrub(message, route),
        trace_id=ctx.trace_id or ctx.request_id or uuid.uuid4().hex,
        request_time=_elapsed(opts),
        route=route,
        code=code,
        session_id=ctx.attributes.get("session_id"),
    )


# -- the funnel ----------------------------------------------------------


def send(
    status_code: int,
    message: str,
    data: Any = None,
    *,
    code: str = "",
    headers: dict[str, str] | None = None,
    options: ResponseOptions | None = None,
) -> JSONResponse:
    """Every successful reply goes through here."""
    opts = options or _default
    envelope = Envelope(
        data=data,
        meta=build_meta(status_code, message or opts.success_message, code=code, options=opts),
    )
    return envelope.to_response(options, headers=headers)


def ok(data: Any = None, message: str = "", **kwargs: Any) -> JSONResponse:
    """200."""
    return send(200, message or _default.success_message, data, **kwargs)


def created(data: Any = None, message: str = "Created", *, location: str = "", **kwargs: Any):
    """201, with an optional ``Location`` header."""
    headers = dict(kwargs.pop("headers", None) or {})
    if location:
        headers["Location"] = location
    return send(201, message, data, code="CREATED", headers=headers, **kwargs)


def accepted(data: Any = None, message: str = "Accepted", **kwargs: Any) -> JSONResponse:
    """202 — taken, not yet done."""
    return send(202, message, data, **kwargs)


def no_content() -> Response:
    """204. Deliberately *not* enveloped — 204 may not carry a body."""
    return Response(status_code=204)


def pages_for(total: int, limit: int) -> int:
    if limit <= 0:
        limit = 1
    return (total + limit - 1) // limit


def listing(
    data: Any,
    *,
    page: int,
    page_size: int,
    total: int,
    message: str = "",
    options: ResponseOptions | None = None,
) -> JSONResponse:
    """A page of results. Always 200, even when the page is empty."""
    opts = options or _default
    size = page_size if page_size > 0 else 1
    meta = build_meta(200, message or opts.success_message, options=opts)
    meta.page = page
    meta.page_size = size
    meta.total = total
    meta.pages = pages_for(total, size)
    return Envelope(data=data, meta=meta).to_response(opts)


# -- failures ------------------------------------------------------------


def fail_envelope(error: BaseException | None, options: ResponseOptions | None = None) -> Envelope:
    """Render any exception as the envelope.

    An error that is not an :class:`~inceptools.core.errs.AppError` becomes a
    redacted 500 — an unclassified error is by definition one nobody checked
    for leaks. The redacted message is **not** passed to the sanitizer; it is
    already the safest thing we can say.
    """
    if error is None:
        return Envelope(data=None, meta=build_meta(200, "ok", options=options))

    app_error = errs.from_exception(error)
    status = app_error.status_code
    internal = app_error.kind is errs.ErrorKind.INTERNAL

    ctx = ctxkit.current()
    opts = options or _default
    if internal or not app_error.message:
        message = errs.OPAQUE_MESSAGE
    else:
        message = _scrub(app_error.client_message, ctx.route)

    meta = Meta(
        success=False,
        status_code=status,
        message=message,
        trace_id=ctx.trace_id or ctx.request_id or uuid.uuid4().hex,
        request_time=_elapsed(opts),
        route=ctx.route,
        code=app_error.code,
        # Field detail from an internal error could name a column. Withheld.
        errors=[] if internal else [d.as_dict() for d in app_error.details],
        session_id=ctx.attributes.get("session_id"),
    )
    return Envelope(data=None, meta=meta)


def fail(
    error: BaseException | None,
    *,
    options: ResponseOptions | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """The failure funnel. ``data`` is always ``null``."""
    return fail_envelope(error, options).to_response(options, headers=headers)


def fail_validation(
    details: list[errs.FieldError], *, message: str = "validation failed"
) -> JSONResponse:
    """400 with per-field detail."""
    return fail(errs.invalid(message).with_code("VALIDATION_FAILED").with_details(*details))


def bad_request(message: str, data: Any = None, **kwargs: Any) -> JSONResponse:
    return fail(errs.invalid(message), **kwargs)


def unauthorized(message: str = "authentication required", **kwargs: Any) -> JSONResponse:
    return fail(errs.unauthorized(message), **kwargs)


def forbidden(message: str = "not permitted", **kwargs: Any) -> JSONResponse:
    return fail(errs.forbidden(message), **kwargs)


def not_found(message: str = "not found", **kwargs: Any) -> JSONResponse:
    return fail(errs.not_found(message), **kwargs)


def conflict(message: str, **kwargs: Any) -> JSONResponse:
    return fail(errs.conflict(message), **kwargs)


def unprocessable_entity(message: str, **kwargs: Any) -> JSONResponse:
    return fail(errs.unprocessable(message), **kwargs)


def too_many_requests(message: str = "too many requests", **kwargs: Any) -> JSONResponse:
    return fail(errs.exhausted(message), **kwargs)


def service_unavailable(message: str = "service unavailable", **kwargs: Any) -> JSONResponse:
    return fail(errs.unavailable(message), **kwargs)


def internal_error(**kwargs: Any) -> JSONResponse:
    """500 with the generic message. The cause belongs in the log."""
    return fail(errs.internal(), **kwargs)


# -- double-wrap protection ---------------------------------------------

_ENVELOPE_MARKER = "__inceptools_enveloped__"


def is_enveloped(response: Response) -> bool:
    """True when *response* already left through this module.

    An explicit marker, not a guess from the body's keys — a payload that
    happens to contain ``data`` is not an envelope, and wrapping it again
    corrupts the reply.
    """
    return getattr(response, _ENVELOPE_MARKER, False) is True


def mark_enveloped(response: Response) -> Response:
    setattr(response, _ENVELOPE_MARKER, True)
    return response
