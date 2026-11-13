"""inceptools — an opinionated application framework for Python services.

Modules, dependency injection, guards, interceptors, typed errors, structured
observability, a scoped data layer, scheduling, caching, locks, idempotency and
an outbox, on top of FastAPI.

    from inceptools import Controller, Get, Module, App, create_app, errs

    @Controller("/products")
    class ProductController:
        def __init__(self, service: ProductService) -> None:
            self.service = service

        @Get("/{pid}")
        async def retrieve(self, pid: int):
            return await self.service.get(pid)

    @Module(controllers=[ProductController], providers=[ProductService])
    class ProductsModule: ...

    @App(modules=[ProductsModule])
    class CatalogApp: ...

    app = create_app(service="shop", prefix="/api/v1")
    app.mount(CatalogApp)

Everything here is re-exported from a subpackage; the subpackage is the stable
place to import from when you want the whole namespace.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

from inceptools.application import (
    APP_FILTER,
    APP_GUARD,
    APP_INTERCEPTOR,
    APP_PIPE,
    VERSION,
    Application,
    Options,
    create_app,
)
from inceptools.core import config, ctxkit, di, errs, lifecycle, logx, telemetry
from inceptools.core.config import ConfigError, Secret, Settings
from inceptools.core.di import (
    Container,
    ForwardRef,
    Inject,
    Injectable,
    ModuleRef,
    Optional,
    Scope,
    Token,
    class_provider,
    existing,
    factory,
    forward_ref,
    inject,
    value,
)
from inceptools.core.discovery import DiscoveryService
from inceptools.core.errs import AppError, ErrorKind, FieldError
from inceptools.core.lifecycle import (
    BeforeApplicationShutdown,
    OnApplicationBootstrap,
    OnApplicationReady,
    OnApplicationReload,
    OnApplicationShutdown,
    OnModuleDestroy,
    OnModuleInit,
)
from inceptools.core.metadata import Reflector, get_metadata, set_metadata
from inceptools.core.module import App, DynamicModule, Global, Module
from inceptools.core.telemetry import Telemetry
from inceptools.web import (
    binding,
    extract,
    guard,
    health,
    interceptor,
    middleware,
    pagination,
    params,
    pipes,
    response,
)
from inceptools.web import filter as filters
from inceptools.web.binding_decorators import (
    Public,
    Roles,
    Scopes,
    SetMetadata,
    UseFilters,
    UseGuards,
    UseInterceptors,
    UsePipes,
)
from inceptools.web.consumer import MiddlewareConsumer
from inceptools.web.execution import ExecutionContext
from inceptools.web.filter import Catch
from inceptools.web.middleware import ChainOptions, CORSOptions, RateLimitOptions
from inceptools.web.response import EnvelopeStyle, ResponseOptions
from inceptools.web.router import (
    Controller,
    Delete,
    Get,
    Head,
    Patch,
    Post,
    Put,
    Route,
)
from inceptools.web.router import (
    Options as RouteOptions,
)

__version__ = VERSION

__all__ = [
    "APP_FILTER",
    "APP_GUARD",
    "APP_INTERCEPTOR",
    "APP_PIPE",
    "VERSION",
    "App",
    "AppError",
    "Application",
    "BeforeApplicationShutdown",
    "CORSOptions",
    "Catch",
    "ChainOptions",
    "ConfigError",
    "Container",
    "Controller",
    "Delete",
    "DiscoveryService",
    "DynamicModule",
    "EnvelopeStyle",
    "ErrorKind",
    "ExecutionContext",
    "FieldError",
    "ForwardRef",
    "Get",
    "Global",
    "Head",
    "Inject",
    "Injectable",
    "MiddlewareConsumer",
    "Module",
    "ModuleRef",
    "OnApplicationBootstrap",
    "OnApplicationReady",
    "OnApplicationReload",
    "OnApplicationShutdown",
    "OnModuleDestroy",
    "OnModuleInit",
    "Optional",
    "Options",
    "Patch",
    "Post",
    "Public",
    "Put",
    "RateLimitOptions",
    "Reflector",
    "Request",
    "Response",
    "ResponseOptions",
    "Roles",
    "Route",
    "RouteOptions",
    "Scope",
    "Scopes",
    "Secret",
    "SetMetadata",
    "Settings",
    "Telemetry",
    "Token",
    "UseFilters",
    "UseGuards",
    "UseInterceptors",
    "UsePipes",
    "__version__",
    "binding",
    "class_provider",
    "config",
    "create_app",
    "ctxkit",
    "di",
    "errs",
    "existing",
    "extract",
    "factory",
    "filters",
    "forward_ref",
    "get_metadata",
    "guard",
    "health",
    "inject",
    "interceptor",
    "lifecycle",
    "logx",
    "middleware",
    "pagination",
    "params",
    "pipes",
    "response",
    "set_metadata",
    "telemetry",
    "value",
]
