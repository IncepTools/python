"""Metadata on classes and handlers, and the Reflector that reads it.

This is the mechanism behind every ``@Use*`` decorator and behind your own::

    ROLES = "roles"
    Roles = lambda *r: set_metadata(ROLES, list(r))

    @Post("/", guards=[RolesGuard])
    @Roles("admin", "billing")
    async def create(self, body: CreateInvoice): ...

    class RolesGuard:
        def __init__(self, reflector: Reflector) -> None:
            self.reflector = reflector

        async def can_activate(self, ctx: ExecutionContext) -> None:
            needed = self.reflector.get_all_and_override(ROLES, ctx.handler, ctx.controller)
            ...

Metadata is stored on the function or class object itself, so it survives
inheritance lookups and costs nothing at request time.

Two lookup strategies, and the difference matters:

``get_all_and_override``
    The most specific wins. A method's ``@Roles("admin")`` *replaces* the
    controller's. Use for a value that is a decision — one role set, one cache
    TTL.
``get_all_and_merge``
    Every level contributes. Use for a value that is a list of things to do —
    guards, interceptors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypeVar

__all__ = [
    "METADATA_ATTR",
    "Reflector",
    "all_metadata",
    "get_metadata",
    "has_metadata",
    "set_metadata",
]

T = TypeVar("T")

METADATA_ATTR = "__inceptools_metadata__"


def _store(target: Any) -> dict[str, Any]:
    """The target's own metadata dict, not an inherited one.

    ``vars(cls)`` rather than ``getattr``: a subclass must not silently write
    into its parent's dict and change the parent's behaviour.
    """
    own = target.__dict__.get(METADATA_ATTR) if hasattr(target, "__dict__") else None
    if own is None:
        own = {}
        try:
            setattr(target, METADATA_ATTR, own)
        except (AttributeError, TypeError):  # pragma: no cover - builtins
            return {}
    return own


def set_metadata(key: str, value: Any) -> Callable[[T], T]:
    """Attach *value* under *key*. The building block for custom decorators.

    Roles = lambda *names: set_metadata("roles", list(names))
    """

    def decorate(target: T) -> T:
        _store(target)[key] = value
        return target

    return decorate


def get_metadata(key: str, target: Any, default: Any = None) -> Any:
    """The value on *target*, or *default*."""
    if target is None:
        return default
    own = target.__dict__.get(METADATA_ATTR) if hasattr(target, "__dict__") else None
    if own is not None and key in own:
        return own[key]
    inherited = getattr(target, METADATA_ATTR, None)
    if isinstance(inherited, dict) and key in inherited:
        return inherited[key]
    return default


def has_metadata(key: str, target: Any) -> bool:
    sentinel = object()
    return get_metadata(key, target, sentinel) is not sentinel


def all_metadata(target: Any) -> dict[str, Any]:
    return dict(getattr(target, METADATA_ATTR, {}) or {})


class Reflector:
    """Reads metadata across the handler/controller/module chain.

    Injectable: ask for it on a guard's constructor and the container supplies
    it.
    """

    def get(self, key: str, target: Any, default: Any = None) -> Any:
        return get_metadata(key, target, default)

    def get_all(self, key: str, targets: Sequence[Any]) -> list[Any]:
        """Every value found, nearest first. Missing levels are skipped."""
        sentinel = object()
        found = [get_metadata(key, t, sentinel) for t in targets if t is not None]
        return [v for v in found if v is not sentinel]

    def get_all_and_override(self, key: str, *targets: Any, default: Any = None) -> Any:
        """The first value found, in the order given. The specific wins."""
        values = self.get_all(key, targets)
        return values[0] if values else default

    def get_all_and_merge(self, key: str, *targets: Any) -> list[Any]:
        """Every value, flattened, nearest level last.

        Reversed so an outer level's guards run before an inner level's, which
        is the order a reader expects from the source.
        """
        merged: list[Any] = []
        for value in reversed(self.get_all(key, targets)):
            if isinstance(value, Iterable) and not isinstance(value, str | bytes | dict):
                merged.extend(value)
            else:
                merged.append(value)
        return merged


#: A process-wide reflector. Stateless, so sharing one is free.
reflector = Reflector()
