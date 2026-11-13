"""Typed extraction from a request, that cannot silently yield a zero value.

``int(request.query_params.get("limit", 0))`` has three failure modes and
reports none of them. Every function here either returns the value you asked
for or raises a 400 naming the parameter and the rule it broke.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from starlette.requests import Request

from inceptools.core import errs

__all__ = [
    "bool_query",
    "csv_query",
    "datetime_query",
    "enum_query",
    "int_param",
    "int_query",
    "string_param",
    "uuid_param",
    "uuid_query",
]

_TRUE = frozenset({"1", "t", "true", "yes", "y", "on"})
_FALSE = frozenset({"0", "f", "false", "no", "n", "off"})


def _fail(name: str, message: str, rule: str) -> errs.AppError:
    return (
        errs.invalid(message)
        .with_code("INVALID_PARAMETER")
        .with_details(errs.FieldError(field=name, message=message, rule=rule))
    )


def _required(request: Request, name: str) -> str:
    raw = request.path_params.get(name)
    value = "" if raw is None else str(raw).strip()
    if not value:
        raise _fail(name, f"{name} is required", "required")
    return value


def string_param(request: Request, name: str) -> str:
    """A required, trimmed path parameter."""
    return _required(request, name)


def uuid_param(request: Request, name: str) -> uuid.UUID:
    """A required path parameter that must be a UUID."""
    value = _required(request, name)
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise _fail(name, f"{name} must be a valid UUID", "uuid") from exc


def int_param(request: Request, name: str) -> int:
    """A required path parameter that must be a whole number."""
    value = _required(request, name)
    try:
        return int(value)
    except ValueError as exc:
        raise _fail(name, f"{name} must be a whole number", "numeric") from exc


def uuid_query(request: Request, name: str) -> uuid.UUID | None:
    """An **optional** UUID query parameter. Absent means ``None``, not an error."""
    value = (request.query_params.get(name) or "").strip()
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise _fail(name, f"{name} must be a valid UUID", "uuid") from exc


def int_query(request: Request, name: str, default: int = 0) -> int:
    value = (request.query_params.get(name) or "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise _fail(name, f"{name} must be a whole number", "numeric") from exc


def bool_query(request: Request, name: str, default: bool = False) -> bool:
    value = (request.query_params.get(name) or "").strip().lower()
    if not value:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise _fail(name, f"{name} must be true or false", "boolean")


def enum_query(request: Request, name: str, *allowed: str) -> str:
    """One of *allowed*, or ``""`` when absent."""
    value = (request.query_params.get(name) or "").strip()
    if not value:
        return ""
    if value not in allowed:
        raise _fail(name, f"{name} must be one of: {', '.join(allowed)}", "oneof")
    return value


def datetime_query(request: Request, name: str) -> dt.datetime | None:
    """An RFC 3339 timestamp, or ``None`` when absent."""
    value = (request.query_params.get(name) or "").strip()
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail(name, f"{name} must be an RFC 3339 timestamp", "datetime") from exc


def csv_query(request: Request, name: str) -> list[str]:
    """A comma-separated list, trimmed, empties dropped. Never fails."""
    raw = request.query_params.get(name) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _unused(*_: Any) -> None:  # pragma: no cover
    return None
