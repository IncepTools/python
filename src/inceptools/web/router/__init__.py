"""Routes as data, plus the registry that makes them assertable.

A controller *declares* its routes; mounting is what turns them into handlers.
Because a route is a value rather than a side effect, the same declaration
mounts it, prints it, documents it — and lets a test assert that every mutating
route has a guard::

    def test_every_write_is_guarded(app):
        assert app.routes.unguarded() == []

That is the cheapest security control in the framework. The failure it catches
— a new write endpoint shipped without authentication — is both easy to commit
and expensive to find.

Controllers are real classes with constructor-injected dependencies, so a
handler is an instance method that can reach its service without a global.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import typing
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from fastapi import APIRouter
from fastapi import params as fastapi_params
from starlette.requests import Request
from starlette.responses import Response

from inceptools.core import errs
from inceptools.web import response as response_mod
from inceptools.web.binding_decorators import (
    FILTERS,
    GUARDS,
    INTERCEPTORS,
    PIPES,
    bound,
    is_public,
)
from inceptools.web.execution import ExecutionContext, Kind
from inceptools.web.guard import Guard, run_guards
from inceptools.web.interceptor import Interceptor, run_interceptors
from inceptools.web.pipes import Piped, ValueMeta, apply_pipes

__all__ = [
    "ROUTE_TEMPLATE_KEY",
    "Controller",
    "Delete",
    "Doc",
    "Get",
    "Head",
    "MountOptions",
    "Mounted",
    "Options",
    "ParamDoc",
    "Patch",
    "Post",
    "Put",
    "Registry",
    "Route",
    "controller_routes",
    "delete",
    "get",
    "head",
    "is_controller",
    "mount_controller",
    "mount_routes",
    "options",
    "patch",
    "post",
    "put",
    "resolve_bindings",
    "route",
]

F = TypeVar("F", bound=Callable[..., Any])

#: Methods that change state, and therefore must be guarded or marked public.
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_log = logging.getLogger("inceptools.router")

_ROUTE_ATTR = "__inceptools_route__"
_ROUTES_ATTR = "__inceptools_routes__"
_CONTROLLER_ATTR = "__inceptools_controller__"


@dataclass(frozen=True, slots=True)
class ParamDoc:
    name: str
    location: str = "query"  # path | query | header
    description: str = ""
    required: bool = False
    example: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "in": self.location}
        if self.description:
            out["description"] = self.description
        if self.required:
            out["required"] = True
        if self.example:
            out["example"] = self.example
        return out


@dataclass(frozen=True, slots=True)
class Doc:
    """What the route says about itself — to OpenAPI, and to a reader."""

    operation_id: str = ""
    summary: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    params: tuple[ParamDoc, ...] = ()
    request_body: Any = None
    response: Any = None
    statuses: tuple[int, ...] = ()
    deprecated: bool = False
    include_in_schema: bool = True


@dataclass(frozen=True)
class Route:
    """One route, as a value.

    Every builder returns a copy, so a route can be shared, listed and amended
    without one caller's ``.guard()`` leaking into another's.
    """

    method: str
    path: str
    handler: Callable[..., Any] | None = None
    handler_name: str = ""
    guards: tuple[Guard, ...] = ()
    interceptors: tuple[Interceptor, ...] = ()
    #: FastAPI ``Depends`` applied before guards — the escape hatch.
    dependencies: tuple[Any, ...] = ()
    status_code: int | None = None
    #: Deliberately open. A mutating route is either guarded or public; there
    #: is no third state, because the third state is "nobody checked".
    public: bool = False
    doc: Doc = field(default_factory=Doc)
    #: Skip the envelope and return the handler's value untouched.
    raw: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "path", _normalise(self.path))

    # -- fluent builders -------------------------------------------------

    def guard(self, *guards: Guard) -> Route:
        return replace(self, guards=(*self.guards, *guards))

    def intercept(self, *interceptors: Interceptor) -> Route:
        return replace(self, interceptors=(*self.interceptors, *interceptors))

    def depends(self, *dependencies: Any) -> Route:
        return replace(self, dependencies=(*self.dependencies, *dependencies))

    def id(self, operation_id: str) -> Route:
        return replace(self, doc=replace(self.doc, operation_id=operation_id))

    def summary(self, text: str) -> Route:
        return replace(self, doc=replace(self.doc, summary=text))

    def describe(self, text: str) -> Route:
        return replace(self, doc=replace(self.doc, description=text))

    def tag(self, *tags: str) -> Route:
        return replace(self, doc=replace(self.doc, tags=(*self.doc.tags, *tags)))

    def param(self, name: str, description: str = "", *, example: str = "") -> Route:
        doc = ParamDoc(name, "path", description, required=True, example=example)
        return replace(self, doc=replace(self.doc, params=(*self.doc.params, doc)))

    def query_param(
        self, name: str, description: str = "", *, required: bool = False, example: str = ""
    ) -> Route:
        doc = ParamDoc(name, "query", description, required=required, example=example)
        return replace(self, doc=replace(self.doc, params=(*self.doc.params, doc)))

    def header_param(self, name: str, description: str = "", *, required: bool = False) -> Route:
        doc = ParamDoc(name, "header", description, required=required)
        return replace(self, doc=replace(self.doc, params=(*self.doc.params, doc)))

    def accepts(self, model: Any) -> Route:
        return replace(self, doc=replace(self.doc, request_body=model))

    def returns(self, model: Any, *statuses: int) -> Route:
        return replace(
            self,
            doc=replace(self.doc, response=model, statuses=(*self.doc.statuses, *statuses)),
            status_code=self.status_code or (statuses[0] if statuses else None),
        )

    def deprecate(self) -> Route:
        return replace(self, doc=replace(self.doc, deprecated=True))

    def as_public(self) -> Route:
        """Mark a mutating route as deliberately unauthenticated."""
        return replace(self, public=True)

    def hidden(self) -> Route:
        return replace(self, doc=replace(self.doc, include_in_schema=False))

    def as_raw(self) -> Route:
        """Return the handler's value as-is: a file download, a redirect, SSE."""
        return replace(self, raw=True)

    # -- introspection ---------------------------------------------------

    @property
    def guard_names(self) -> list[str]:
        return [g.name for g in self.guards]

    @property
    def is_mutating(self) -> bool:
        return self.method in MUTATING

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


def _normalise(path: str) -> str:
    if not path or path == "/":
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


# -- construction --------------------------------------------------------


def route(
    method: str, path: str, handler: Callable[..., Any] | None = None, **kwargs: Any
) -> Route:
    return Route(method=method, path=path, handler=handler, **kwargs)


def _verb(method: str) -> Callable[..., Any]:
    def build(path: str = "/", handler: Callable[..., Any] | None = None, **kwargs: Any) -> Route:
        return Route(method=method, path=path, handler=handler, **kwargs)

    build.__name__ = method.lower()
    build.__doc__ = f"A ``{method}`` route value."
    return build


get = _verb("GET")
post = _verb("POST")
put = _verb("PUT")
patch = _verb("PATCH")
delete = _verb("DELETE")
head = _verb("HEAD")
options = _verb("OPTIONS")


def _decorator(method: str) -> Callable[..., Callable[[F], F]]:
    """Build the ``@Get(...)`` style decorator for one HTTP method."""

    def make(
        path: str = "/",
        *,
        guards: Sequence[Guard] = (),
        interceptors: Sequence[Interceptor] = (),
        dependencies: Sequence[Any] = (),
        status_code: int | None = None,
        public: bool = False,
        raw: bool = False,
        summary: str = "",
        description: str = "",
        operation_id: str = "",
        tags: Sequence[str] = (),
        response_model: Any = None,
        deprecated: bool = False,
        include_in_schema: bool = True,
    ) -> Callable[[F], F]:
        def decorate(fn: F) -> F:
            built = Route(
                method=method,
                path=path,
                handler=fn,
                handler_name=fn.__name__,
                guards=tuple(guards),
                interceptors=tuple(interceptors),
                dependencies=tuple(dependencies),
                status_code=status_code,
                public=public,
                raw=raw,
                doc=Doc(
                    operation_id=operation_id,
                    summary=summary or _first_line(fn.__doc__),
                    description=description,
                    tags=tuple(tags),
                    response=response_model,
                    deprecated=deprecated,
                    include_in_schema=include_in_schema,
                ),
            )
            setattr(fn, _ROUTE_ATTR, built)
            return fn

        return decorate

    make.__name__ = method.capitalize()
    make.__doc__ = f"Declare a ``{method}`` route on a controller method."
    return make


Get = _decorator("GET")
Post = _decorator("POST")
Put = _decorator("PUT")
Patch = _decorator("PATCH")
Delete = _decorator("DELETE")
Head = _decorator("HEAD")
Options = _decorator("OPTIONS")


def _first_line(doc: str | None) -> str:
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def Controller(
    prefix: str = "",
    *,
    tags: Sequence[str] = (),
    guards: Sequence[Guard] = (),
    interceptors: Sequence[Interceptor] = (),
    dependencies: Sequence[Any] = (),
) -> Callable[[type], type]:
    """Mark a class as a controller and collect its declared routes.

    ``guards`` here apply to every route the controller declares — unlike a
    router-level dependency, they stay visible per route in the route table.
    """

    def decorate(cls: type) -> type:
        collected: list[Route] = []
        for name, member in inspect.getmembers(cls, callable):
            declared = getattr(member, _ROUTE_ATTR, None)
            if declared is None:
                continue
            built = replace(
                declared,
                handler_name=name,
                path=_normalise(_join(prefix, declared.path)),
                guards=(*guards, *declared.guards),
                interceptors=(*interceptors, *declared.interceptors),
                dependencies=(*dependencies, *declared.dependencies),
                doc=replace(declared.doc, tags=(*tags, *declared.doc.tags)),
            )
            collected.append(built)

        collected.sort(key=lambda r: (r.path, r.method))
        setattr(cls, _ROUTES_ATTR, tuple(collected))
        setattr(cls, _CONTROLLER_ATTR, {"prefix": _normalise(prefix) if prefix else ""})
        return cls

    return decorate


def is_controller(obj: Any) -> bool:
    cls = obj if isinstance(obj, type) else type(obj)
    return hasattr(cls, _ROUTES_ATTR) or hasattr(cls, "routes")


def controller_routes(controller: Any) -> list[Route]:
    """The routes a controller declares, by decorator or by ``routes()``."""
    declared = list(getattr(controller, _ROUTES_ATTR, ()) or ())
    method = getattr(controller, "routes", None)
    if callable(method) and not isinstance(controller, type):
        produced = method()
        if produced:
            declared = [*declared, *produced]
    return declared


def _join(prefix: str, path: str) -> str:
    left = (prefix or "").rstrip("/")
    right = path or "/"
    if right == "/":
        return left or "/"
    if not right.startswith("/"):
        right = "/" + right
    return (left + right) or "/"


# -- the registry --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mounted:
    """A route, once it knows where it lives."""

    route: Route
    full_path: str
    module: str

    @property
    def method(self) -> str:
        return self.route.method

    @property
    def guard_names(self) -> list[str]:
        return self.route.guard_names

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.route.method,
            "path": self.route.path,
            "full_path": self.full_path,
            "module": self.module,
            "guards": self.guard_names,
            "public": self.route.public,
        }


class Registry:
    """Every mounted route, in a form a test and the CLI can both read."""

    def __init__(self) -> None:
        self._mounted: list[Mounted] = []

    def add(self, mounted: Mounted) -> None:
        self._mounted.append(mounted)

    def __len__(self) -> int:
        return len(self._mounted)

    def __iter__(self) -> Any:
        return iter(self.all())

    def all(self) -> list[Mounted]:
        """Sorted by path then method, so two runs produce the same list."""
        return sorted(self._mounted, key=lambda m: (m.full_path, m.route.method))

    def unguarded(self) -> list[Mounted]:
        """Mutating routes with no guard that were not marked public.

        Assert this is empty in CI and a write endpoint cannot ship without
        somebody having made an explicit decision about who may call it.
        """
        return [
            m
            for m in self.all()
            if m.route.is_mutating and not m.route.guards and not m.route.public
        ]

    def for_module(self, module: str) -> list[Mounted]:
        return [m for m in self.all() if m.module == module]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [m.as_dict() for m in self.all()]

    def table(self) -> str:
        """An aligned, diffable route table."""
        rows = self.all()
        if not rows:
            return "no routes mounted"
        headers = ("METHOD", "PATH", "MODULE", "GUARDS")
        data = [
            (
                m.route.method,
                m.full_path,
                m.module or "-",
                ", ".join(m.guard_names) or ("(public)" if m.route.public else "-"),
            )
            for m in rows
        ]
        widths = [
            max(len(headers[i]), max(len(row[i]) for row in data)) for i in range(len(headers))
        ]
        lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
        lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
        for row in data:
            lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(row))).rstrip())
        return "\n".join(lines)


# -- mounting ------------------------------------------------------------


@dataclass(slots=True)
class MountOptions:
    module: str = ""
    prefix: str = ""
    registry: Registry | None = None
    #: Applied to every route, before the controller's and the route's own.
    guards: tuple[Guard, ...] = ()
    interceptors: tuple[Interceptor, ...] = ()
    pipes: tuple[Any, ...] = ()
    container: Any = None
    options: response_mod.ResponseOptions | None = None
    #: The controller class, so the Reflector can read class-level metadata.
    controller: type | None = None


async def resolve_bindings(items: Sequence[Any], container: Any) -> list[Any]:
    """Build any classes in *items* from the container; pass instances through.

    A guard, interceptor or pipe may be a class with dependencies of its own,
    which is the only way a ``RolesGuard`` can reach a ``Reflector``.
    """
    resolved: list[Any] = []
    for item in items:
        if isinstance(item, type):
            if container is None:
                resolved.append(item())
            else:
                resolved.append(await container.resolve(item))
        else:
            resolved.append(item)
    return resolved


async def mount_controller(router: APIRouter, controller: Any, opt: MountOptions) -> list[Mounted]:
    """Mount every route a controller declares."""
    if isinstance(controller, type):
        raise errs.internal(
            f"{controller.__name__} was mounted as a class; "
            "construct it through the container so its dependencies are injected"
        )
    if opt.controller is None:
        opt.controller = type(controller)
    return await mount_routes(router, controller_routes(controller), opt, instance=controller)


async def mount_routes(
    router: APIRouter,
    routes: Iterable[Route],
    opt: MountOptions,
    *,
    instance: Any = None,
) -> list[Mounted]:
    """Register *routes* on a FastAPI router, preserving their metadata.

    Asynchronous because a guard, interceptor or pipe may be a class with
    dependencies, and the only honest time to construct it is now — at boot,
    where a missing provider fails the deploy.
    """
    mounted: list[Mounted] = []

    for item in routes:
        handler = _resolve_handler(item, instance)
        if handler is None:
            raise errs.internal(
                f"router: {item.method} {item.path} in module {opt.module!r} has no handler"
            )
        if not item.method:
            raise errs.internal(f"router: route {item.path} in module {opt.module!r} has no method")

        controller_cls = opt.controller or (type(instance) if instance is not None else None)
        per_request = bool(
            controller_cls is not None
            and opt.container is not None
            and opt.container.needs_request_scope(controller_cls)
        )
        handler_fn = getattr(controller_cls, item.handler_name, None) if controller_cls else None

        # Global, then controller-level @UseGuards, then the route's own.
        guards = [
            *opt.guards,
            *bound(GUARDS, handler_fn, controller_cls),
            *item.guards,
        ]
        interceptors = [
            *opt.interceptors,
            *bound(INTERCEPTORS, handler_fn, controller_cls),
            *item.interceptors,
        ]
        guards = await resolve_bindings(guards, opt.container)
        interceptors = await resolve_bindings(interceptors, opt.container)
        route_pipes = await resolve_bindings(
            [*opt.pipes, *bound(PIPES, handler_fn, controller_cls)], opt.container
        )
        route_filters = await resolve_bindings(
            bound(FILTERS, handler_fn, controller_cls), opt.container
        )
        declared_public = item.public or is_public(handler_fn, controller_cls)
        item = replace(item, public=declared_public) if declared_public != item.public else item
        full_path = _normalise(_join(opt.prefix, item.path))
        record = Mounted(
            route=replace(item, guards=tuple(guards)),
            full_path=full_path,
            module=opt.module,
        )

        endpoint = _build_endpoint(
            item,
            handler,
            guards,
            interceptors,
            record,
            opt,
            pipes=route_pipes,
            filters=route_filters,
            controller=controller_cls,
            per_request=per_request,
        )

        router.add_api_route(
            item.path,
            endpoint,
            methods=[item.method],
            name=item.handler_name or getattr(handler, "__name__", None),
            status_code=item.status_code,
            summary=item.doc.summary or None,
            description=item.doc.description or None,
            operation_id=item.doc.operation_id or None,
            tags=list(item.doc.tags) or None,
            deprecated=item.doc.deprecated or None,
            include_in_schema=item.doc.include_in_schema,
            dependencies=[d for d in item.dependencies if isinstance(d, fastapi_params.Depends)],
            response_model=None,
            responses=_openapi_responses(item),
        )

        if opt.registry is not None:
            opt.registry.add(record)
        mounted.append(record)

    return mounted


def _resolve_handler(item: Route, instance: Any) -> Callable[..., Any] | None:
    if instance is not None and item.handler_name:
        bound = getattr(instance, item.handler_name, None)
        if bound is not None:
            return bound
    if item.handler is None:
        return None
    if instance is not None and _wants_self(item.handler):
        return item.handler.__get__(instance, type(instance))
    return item.handler


def _wants_self(fn: Callable[..., Any]) -> bool:
    try:
        first = next(iter(inspect.signature(fn).parameters))
    except (ValueError, StopIteration):
        return False
    return first in ("self", "cls")


_REQUEST_PARAM = "__inceptools_request"

#: ASGI scope key holding the full route template, e.g.
#: ``/api/v1/catalog/products/{pid}``.
ROUTE_TEMPLATE_KEY = "inceptools_route_template"


def _build_endpoint(
    item: Route,
    handler: Callable[..., Any],
    guards: list[Guard],
    interceptors: list[Interceptor],
    record: Mounted,
    opt: MountOptions,
    *,
    pipes: Sequence[Any] = (),
    filters: Sequence[Any] = (),
    controller: type | None = None,
    per_request: bool = False,
) -> Callable[..., Awaitable[Any]]:
    """Wrap *handler* so guards, interceptors and the envelope apply.

    The wrapper carries the handler's own signature, so FastAPI still performs
    its parameter parsing, validation, ``Depends`` resolution and OpenAPI
    generation exactly as if the handler had been registered directly.
    """
    handler_fn = getattr(controller, item.handler_name, None) if controller is not None else None
    signature = _resolved_signature(handler)
    parameters = [p for p in signature.parameters.values() if p.name not in ("self", "cls")]

    request_param = next((p.name for p in parameters if _is_request_annotation(p.annotation)), None)
    injected = request_param is None
    if injected:
        parameters.append(
            inspect.Parameter(_REQUEST_PARAM, inspect.Parameter.KEYWORD_ONLY, annotation=Request)
        )

    is_async = inspect.iscoroutinefunction(
        handler.__func__ if inspect.ismethod(handler) else handler
    )

    # Per-parameter pipes, read once at mount time from Annotated metadata.
    param_pipes: dict[str, tuple[Any, ...]] = {}
    param_sources: dict[str, Any] = {}
    for parameter in parameters:
        param_sources[parameter.name] = parameter.annotation
        if typing.get_origin(parameter.annotation) is typing.Annotated:
            found = [
                extra
                for extra in typing.get_args(parameter.annotation)[1:]
                if isinstance(extra, Piped)
            ]
            if found:
                param_pipes[parameter.name] = tuple(p for marker in found for p in marker.pipes)
    all_pipes = tuple(pipes)

    async def endpoint(**kwargs: Any) -> Any:
        request: Request | None = (
            kwargs.pop(_REQUEST_PARAM) if injected else kwargs.get(request_param or "")
        )

        if request is not None:
            # The matched Starlette route carries a router-local path, so
            # anything outside the router (telemetry, metrics) cannot see the
            # full template. Publish it where they can.
            request.scope[ROUTE_TEMPLATE_KEY] = record.full_path

        ctx = ExecutionContext(
            kind=Kind.HTTP,
            request=request,
            route=item,
            module=opt.module,
            full_path=record.full_path,
            handler=handler_fn or handler,
            controller=controller,
            container=opt.container,
        )

        await run_guards(guards, ctx)

        if all_pipes or param_pipes:
            for name, value in list(kwargs.items()):
                applicable = (*all_pipes, *param_pipes.get(name, ()))
                if not applicable:
                    continue
                kwargs[name] = apply_pipes(
                    value,
                    applicable,
                    ValueMeta(name=name, annotation=param_sources.get(name)),
                )

        async def call() -> Any:
            if per_request:
                # Something in this controller's graph is request-scoped, so
                # the controller is built here rather than at boot. Reusing a
                # boot-time instance would hand every request the first
                # request's dependency.
                assert controller is not None and opt.container is not None
                scope = opt.container.create_scope("request")
                try:
                    per_request_controller = await scope.resolve(controller)
                    bound_handler = getattr(per_request_controller, item.handler_name)
                    produced = bound_handler(**kwargs)
                    result = await produced if inspect.isawaitable(produced) else produced
                finally:
                    with contextlib.suppress(Exception):
                        await scope.aclose()
                return _render(result, item, opt)

            result = handler(**kwargs)
            if is_async:
                result = await result
            # Rendered *inside* the chain, so an interceptor wraps the real
            # response and can set a header or replace the body — which is
            # what an interceptor is for.
            return _render(result, item, opt)

        return await run_interceptors(interceptors, ctx, call)

    endpoint.__name__ = item.handler_name or getattr(handler, "__name__", "endpoint")
    endpoint.__doc__ = handler.__doc__
    # So anything that re-resolves annotations looks in the handler's module,
    # not in this one.
    endpoint.__module__ = getattr(handler, "__module__", __name__)
    endpoint.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=parameters, return_annotation=inspect.Signature.empty
    )
    endpoint.__annotations__ = {p.name: p.annotation for p in parameters}
    return endpoint


def _resolved_signature(handler: Callable[..., Any]) -> inspect.Signature:
    """The handler's signature with string annotations turned back into types.

    A module using ``from __future__ import annotations`` — which is most of
    them — has every annotation as a ``str``. Handing those to FastAPI means it
    cannot see that a parameter is a Pydantic model, and the body silently
    stops being bound. ``eval_str`` resolves them against the handler's own
    module, which is the only place the names are in scope.
    """
    target = handler.__func__ if inspect.ismethod(handler) else handler
    try:
        return inspect.signature(target, eval_str=True)
    except (NameError, TypeError, AttributeError) as exc:
        # A name that is not importable from the handler's module — almost
        # always a model defined inside a function. String annotations cannot
        # reference a local, so nothing can resolve it later either.
        #
        # Fail here, at mount time, with a message that says what to do. The
        # alternative is a route that mounts fine and then answers 400 "body
        # is required" to every request, which is a genuinely horrible hour.
        raise errs.internal(
            f"cannot resolve the type annotations of "
            f"{getattr(target, '__qualname__', target)}: {exc}. "
            "This module uses `from __future__ import annotations`, so its "
            "annotations are strings and must name something importable at "
            "module level — move the model out of the enclosing function."
        ).with_cause(exc) from exc


def _is_request_annotation(annotation: Any) -> bool:
    if annotation is Request:
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _is_request_annotation(typing.get_args(annotation)[0])
    return False


def _render(result: Any, item: Route, opt: MountOptions) -> Any:
    """Put the handler's return value through the envelope."""
    if item.raw:
        return result
    if isinstance(result, Response):
        # A handler that built its own response has already decided; wrapping
        # it again would corrupt a file download or an SSE stream.
        return result
    status = item.status_code or (201 if item.method == "POST" else 200)
    return response_mod.send(status, "", result, options=opt.options)


def _openapi_responses(item: Route) -> dict[int | str, dict[str, Any]] | None:
    """Document the envelope, with the DTO nested under ``data``."""
    model = item.doc.response
    if model is None:
        return None
    try:
        from inceptools.web.schema import envelope_model
    except Exception:  # pragma: no cover
        return None
    wrapped = envelope_model(model)
    if wrapped is None:
        return None
    status = item.status_code or (item.doc.statuses[0] if item.doc.statuses else 200)
    return {status: {"model": wrapped, "description": item.doc.summary or "Success"}}
