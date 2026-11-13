"""OpenAPI models for the envelope.

The handlers return a DTO; the client receives that DTO *inside* an envelope.
Documenting the DTO alone would describe a response shape nobody ever gets, so
every documented route advertises ``{"data": <DTO>, "meta": {...}}`` with the
DTO's real schema nested in place.
"""

from __future__ import annotations

import functools
import typing
from typing import Any

from pydantic import BaseModel, Field, create_model

__all__ = ["EnvelopeMeta", "ErrorEnvelope", "FieldErrorModel", "envelope_model"]


class FieldErrorModel(BaseModel):
    """One field-scoped validation failure."""

    field: str = Field(description="The field name as the client sent it")
    message: str
    rule: str = ""


class EnvelopeMeta(BaseModel):
    """Everything about the reply that is not the payload."""

    success: bool
    statusCode: int
    message: str
    traceId: str = ""
    requestTime: int | str = 0
    route: str = ""
    code: str = ""
    errors: list[FieldErrorModel] = Field(default_factory=list)
    page: int | None = None
    pageSize: int | None = None
    total: int | None = None
    pages: int | None = None


class ErrorEnvelope(BaseModel):
    """What a client receives for any failure."""

    data: None = None
    meta: EnvelopeMeta


def _name_of(model: Any) -> str:
    """A readable, collision-free component name.

    ``list[ProductOut]`` must not be called ``list`` — several routes would
    then share one OpenAPI component and the last one registered would win.
    """
    args = typing.get_args(model)
    if args:
        origin = typing.get_origin(model)
        prefix = getattr(origin, "__name__", "Of").capitalize()
        return "".join(_name_of(a) for a in args) + prefix
    name = getattr(model, "__name__", None)
    if name:
        return str(name)
    return str(model).replace("[", "_").replace("]", "").replace(".", "_").replace(" ", "")


@functools.lru_cache(maxsize=512)
def _build(model: Any, name: str) -> type[BaseModel] | None:
    try:
        return create_model(
            name,
            data=(model, ...),
            meta=(EnvelopeMeta, ...),
            __doc__=f"{getattr(model, '__name__', 'Response')}, enveloped.",
        )
    except Exception:
        return None


def envelope_model(model: Any) -> type[BaseModel] | None:
    """``X`` -> a model describing ``{"data": X, "meta": {...}}``.

    Cached, because FastAPI resolves this once per route at registration and a
    fresh class per call would bloat the OpenAPI component map.
    """
    if model is None:
        return None
    base = _name_of(model)
    try:
        return _build(model, f"{base}Envelope")
    except TypeError:
        # An unhashable annotation (a bare dict, say) cannot be cached.
        return _build.__wrapped__(model, f"{base}Envelope")
