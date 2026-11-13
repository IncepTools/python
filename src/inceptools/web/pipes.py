"""Pipes: transform and validate a parameter before the handler sees it.

FastAPI and Pydantic already parse and validate types, so a pipe here is for
what they do not do — normalising a value, enforcing a rule that is not a
type, or applying the same transform to every parameter of a handler.

Two ways to attach one:

    @Post("/")
    @UsePipes(Trim())                                  # every parameter
    async def create(self, body: CreateProduct): ...

    async def show(self, pid: Annotated[int, Piped(Clamp(1, 1000))]): ...  # one

A pipe's failure is an ``AppError``, so it renders as a field-scoped 400 like
any other validation failure.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from inceptools.core import errs

__all__ = [
    "DefaultValue",
    "ParseBool",
    "ParseEnum",
    "ParseFloat",
    "ParseInt",
    "ParseList",
    "ParseUUID",
    "Pipe",
    "PipeFunc",
    "Piped",
    "Trim",
    "ValueMeta",
    "apply_pipes",
    "pipe",
]


@dataclass(frozen=True, slots=True)
class ValueMeta:
    """What a pipe is told about the value it was handed."""

    name: str
    annotation: Any = None
    #: ``"body"``, ``"query"``, ``"path"``, ``"header"`` or ``""`` when unknown.
    source: str = ""


@runtime_checkable
class Pipe(Protocol):
    def transform(self, value: Any, meta: ValueMeta) -> Any: ...


class PipeFunc:
    """A pipe built from a function."""

    __slots__ = ("_fn", "_name")

    def __init__(self, name: str, fn: Callable[[Any, ValueMeta], Any]) -> None:
        self._name = name
        self._fn = fn

    def transform(self, value: Any, meta: ValueMeta) -> Any:
        return self._fn(value, meta)

    def __repr__(self) -> str:
        return f"<pipe {self._name}>"


def pipe(name: str, fn: Callable[[Any, ValueMeta], Any]) -> Pipe:
    """Build a pipe from a function."""
    return PipeFunc(name, fn)


@dataclass(frozen=True, slots=True)
class Piped:
    """Annotate one parameter with pipes: ``Annotated[int, Piped(Clamp(1, 100))]``."""

    pipes: tuple[Pipe, ...]

    def __init__(self, *pipes: Pipe) -> None:
        object.__setattr__(self, "pipes", tuple(pipes))


def _fail(meta: ValueMeta, message: str, rule: str) -> errs.AppError:
    return (
        errs.invalid(message)
        .with_code("INVALID_PARAMETER")
        .with_details(errs.FieldError(field=meta.name, message=message, rule=rule))
    )


# -- built-ins -----------------------------------------------------------


class ParseInt:
    """A whole number, or a 400 naming the parameter."""

    def __init__(self, *, minimum: int | None = None, maximum: int | None = None) -> None:
        self.minimum = minimum
        self.maximum = maximum

    def transform(self, value: Any, meta: ValueMeta) -> int | None:
        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise _fail(meta, f"{meta.name} must be a whole number", "numeric") from exc
        if self.minimum is not None and parsed < self.minimum:
            raise _fail(meta, f"{meta.name} must be {self.minimum} or more", "gte")
        if self.maximum is not None and parsed > self.maximum:
            raise _fail(meta, f"{meta.name} must be {self.maximum} or less", "lte")
        return parsed


class ParseFloat:
    def transform(self, value: Any, meta: ValueMeta) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise _fail(meta, f"{meta.name} must be a number", "numeric") from exc


_TRUE = frozenset({"1", "t", "true", "yes", "y", "on"})
_FALSE = frozenset({"0", "f", "false", "no", "n", "off"})


class ParseBool:
    def transform(self, value: Any, meta: ValueMeta) -> bool | None:
        if value is None or isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise _fail(meta, f"{meta.name} must be true or false", "boolean")


class ParseUUID:
    def transform(self, value: Any, meta: ValueMeta) -> uuid.UUID | None:
        if value is None or isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise _fail(meta, f"{meta.name} must be a valid UUID", "uuid") from exc


class ParseEnum:
    """One of *allowed*, compared case-sensitively."""

    def __init__(self, *allowed: str) -> None:
        self.allowed = allowed

    def transform(self, value: Any, meta: ValueMeta) -> Any:
        if value is None:
            return None
        if isinstance(value, enum.Enum):
            return value
        if str(value) not in self.allowed:
            raise _fail(meta, f"{meta.name} must be one of: {', '.join(self.allowed)}", "oneof")
        return value


class ParseList:
    """A comma-separated string into a list, each item through *item*."""

    def __init__(self, item: Pipe | None = None, *, separator: str = ",") -> None:
        self.item = item
        self.separator = separator

    def transform(self, value: Any, meta: ValueMeta) -> list[Any] | None:
        if value is None:
            return None
        parts = (
            list(value)
            if isinstance(value, list | tuple)
            else [p.strip() for p in str(value).split(self.separator) if p.strip()]
        )
        if self.item is None:
            return parts
        return [self.item.transform(p, meta) for p in parts]


class DefaultValue:
    """Substitute *default* when the value is absent."""

    def __init__(self, default: Any) -> None:
        self.default = default

    def transform(self, value: Any, meta: ValueMeta) -> Any:
        return self.default if value is None else value


class Trim:
    """Strip whitespace from strings, recursively through models and lists.

    The failure this prevents: ``" ada@example.com "`` passing an email rule
    and then not matching the row it was supposed to find.
    """

    def __init__(self, *, empty_to_none: bool = False) -> None:
        self.empty_to_none = empty_to_none

    def transform(self, value: Any, meta: ValueMeta) -> Any:
        return self._walk(value)

    def _walk(self, value: Any) -> Any:
        if isinstance(value, str):
            trimmed = value.strip()
            return None if self.empty_to_none and not trimmed else trimmed
        if isinstance(value, list):
            return [self._walk(v) for v in value]
        if isinstance(value, dict):
            return {k: self._walk(v) for k, v in value.items()}
        if isinstance(value, BaseModel):
            # `type(value).model_fields`, never `value.model_fields`: reading
            # it from the instance is deprecated in Pydantic and raises under
            # a warnings-as-errors test run.
            for name in type(value).model_fields:
                current = getattr(value, name, None)
                walked = self._walk(current)
                if walked is not current:
                    object.__setattr__(value, name, walked)
            return value
        return value


def apply_pipes(value: Any, pipes: Sequence[Pipe], meta: ValueMeta) -> Any:
    """Run *value* through *pipes* in order."""
    for item in pipes:
        value = item.transform(value, meta)
    return value
