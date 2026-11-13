"""One error type.

A transport-neutral *kind*, a *safe* message a client may read, and an
*internal* cause that never leaves the process.

The failure this prevents: a service that imports ``fastapi`` to raise
``HTTPException`` cannot be called from a scheduled job, a CLI, or a queue
consumer without faking a request. A service raises :class:`AppError`; the
controller, the exception filter and the response funnel decide the status.

    raise errs.conflict("this product is archived").with_code("PRODUCT_ARCHIVED")
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "OPAQUE_MESSAGE",
    "AppError",
    "ErrorKind",
    "FieldError",
    "bad_gateway",
    "code_of",
    "conflict",
    "exhausted",
    "forbidden",
    "from_exception",
    "internal",
    "invalid",
    "is_client_error",
    "is_kind",
    "kind_of",
    "not_found",
    "not_implemented",
    "not_supported",
    "payment_required",
    "precondition_failed",
    "rate_limited",
    "status_for",
    "status_for_error",
    "timeout",
    "unauthenticated",
    "unauthorized",
    "unavailable",
    "unprocessable",
    "wrap",
]


class ErrorKind(str, enum.Enum):
    """What went wrong, independent of any transport.

    The eleven canonical kinds are the framework's shared vocabulary. The three
    that follow them are Python-specific additions: ``UNPROCESSABLE`` because
    FastAPI and Pydantic already mean 422 by it, and the other two because they
    were being spelled as an opaque 500 otherwise.
    """

    INVALID = "invalid"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    EXHAUSTED = "exhausted"
    PAYMENT = "payment"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    NOT_SUPPORTED = "not_supported"
    INTERNAL = "internal"

    UNPROCESSABLE = "unprocessable"
    PRECONDITION_FAILED = "precondition_failed"
    BAD_GATEWAY = "bad_gateway"


_STATUS: dict[ErrorKind, int] = {
    ErrorKind.INVALID: 400,
    ErrorKind.UNAUTHORIZED: 401,
    ErrorKind.PAYMENT: 402,
    ErrorKind.FORBIDDEN: 403,
    ErrorKind.NOT_FOUND: 404,
    ErrorKind.CONFLICT: 409,
    ErrorKind.PRECONDITION_FAILED: 412,
    ErrorKind.UNPROCESSABLE: 422,
    ErrorKind.EXHAUSTED: 429,
    ErrorKind.INTERNAL: 500,
    ErrorKind.NOT_SUPPORTED: 501,
    ErrorKind.BAD_GATEWAY: 502,
    ErrorKind.UNAVAILABLE: 503,
    ErrorKind.TIMEOUT: 504,
}

_DEFAULT_CODE: dict[ErrorKind, str] = {
    ErrorKind.INVALID: "BAD_REQUEST",
    ErrorKind.UNAUTHORIZED: "UNAUTHENTICATED",
    ErrorKind.PAYMENT: "PAYMENT_REQUIRED",
    ErrorKind.FORBIDDEN: "FORBIDDEN",
    ErrorKind.NOT_FOUND: "NOT_FOUND",
    ErrorKind.CONFLICT: "CONFLICT",
    ErrorKind.UNPROCESSABLE: "UNPROCESSABLE_ENTITY",
    ErrorKind.PRECONDITION_FAILED: "PRECONDITION_FAILED",
    ErrorKind.EXHAUSTED: "RATE_LIMITED",
    ErrorKind.TIMEOUT: "TIMEOUT",
    ErrorKind.UNAVAILABLE: "UNAVAILABLE",
    ErrorKind.BAD_GATEWAY: "BAD_GATEWAY",
    ErrorKind.NOT_SUPPORTED: "NOT_SUPPORTED",
    ErrorKind.INTERNAL: "INTERNAL_ERROR",
}

#: What a client is told when the error was never classified for disclosure.
OPAQUE_MESSAGE = "an internal error occurred"


def status_for(kind: ErrorKind) -> int:
    """The HTTP status a *kind* maps to."""
    return _STATUS.get(kind, 500)


@dataclass(frozen=True, slots=True)
class FieldError:
    """One field-scoped validation failure.

    ``field`` is the name **the client sent** (the serialization alias), so the
    message points at something they can actually find in their payload.
    """

    field: str
    message: str
    rule: str = ""

    def as_dict(self) -> dict[str, str]:
        out = {"field": self.field, "message": self.message}
        if self.rule:
            out["rule"] = self.rule
        return out


@dataclass
class AppError(Exception):
    """A classified failure.

    Only :attr:`message` is ever shown to a client. :attr:`cause` is the
    original exception and goes to the log alone.
    """

    kind: ErrorKind = ErrorKind.INTERNAL
    message: str = OPAQUE_MESSAGE
    code: str = ""
    details: list[FieldError] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    cause: BaseException | None = None
    #: False when the message is a safe, deliberate disclosure. Internal errors
    #: start redacted and the funnel substitutes :data:`OPAQUE_MESSAGE`.
    redact: bool = False

    def __post_init__(self) -> None:
        if not self.code:
            self.code = _DEFAULT_CODE.get(self.kind, "INTERNAL_ERROR")
        Exception.__init__(self, self.message)

    # -- presentation ----------------------------------------------------

    @property
    def status_code(self) -> int:
        """The HTTP status for this error."""
        return status_for(self.kind)

    @property
    def client_message(self) -> str:
        """The message a client may see."""
        return OPAQUE_MESSAGE if self.redact else self.message

    def is_client_error(self) -> bool:
        return 400 <= self.status_code < 500

    # -- fluent builders -------------------------------------------------
    # Each returns a new error; an AppError is cheap and treating it as
    # immutable keeps a shared sentinel from being mutated by one caller.

    def _copy(self, **changes: Any) -> AppError:
        data: dict[str, Any] = {
            "kind": self.kind,
            "message": self.message,
            "code": self.code,
            "details": list(self.details),
            "meta": dict(self.meta),
            "cause": self.cause,
            "redact": self.redact,
        }
        data.update(changes)
        return AppError(**data)

    def with_code(self, code: str) -> AppError:
        """Set the stable, machine-readable code clients branch on."""
        return self._copy(code=code)

    def with_message(self, message: str) -> AppError:
        return self._copy(message=message, redact=False)

    def with_cause(self, cause: BaseException | None) -> AppError:
        """Attach the underlying exception. It is logged, never returned."""
        return self._copy(cause=cause)

    def with_details(self, *details: FieldError) -> AppError:
        return self._copy(details=[*self.details, *details])

    def with_meta(self, **meta: Any) -> AppError:
        merged = dict(self.meta)
        merged.update(meta)
        return self._copy(meta=merged)

    def with_kind(self, kind: ErrorKind) -> AppError:
        return self._copy(kind=kind)

    def as_dict(self) -> dict[str, Any]:
        """The client-safe projection. Never includes :attr:`cause`."""
        out: dict[str, Any] = {
            "code": self.code,
            "message": self.client_message,
            "statusCode": self.status_code,
        }
        if self.details:
            out["errors"] = [d.as_dict() for d in self.details]
        if self.meta:
            out["meta"] = self.meta
        return out

    def __str__(self) -> str:
        base = f"{self.code}: {self.message}"
        return f"{base} (cause: {self.cause!r})" if self.cause is not None else base


# -- constructors --------------------------------------------------------


def _make(kind: ErrorKind, message: str, *, code: str = "", redact: bool = False) -> AppError:
    return AppError(kind=kind, message=message, code=code, redact=redact)


def invalid(message: str, *, code: str = "") -> AppError:
    """400 — the request itself is malformed."""
    return _make(ErrorKind.INVALID, message, code=code)


def unauthorized(message: str = "authentication required", *, code: str = "") -> AppError:
    """401 — we do not know who you are."""
    return _make(ErrorKind.UNAUTHORIZED, message, code=code)


#: Readability alias. 401 is "unauthenticated" in plain English and
#: ``unauthorized`` in HTTP; both names build the same error.
unauthenticated = unauthorized


def forbidden(message: str = "you may not do that", *, code: str = "") -> AppError:
    """403 — we know who you are and the answer is still no."""
    return _make(ErrorKind.FORBIDDEN, message, code=code)


def not_found(message: str = "not found", *, code: str = "") -> AppError:
    """404 — no such resource, or none you are allowed to see."""
    return _make(ErrorKind.NOT_FOUND, message, code=code)


def conflict(message: str, *, code: str = "") -> AppError:
    """409 — the request is well-formed but the current state refuses it."""
    return _make(ErrorKind.CONFLICT, message, code=code)


def unprocessable(message: str, *, code: str = "") -> AppError:
    """422 — understood, well-formed, and semantically wrong."""
    return _make(ErrorKind.UNPROCESSABLE, message, code=code)


def precondition_failed(message: str, *, code: str = "") -> AppError:
    """412 — an ``If-Match``-style precondition did not hold."""
    return _make(ErrorKind.PRECONDITION_FAILED, message, code=code)


def exhausted(message: str = "too many requests", *, code: str = "") -> AppError:
    """429 — a budget ran out: rate limit, quota, seats."""
    return _make(ErrorKind.EXHAUSTED, message, code=code)


#: Alias for the most common cause of :func:`exhausted`.
rate_limited = exhausted


def payment_required(message: str = "payment required", *, code: str = "") -> AppError:
    """402 — the plan does not cover this."""
    return _make(ErrorKind.PAYMENT, message, code=code)


def timeout(message: str = "the request timed out", *, code: str = "") -> AppError:
    """504 — we gave up waiting on something downstream."""
    return _make(ErrorKind.TIMEOUT, message, code=code)


def unavailable(message: str = "service unavailable", *, code: str = "") -> AppError:
    """503 — a dependency is down or we are shutting down."""
    return _make(ErrorKind.UNAVAILABLE, message, code=code)


def bad_gateway(message: str = "upstream error", *, code: str = "") -> AppError:
    """502 — an upstream answered, and the answer was unusable."""
    return _make(ErrorKind.BAD_GATEWAY, message, code=code)


def not_supported(message: str = "not supported", *, code: str = "") -> AppError:
    """501 — this operation does not exist here, and will not later."""
    return _make(ErrorKind.NOT_SUPPORTED, message, code=code)


#: Alias matching Python's own ``NotImplementedError``.
not_implemented = not_supported


def internal(message: str = OPAQUE_MESSAGE, *, code: str = "") -> AppError:
    """500 — redacted by default.

    Call :meth:`AppError.with_message` only when the text is a deliberate,
    reviewed disclosure.
    """
    return _make(ErrorKind.INTERNAL, message, code=code, redact=True)


def wrap(exc: BaseException, message: str = "", *, kind: ErrorKind | None = None) -> AppError:
    """Classify an arbitrary exception, keeping it as the cause."""
    if isinstance(exc, AppError):
        return exc if not message else exc.with_message(message)
    resolved = kind or ErrorKind.INTERNAL
    err = AppError(
        kind=resolved,
        message=message or OPAQUE_MESSAGE,
        redact=resolved is ErrorKind.INTERNAL and not message,
    )
    return err.with_cause(exc)


def from_exception(exc: BaseException) -> AppError:
    """Coerce *anything* into an ``AppError``.

    An exception that is not already classified becomes a redacted 500, because
    an unclassified error is by definition one nobody checked for leaks.
    """
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, TimeoutError):
        return timeout().with_cause(exc)
    if isinstance(exc, PermissionError):
        return forbidden().with_cause(exc)
    if isinstance(exc, NotImplementedError):
        return not_supported().with_cause(exc)
    return wrap(exc)


def is_client_error(exc: BaseException) -> bool:
    """True when *exc* maps to a 4xx — i.e. the caller's problem, not ours."""
    return from_exception(exc).is_client_error()


def kind_of(exc: BaseException | None) -> ErrorKind | None:
    """The kind of *exc*. ``None`` stays ``None``; anything unclassified is internal."""
    if exc is None:
        return None
    return exc.kind if isinstance(exc, AppError) else ErrorKind.INTERNAL


def is_kind(exc: BaseException | None, kind: ErrorKind) -> bool:
    return kind_of(exc) is kind


def code_of(exc: BaseException | None) -> str:
    return "" if exc is None else from_exception(exc).code


def status_for_error(exc: BaseException | None) -> int:
    """The status *exc* will be rendered with. ``None`` means the request succeeded.

    Instrumentation must use this rather than reading the response status: the
    exception handler runs after the middleware chain unwinds, so a failed
    request still reads as 200 on the way out.
    """
    return 200 if exc is None else from_exception(exc).status_code
