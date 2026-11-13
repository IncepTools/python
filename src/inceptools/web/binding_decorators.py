"""``@UseGuards``, ``@UseInterceptors``, ``@UseFilters``, ``@UsePipes``.

Each works on a **class** or a **method**, and the two merge: a controller's
guards run before a method's own. That is the order a reader expects from the
source, and it means a controller-wide rule cannot be silently skipped by
adding a method-level one.

Every entry may be a class or an instance. A class is constructed by the
container, so a guard can take a dependency::

    @Injectable()
    class RolesGuard:
        def __init__(self, reflector: Reflector) -> None:
            self.reflector = reflector

        @property
        def name(self) -> str:
            return "roles"

        async def can_activate(self, ctx: ExecutionContext) -> None:
            needed = self.reflector.get_all_and_override(ROLES, ctx.handler, ctx.controller)
            if needed and not ctxkit.has_role(*needed):
                raise errs.forbidden("not permitted").with_code("ROLE_REQUIRED")

    @Controller("/invoices")
    @UseGuards(RolesGuard)
    class InvoiceController:
        @Post("/")
        @Roles("billing:write")
        async def create(self, body: CreateInvoice): ...
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from inceptools.core.metadata import get_metadata, set_metadata

__all__ = [
    "FILTERS",
    "GUARDS",
    "INTERCEPTORS",
    "PIPES",
    "PUBLIC",
    "ROLES",
    "SCOPES",
    "Public",
    "Roles",
    "Scopes",
    "SetMetadata",
    "UseFilters",
    "UseGuards",
    "UseInterceptors",
    "UsePipes",
    "bound",
]

T = TypeVar("T")

GUARDS = "inceptools:guards"
INTERCEPTORS = "inceptools:interceptors"
FILTERS = "inceptools:filters"
PIPES = "inceptools:pipes"
PUBLIC = "inceptools:public"
ROLES = "inceptools:roles"
SCOPES = "inceptools:scopes"


def _append(key: str) -> Callable[..., Callable[[T], T]]:
    def make(*items: Any) -> Callable[[T], T]:
        def decorate(target: T) -> T:
            # Read the target's own value, so a subclass or a method does not
            # accumulate its parent's entries twice.
            own = (
                target.__dict__.get("__inceptools_metadata__", {})
                if hasattr(target, "__dict__")
                else {}
            )
            existing = tuple(own.get(key, ()))
            set_metadata(key, (*existing, *items))(target)
            return target

        return decorate

    return make


UseGuards = _append(GUARDS)
UseGuards.__doc__ = "Attach guards to a controller or one handler."

UseInterceptors = _append(INTERCEPTORS)
UseInterceptors.__doc__ = "Attach interceptors to a controller or one handler."

UseFilters = _append(FILTERS)
UseFilters.__doc__ = "Attach exception filters to a controller or one handler."

UsePipes = _append(PIPES)
UsePipes.__doc__ = "Attach pipes, which transform and validate parameters."


def SetMetadata(key: str, value: Any) -> Callable[[T], T]:
    """Attach arbitrary metadata, for a guard to read back via the Reflector."""
    return set_metadata(key, value)


def Public() -> Callable[[T], T]:
    """Mark a handler as deliberately unauthenticated.

    A global guard reads this and stands down. Without it, a global guard has
    to special-case paths, and the special case drifts away from the routes.
    """
    return set_metadata(PUBLIC, True)


def Roles(*roles: str) -> Callable[[T], T]:
    """Declare the roles a handler needs. Read by a guard, not enforced here."""
    return set_metadata(ROLES, list(roles))


def Scopes(*scopes: str) -> Callable[[T], T]:
    return set_metadata(SCOPES, list(scopes))


def bound(key: str, handler: Any, controller: Any) -> list[Any]:
    """Everything attached under *key*, controller first then handler."""
    from_controller = tuple(get_metadata(key, controller, ()) or ())
    from_handler = tuple(get_metadata(key, handler, ()) or ())
    return [*from_controller, *from_handler]


def is_public(handler: Any, controller: Any) -> bool:
    """True when either level marked it public."""
    return bool(get_metadata(PUBLIC, handler, False) or get_metadata(PUBLIC, controller, False))
