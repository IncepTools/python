"""Bind-and-validate with field-scoped errors.

A validation failure names the field **the client sent** — ``category_id``, not
``CategoryID``; ``rules[0].protocol``, not ``body -> rules -> 0 -> protocol`` —
because a message pointing at something the caller cannot find in their payload
is a message that costs a support ticket.

A DTO may implement ``normalize()``, ``set_defaults()`` and ``validate_self()``.
They run in that order, so a default can satisfy ``required`` and a cross-field
rule can assume the individual fields are already well-formed.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError
from starlette.requests import Request

from inceptools.core import errs

__all__ = [
    "Defaulter",
    "Normalizer",
    "SelfValidator",
    "body",
    "details_from_validation_error",
    "header",
    "params",
    "query",
    "run_dto_hooks",
    "validate",
]

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class Normalizer(Protocol):
    """Runs first: trim, lowercase, canonicalise."""

    def normalize(self) -> None: ...


@runtime_checkable
class Defaulter(Protocol):
    """Runs after :class:`Normalizer`, before cross-field rules."""

    def set_defaults(self) -> None: ...


@runtime_checkable
class SelfValidator(Protocol):
    """Runs last: cross-field rules, which may assume each field is well-formed."""

    def validate_self(self) -> None: ...


# -- message vocabulary --------------------------------------------------

#: Pydantic error type -> (rule name, message template).
#: ``{field}`` is the client-facing field name; ``{limit}`` the constraint.
_MESSAGES: dict[str, tuple[str, str]] = {
    "missing": ("required", "{field} is required"),
    "none_required": ("required", "{field} must be null"),
    "value_error": ("invalid", "{field} is invalid"),
    "assertion_error": ("invalid", "{field} is invalid"),
    "string_type": ("string", "{field} must be text"),
    "string_too_short": ("min", "{field} must be at least {limit} characters"),
    "string_too_long": ("max", "{field} must be at most {limit} characters"),
    "string_pattern_mismatch": ("pattern", "{field} is not in the expected format"),
    "int_type": ("numeric", "{field} must be a whole number"),
    "int_parsing": ("numeric", "{field} must be a whole number"),
    "float_type": ("numeric", "{field} must be a number"),
    "float_parsing": ("numeric", "{field} must be a number"),
    "decimal_parsing": ("numeric", "{field} must be a number"),
    "bool_type": ("boolean", "{field} must be true or false"),
    "bool_parsing": ("boolean", "{field} must be true or false"),
    "greater_than": ("gt", "{field} must be greater than {limit}"),
    "greater_than_equal": ("gte", "{field} must be {limit} or more"),
    "less_than": ("lt", "{field} must be less than {limit}"),
    "less_than_equal": ("lte", "{field} must be {limit} or less"),
    "too_short": ("min", "{field} must have at least {limit} entries"),
    "too_long": ("max", "{field} must have at most {limit} entries"),
    "enum": ("oneof", "{field} must be one of: {limit}"),
    "literal_error": ("oneof", "{field} must be one of: {limit}"),
    "uuid_parsing": ("uuid", "{field} must be a valid UUID"),
    "uuid_type": ("uuid", "{field} must be a valid UUID"),
    "url_parsing": ("url", "{field} must be a valid URL"),
    "url_type": ("url", "{field} must be a valid URL"),
    "datetime_parsing": ("datetime", "{field} must be an RFC 3339 timestamp"),
    "datetime_type": ("datetime", "{field} must be an RFC 3339 timestamp"),
    "date_parsing": ("date", "{field} must be a date (YYYY-MM-DD)"),
    "date_type": ("date", "{field} must be a date (YYYY-MM-DD)"),
    "time_parsing": ("time", "{field} must be a time (HH:MM:SS)"),
    "json_invalid": ("json", "{field} must be valid JSON"),
    "list_type": ("list", "{field} must be a list"),
    "dict_type": ("object", "{field} must be an object"),
    "model_type": ("object", "{field} must be an object"),
    "model_attributes_type": ("object", "{field} must be an object"),
    "extra_forbidden": ("extra", "{field} is not a recognised field"),
    "email": ("email", "{field} must be a valid email address"),
    "value_error.email": ("email", "{field} must be a valid email address"),
}

#: Location prefixes pydantic adds that the client never sent.
_DROPPED_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})


def _field_name(loc: tuple[Any, ...], *, drop_root: bool = True) -> str:
    """``("body", "rules", 0, "protocol")`` -> ``rules[0].protocol``."""
    parts = list(loc)
    if drop_root and parts and parts[0] in _DROPPED_LOCATIONS:
        parts = parts[1:]
    if not parts:
        return "body"

    rendered = ""
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif rendered:
            rendered += f".{part}"
        else:
            rendered = str(part)
    return rendered or "body"


def _limit_of(error: Any) -> str:
    ctx = error.get("ctx") or {}
    for key in (
        "min_length",
        "max_length",
        "gt",
        "ge",
        "lt",
        "le",
        "limit_value",
        "expected",
        "pattern",
    ):
        if key in ctx:
            value = ctx[key]
            if key == "expected" and isinstance(value, str):
                # pydantic renders literals as "'a' or 'b'"; make it a list.
                return value.replace("'", "").replace(" or ", ", ")
            return _render_limit(value)
    return ""


def _render_limit(value: Any) -> str:
    """``0.0`` -> ``"0"``.

    The constraint's type follows the field's, so ``ge=0`` on a float reads
    back as ``0.0``. "price must be 0.0 or more" is a message written by a
    machine; "0 or more" is one written for a person.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def details_from_validation_error(
    error: ValidationError | Any, *, drop_root: bool = True
) -> list[errs.FieldError]:
    """Turn a pydantic/FastAPI validation error into client-facing details."""
    raw = error.errors() if hasattr(error, "errors") else []
    details: list[errs.FieldError] = []

    for item in raw:
        kind = str(item.get("type", ""))
        field = _field_name(tuple(item.get("loc", ())), drop_root=drop_root)
        rule, template = _MESSAGES.get(kind, ("invalid", "{field} is invalid"))

        if kind in ("value_error", "assertion_error"):
            # A custom validator's own message is better than a generic one.
            message = str(item.get("msg", "")).removeprefix("Value error, ").strip()
            message = message or template.format(field=field, limit="")
        else:
            message = template.format(field=field, limit=_limit_of(item))

        details.append(errs.FieldError(field=field, message=message, rule=rule))

    if not details:
        details.append(
            errs.FieldError(field="body", message="the request is not valid", rule="invalid")
        )
    return details


def run_dto_hooks(dto: Any) -> Any:
    """``normalize()`` -> ``set_defaults()`` -> ``validate_self()``."""
    if isinstance(dto, Normalizer):
        dto.normalize()
    if isinstance(dto, Defaulter):
        dto.set_defaults()
    if isinstance(dto, SelfValidator):
        try:
            dto.validate_self()
        except errs.AppError:
            raise
        except ValidationError as exc:
            raise _validation_failed(details_from_validation_error(exc)) from exc
        except (ValueError, AssertionError) as exc:
            raise _validation_failed(
                [errs.FieldError(field="body", message=str(exc), rule="rule")]
            ) from exc
    return dto


def _validation_failed(details: list[errs.FieldError]) -> errs.AppError:
    return errs.invalid("validation failed").with_code("VALIDATION_FAILED").with_details(*details)


def validate(model: type[T], data: Any, *, code: str = "VALIDATION_FAILED") -> T:
    """Build *model* from *data*, running the DTO hooks.

    Raises an :class:`~inceptools.core.errs.AppError` carrying one detail per
    bad field — never a raw :class:`pydantic.ValidationError`, which would
    reach a client as an unclassified 500.
    """
    try:
        dto = model.model_validate(data)
    except ValidationError as exc:
        raise _validation_failed(details_from_validation_error(exc)).with_code(code) from exc
    return run_dto_hooks(dto)


# -- request binders -----------------------------------------------------


async def body(request: Request, model: type[T]) -> T:
    """Bind and validate the JSON body."""
    try:
        raw = await request.body()
        payload = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise (
            errs.invalid("the request body could not be read")
            .with_code("MALFORMED_BODY")
            .with_details(
                errs.FieldError(field="body", message="body is not valid JSON", rule="parse")
            )
        ) from exc
    return validate(model, payload)


def query(request: Request, model: type[T]) -> T:
    """Bind and validate the query string."""
    return _bind_mapping(dict(request.query_params), model, "MALFORMED_QUERY", "query")


def params(request: Request, model: type[T]) -> T:
    """Bind and validate the path parameters."""
    return _bind_mapping(dict(request.path_params), model, "MALFORMED_PARAMS", "path")


def header(request: Request, model: type[T]) -> T:
    """Bind and validate headers (matched case-insensitively)."""
    data = {k.lower().replace("-", "_"): v for k, v in request.headers.items()}
    return _bind_mapping(data, model, "MALFORMED_HEADERS", "header")


def _bind_mapping(data: dict[str, Any], model: type[T], code: str, where: str) -> T:
    try:
        dto = model.model_validate(data)
    except ValidationError as exc:
        details = details_from_validation_error(exc, drop_root=False)
        raise (
            errs.invalid(f"the request {where} is not valid").with_code(code).with_details(*details)
        ) from exc
    return run_dto_hooks(dto)
