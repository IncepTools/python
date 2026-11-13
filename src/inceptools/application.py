"""The application factory. Assembles, serves, drains.

One call turns a set of modules into a running, observable, gracefully
stopping HTTP service::

    app = inceptools.create_app(inceptools.Options(service="shop", prefix="/api/v1"))
    app.supply(engine)
    app.mount(CatalogApp)

``app`` is an ASGI application, so ``uvicorn main:app`` serves it and a
serverless adapter can hand it straight to its HTTP bridge.

Boot order, which is the part worth knowing:

    container registered -> modules built -> routes mounted -> health routes
    -> startup: eager construction, then lifecycle init, then ready
    -> shutdown: lifecycle drain (reverse order), then container teardown

Providers are constructed **eagerly** at startup, so a missing environment
variable or an unreachable dependency fails the deploy rather than the first
request that happened to need it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.types import Receive, Scope, Send

from inceptools.core import errs
from inceptools.core import logx as logx_mod
from inceptools.core import telemetry as telemetry_mod
from inceptools.core.di import Container, ModuleRef, Provider, Token
from inceptools.core.di import Scope as DIScope
from inceptools.core.lifecycle import Lifecycle, Phase
from inceptools.core.metadata import Reflector, reflector
from inceptools.core.module import (
    AppDescriptor,
    BuiltModule,
    as_provider,
    build_modules,
    owned_entities,
    register_modules,
    seed_all,
)
from inceptools.web import consumer as consumer_mod
from inceptools.web import filter as filter_mod
from inceptools.web import health as health_mod
from inceptools.web import middleware as middleware_mod
from inceptools.web import response as response_mod
from inceptools.web.guard import Guard
from inceptools.web.interceptor import Interceptor
from inceptools.web.router import MountOptions, Registry, controller_routes, mount_routes

__all__ = [
    "APP_FILTER",
    "APP_GUARD",
    "APP_INTERCEPTOR",
    "APP_PIPE",
    "Application",
    "Options",
    "create_app",
]

#: Register a provider under one of these to apply it to **every** route of
#: every module, with dependency injection available to it.
#:
#:     @Module(providers=[value(APP_GUARD, [AuthGuard]), AuthGuard])
#:     class AppModule: ...
APP_GUARD = Token("inceptools.APP_GUARD")
APP_INTERCEPTOR = Token("inceptools.APP_INTERCEPTOR")
APP_FILTER = Token("inceptools.APP_FILTER")
APP_PIPE = Token("inceptools.APP_PIPE")

#: The framework's own version, reported on the health detail endpoint.
VERSION = "0.1.0"

_log = logging.getLogger("inceptools")


@dataclass(slots=True)
class Options:
    """How this service is assembled."""

    service: str = "service"
    version: str = ""
    #: Mounted under this prefix. Health routes stay **outside** it, because a
    #: probe should not have to know your API version.
    prefix: str = ""
    description: str = ""

    middleware: middleware_mod.ChainOptions = field(default_factory=middleware_mod.ChainOptions)
    cors: middleware_mod.CORSOptions | None = None
    rate_limit: middleware_mod.RateLimitOptions | None = None

    #: Applied to every route of every module, before each route's own.
    guards: tuple[Guard, ...] = ()
    interceptors: tuple[Interceptor, ...] = ()
    #: Consulted before the built-in error mapping.
    filters: tuple[Any, ...] = ()
    log_client_errors: bool = False

    response: response_mod.ResponseOptions = field(default_factory=response_mod.ResponseOptions)
    #: Last-chance rewrite of any outbound message. ``(message, scope, route)``.
    sanitizer: response_mod.Sanitizer | None = None

    logging: logx_mod.LoggingOptions | None = None
    configure_logging: bool = True
    #: OpenTelemetry. ``None`` leaves tracing off; an unconfigured
    #: :class:`~inceptools.core.telemetry.Telemetry` is a working no-op.
    telemetry: telemetry_mod.Telemetry | None = None

    drain_timeout: float = 25.0
    health_checks: tuple[Any, ...] = ()
    disable_health_routes: bool = False
    #: Health routes are mounted here, outside :attr:`prefix`.
    health_prefix: str = ""
    health_timeout: float = 2.0

    docs_url: str | None = "/docs"
    redoc_url: str | None = "/redoc"
    openapi_url: str | None = "/openapi.json"
    #: Warn about unguarded mutating routes at boot. Set to raise in CI.
    fail_on_unguarded: bool = False


class Application:
    """An assembled service."""

    def __init__(self, options: Options) -> None:
        self.options = options
        self.container = Container(name="root")
        self.routes = Registry()
        self.lifecycle = Lifecycle(drain_timeout=options.drain_timeout)
        self.health = health_mod.Registry(
            options.service, options.version, timeout=options.health_timeout
        )
        self.log = logging.getLogger(options.service or "inceptools")

        self._apps: list[AppDescriptor] = []
        self._built: list[BuiltModule] = []
        self._api = APIRouter()
        self._mounted_api = False
        self._started = False
        self._prepare_lock = asyncio.Lock()
        self._middleware_bindings: list[Any] = []

        if options.configure_logging:
            logx_mod.configure(
                options.logging
                or logx_mod.LoggingOptions(service=options.service, json_to_stream=True)
            )

        response_mod.set_default_options(options.response)
        if options.sanitizer is not None:
            response_mod.set_sanitizer(options.sanitizer)

        self.fastapi = self._build_fastapi()
        self.container.supply("logger", self.log)
        self.container.supply(Application, self)
        self.container.supply(Reflector, reflector)
        self.container.supply(ModuleRef, ModuleRef(self.container))

        if options.telemetry is not None:
            telemetry_mod.set_active(options.telemetry)
            self.lifecycle.register_object(options.telemetry, name="telemetry")
            self.container.supply(telemetry_mod.Telemetry, options.telemetry)

        for checker in options.health_checks:
            self.health.register(checker)

    # -- construction ----------------------------------------------------

    def _build_fastapi(self) -> FastAPI:
        opt = self.options

        api = FastAPI(
            title=opt.service,
            version=opt.version or VERSION,
            description=opt.description,
            docs_url=_prefixed(opt.prefix, opt.docs_url),
            redoc_url=_prefixed(opt.prefix, opt.redoc_url),
            openapi_url=_prefixed(opt.prefix, opt.openapi_url),
            lifespan=self._lifespan,
        )

        filter_options = filter_mod.FilterOptions(
            filters=list(opt.filters),
            log_client_errors=opt.log_client_errors,
            options=opt.response,
        )
        filter_mod.install(api, filter_options)

        # Starlette applies middleware in reverse registration order, so the
        # last added is outermost. Register the chain reversed to get the
        # documented order: context, recover, secure headers, access log,
        # identity, timeout.
        chain = middleware_mod.build_chain(opt.middleware, response_options=opt.response)
        for cls, kwargs in reversed(chain):
            api.add_middleware(cls, **kwargs)

        if opt.telemetry is not None:
            cls, kwargs = opt.telemetry.middleware()
            api.add_middleware(cls, **kwargs)

        if opt.rate_limit is not None:
            api.add_middleware(
                middleware_mod.RateLimitMiddleware,
                options=opt.rate_limit,
                response_options=opt.response,
            )

        if opt.cors is not None:
            cors = opt.cors
            origins = list(cors.allow_origins)
            if cors.allow_credentials and "*" in origins:
                # The combination is forbidden by the CORS spec and silently
                # ignored by browsers, which makes it look configured when it
                # is not. Refuse at boot instead.
                raise errs.internal(
                    "CORS: allow_origins=['*'] cannot be combined with allow_credentials=True; "
                    "list the origins explicitly"
                )
            api.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_origin_regex=cors.allow_origin_regex or None,
                allow_methods=list(cors.allow_methods),
                allow_headers=list(cors.allow_headers),
                expose_headers=list(cors.expose_headers),
                allow_credentials=cors.allow_credentials,
                max_age=cors.max_age,
            )

        if not opt.disable_health_routes:
            self._mount_health(api)

        return api

    def _mount_health(self, api: FastAPI) -> None:
        prefix = (self.options.health_prefix or "").rstrip("/")
        router = APIRouter(include_in_schema=False)
        router.add_api_route("/livez", health_mod.live_handler(), methods=["GET"])
        # /healthz is the compatibility alias and checks nothing, deliberately.
        router.add_api_route("/healthz", health_mod.live_handler(), methods=["GET"])
        router.add_api_route("/readyz", health_mod.ready_handler(self.health), methods=["GET"])
        router.add_api_route("/health", health_mod.detail_handler(self.health), methods=["GET"])
        api.include_router(router, prefix=prefix)

    # -- the container ---------------------------------------------------

    def supply(self, token: Any, instance: Any = None) -> Application:
        """Put an already-built object in the container."""
        self.container.supply(token, instance)
        return self

    def provide(self, *providers: Provider | type) -> Application:
        """Register root providers, shared by every app."""
        for provider in providers:
            self.container.register(as_provider(provider))
        return self

    def get(self, token: Any) -> Any:
        """An already-built instance, from the root or any module container.

        Module providers are private to their module, so the root container
        cannot see them. This searches every container the application owns,
        which is what a test or an operational script wants.
        """
        try:
            return self.container.get(token)
        except errs.AppError:
            pass
        for item in self._built:
            try:
                return item.container.get(token)
            except errs.AppError:
                continue
        raise errs.internal(
            f"{getattr(token, '__name__', token)} has not been constructed in this application"
        )

    async def resolve(self, token: Any) -> Any:
        """Build or fetch *token* from the root or any module container."""
        if self.container.has(token):
            return await self.container.resolve(token)
        for item in self._built:
            if item.container.has(token):
                return await item.container.resolve(token)
        return await self.container.resolve(token)

    def module_ref(self) -> ModuleRef:
        """A :class:`~inceptools.core.di.ModuleRef` over the root container."""
        return ModuleRef(self.container)

    def on_init(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        self.lifecycle.on_init(fn)
        return fn

    def on_ready(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        self.lifecycle.on_ready(fn)
        return fn

    def on_reload(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        self.lifecycle.on_reload(fn)
        return fn

    def on_shutdown(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        self.lifecycle.on_shutdown(fn)
        return fn

    # -- mounting --------------------------------------------------------

    def mount(self, *apps: Any) -> Application:
        """Register and mount one or more apps.

        Synchronous on purpose: the routes exist as soon as this returns, so
        OpenAPI generation, a route-table assertion and a test client all see
        a fully built application without having to start it first.

        Inside a running event loop, use :meth:`mount_async`.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.mount_async(*apps))
            return self
        raise errs.internal(
            "Application.mount() cannot run inside an event loop; await mount_async() instead"
        )

    async def mount_async(self, *apps: Any) -> Application:
        """Register every module's providers, build them, then mount routes."""
        descriptors, containers = register_modules(self.container, apps)
        built = await build_modules(self.container, descriptors, containers)

        globals_ = await self._global_bindings([self.container, *(c for c in containers.values())])

        for item in built:
            await self._mount_module(item, globals_)
            self.health.register(*item.descriptor.health_checks)
            if item.instance is not None:
                self.lifecycle.register_object(item.instance, name=item.qualified_name)

        self._install_module_middleware(built)
        self._apps.extend(descriptors)
        self._built.extend(built)
        self._include_api()
        return self

    def _install_module_middleware(self, built: Sequence[BuiltModule]) -> None:
        """Install what each module's ``configure`` asked for.

        Appended to Starlette's list rather than added through
        ``add_middleware``, which would make them *outermost* — outside the
        request-context middleware, so they would run without a trace id.
        """
        bindings = consumer_mod.collect(built)
        if not bindings:
            return
        for binding in bindings:
            self.fastapi.user_middleware.append(
                StarletteMiddleware(consumer_mod.PathScoped, binding=binding)
            )
            self._middleware_bindings.append(binding)

    async def _global_bindings(
        self, containers: Sequence[Container] | None = None
    ) -> dict[str, list[Any]]:
        """Resolve whatever was registered under the ``APP_*`` tokens.

        A global guard registered as a provider — rather than passed to
        ``create_app`` — can have dependencies of its own. That is why both
        routes exist: the option is for a guard that needs nothing, the token
        for one that does.
        """
        searched = list(containers or [self.container])
        collected: dict[str, list[Any]] = {}

        for key, token in (
            ("guards", APP_GUARD),
            ("interceptors", APP_INTERCEPTOR),
            ("filters", APP_FILTER),
            ("pipes", APP_PIPE),
        ):
            found: list[Any] = []
            for container in searched:
                # A module may register its own global binding; that is the
                # point of the APP_* tokens over the constructor options.
                if token not in container:
                    continue
                resolved: Any = await container.resolve(token)
                found.extend(resolved if isinstance(resolved, list | tuple) else [resolved])
            collected[key] = found

        return collected

    async def _mount_module(
        self, item: BuiltModule, globals_: dict[str, list[Any]] | None = None
    ) -> None:
        opt = self.options
        globals_ = globals_ or {}
        app_path = _segment(item.app.mount_path)
        module_path = _segment(item.descriptor.mount_path)
        prefix = f"{opt.prefix}{app_path}{module_path}"

        guards = (
            *globals_.get("guards", ()),
            *opt.guards,
            *item.app.guards,
            *item.descriptor.guards,
        )
        interceptors = (
            *globals_.get("interceptors", ()),
            *opt.interceptors,
            *item.app.interceptors,
            *item.descriptor.interceptors,
        )
        pipes = tuple(globals_.get("pipes", ()))

        router = APIRouter()
        for controller in item.controllers:
            await mount_routes(
                router,
                controller_routes(controller),
                MountOptions(
                    module=item.qualified_name,
                    prefix=prefix,
                    registry=self.routes,
                    guards=guards,
                    interceptors=interceptors,
                    pipes=pipes,
                    container=item.container,
                    options=opt.response,
                    controller=type(controller),
                ),
                instance=controller,
            )
        self._api.include_router(router, prefix=f"{app_path}{module_path}")

    def _include_api(self) -> None:
        if self._mounted_api:
            return
        self.fastapi.include_router(self._api, prefix=self.options.prefix)
        self._mounted_api = True

    # -- lifecycle -------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _lifespan(self, _: FastAPI) -> AsyncIterator[None]:
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    async def start(self) -> None:
        """Construct every eager provider, then run init and ready."""
        async with self._prepare_lock:
            if self._started:
                return

            self._report_unguarded()

            try:
                await self.container.start()
                for item in self._built:
                    await item.container.start()
            except Exception as exc:
                raise errs.internal(f"wiring failed: {exc}").with_cause(exc) from exc

            # Every provider the container built gets its lifecycle hooks.
            # Disposal is left to the container, which runs in reverse
            # construction order — stronger than registration order.
            self._register_provider_hooks()

            await self.lifecycle.start()
            self._started = True

            self.log.info(
                "started",
                extra={
                    "service": self.options.service,
                    "version": self.options.version or VERSION,
                    "framework": VERSION,
                    "prefix": self.options.prefix,
                    "routes": len(self.routes),
                    "modules": len(self._built),
                },
            )

    async def stop(self) -> None:
        """Drain, then tear the container down in reverse construction order."""
        if not self._started:
            return
        self._started = False

        # The documented order: stop taking work, dispose everything, then
        # have the last word.
        for failure in await self.lifecycle.before_shutdown():
            self.log.warning("pre-shutdown hook failed: %s", failure)

        # Module containers first, newest module first, then the root — so a
        # module's repository is disposed before the engine the root provided
        # it. Each container already disposes its own instances in reverse
        # construction order.
        for item in reversed(self._built):
            with contextlib.suppress(errs.AppError):
                await item.container.aclose()
        with contextlib.suppress(errs.AppError):
            await self.container.aclose()

        for failure in await self.lifecycle.shutdown():
            self.log.warning("shutdown hook failed: %s", failure)

        self.log.info("stopped", extra={"service": self.options.service})

    async def reload(self) -> list[Any]:
        """Re-read configuration without dropping connections.

        A server wires this to ``SIGHUP``; a serverless control plane calls it
        directly, because it has no signals.
        """
        failures = await self.lifecycle.reload()
        for failure in failures:
            self.log.warning("reload hook failed: %s", failure)
        return failures

    async def seed(self) -> None:
        """Run every module's seed routine. Must be idempotent."""
        await seed_all(self._built)

    def _register_provider_hooks(self) -> None:
        seen: set[int] = set()
        # Every phase except disposal: ``on_module_destroy`` belongs to the
        # container, which calls it in reverse construction order.
        phases = (
            Phase.INIT,
            Phase.READY,
            Phase.RELOAD,
            Phase.BEFORE_SHUTDOWN,
            Phase.SHUTDOWN,
        )
        containers = [self.container, *(item.container for item in self._built)]
        for container in containers:
            for instance in container.instances():
                if id(instance) in seen or instance is self:
                    continue
                seen.add(id(instance))
                self.lifecycle.register_object(instance, phases=phases)

    def _report_unguarded(self) -> None:
        unguarded = self.routes.unguarded()
        if not unguarded:
            return
        for item in unguarded:
            self.log.warning(
                "mutating route has no guard",
                extra={
                    "method": item.route.method,
                    "route": item.full_path,
                    "module_name": item.module,
                },
            )
        if self.options.fail_on_unguarded:
            listed = ", ".join(f"{m.route.method} {m.full_path}" for m in unguarded)
            raise errs.internal(f"mutating routes with no guard: {listed}")

    # -- ASGI ------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.fastapi(scope, receive, send)

    # -- introspection ---------------------------------------------------

    def route_table(self) -> str:
        return self.routes.table()

    def routes_json(self) -> str:
        return json.dumps(
            {
                "service": self.options.service,
                "version": self.options.version or VERSION,
                "routes": self.routes.as_dicts(),
            },
            indent=2,
        )

    def write_routes(self, path: str | Path) -> None:
        Path(path).write_text(self.routes_json() + "\n", encoding="utf-8")

    def entities(self) -> dict[str, list[Any]]:
        """Which module owns which entity."""
        return owned_entities(self._built)

    def run(self, host: str = "0.0.0.0", port: int = 8080, **kwargs: Any) -> None:
        """Serve with uvicorn. Convenience only — a container runs the ASGI app."""
        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover
            raise errs.internal(
                "uvicorn is not installed; add the 'server' extra or run the ASGI app yourself"
            ).with_cause(exc) from exc
        uvicorn.run(self.fastapi, host=host, port=port, **kwargs)

    def __repr__(self) -> str:
        return (
            f"<Application {self.options.service!r} "
            f"modules={len(self._built)} routes={len(self.routes)}>"
        )


def _segment(name: str) -> str:
    if not name:
        return ""
    return "/" + name.strip("/")


def _prefixed(prefix: str, path: str | None) -> str | None:
    if path is None:
        return None
    return f"{prefix}{path}" if prefix else path


def create_app(options: Options | None = None, **kwargs: Any) -> Application:
    """Build an application.

    app = create_app(service="shop", prefix="/api/v1")
    """
    if options is not None and kwargs:
        raise errs.internal("pass either an Options value or keyword arguments, not both")
    return Application(options or Options(**kwargs))


def _unused(*_: Any) -> None:  # pragma: no cover
    _ = (DIScope, Phase, Sequence)
