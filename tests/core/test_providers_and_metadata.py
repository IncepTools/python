"""Metadata, Use* binding, dynamic modules, globals and request scope."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from inceptools import App, Controller, Get, Module, Post, create_app, errs, testkit
from inceptools.application import APP_GUARD, APP_INTERCEPTOR
from inceptools.core.di import (
    Container,
    Injectable,
    ModuleRef,
    Scope,
    Token,
    class_provider,
    existing,
    factory,
    forward_ref,
    inject,
    value,
)
from inceptools.core.metadata import Reflector
from inceptools.core.module import DynamicModule, Global, register_modules
from inceptools.web import interceptor
from inceptools.web.binding_decorators import (
    Public,
    Roles,
    SetMetadata,
    UseGuards,
    UseInterceptors,
    UsePipes,
)
from inceptools.web.execution import ExecutionContext
from inceptools.web.pipes import Trim

SETTINGS = Token("app.settings")
CACHE_URL = Token("cache.url")


# -- a guard with an injected dependency ---------------------------------


@Injectable
class RolesGuard:
    """A guard that reads the metadata its handler declared."""

    def __init__(self, reflector: Reflector) -> None:
        self.reflector = reflector

    @property
    def name(self) -> str:
        return "roles"

    async def can_activate(self, ctx: ExecutionContext) -> None:
        from inceptools.core import ctxkit
        from inceptools.web.binding_decorators import ROLES, is_public

        if is_public(ctx.handler, ctx.controller):
            return
        needed = self.reflector.get_all_and_override(ROLES, ctx.handler, ctx.controller)
        if needed and not ctxkit.has_role(*needed):
            raise errs.forbidden(f"needs one of: {', '.join(needed)}").with_code("ROLE_REQUIRED")


@Injectable
class AuditService:
    def __init__(self) -> None:
        self.seen: list[str] = []


class CreateInvoice(BaseModel):
    reference: str


@Controller("/invoices", tags=["invoices"])
@UseGuards(RolesGuard)
@Roles("billing:read")
class InvoiceController:
    def __init__(self, audit: AuditService) -> None:
        self.audit = audit

    @Get("/")
    async def index(self) -> list:
        return []

    @Post("/", public=True)
    @Roles("billing:write")
    @UsePipes(Trim())
    async def create(self, body: CreateInvoice) -> dict:
        self.audit.seen.append(body.reference)
        return {"reference": body.reference}

    @Get("/ping")
    @Public()
    async def ping(self) -> dict:
        return {"ok": True}


@Module(controllers=[InvoiceController], providers=[AuditService, RolesGuard])
class InvoicesModule: ...


@App(modules=[InvoicesModule], name="billing")
class BillingApp: ...


@pytest.fixture
def billing():
    with testkit.build(BillingApp) as app:
        yield app


# -- guards read metadata -------------------------------------------------


def test_a_class_level_role_applies_to_every_handler(billing):
    billing.get("/billing/invoices").expect_status(403).expect_code("ROLE_REQUIRED")
    billing.as_actor("u", roles="billing:read")
    billing.get("/billing/invoices").expect_ok()


def test_a_method_level_role_overrides_the_class(billing):
    """The specific wins: @Roles on the handler replaces the controller's."""
    billing.as_actor("u", roles="billing:read")
    billing.post("/billing/invoices", {"reference": "INV-1"}).expect_status(403)
    billing.as_actor("u", roles="billing:write")
    billing.post("/billing/invoices", {"reference": "INV-1"}).expect_status(201)


def test_public_stands_a_global_guard_down(billing):
    billing.anonymous()
    billing.get("/billing/invoices/ping").expect_ok().expect_data({"ok": True})


def test_a_guard_class_gets_its_dependencies_injected(billing):
    """RolesGuard takes a Reflector; it could not work otherwise."""
    billing.as_actor("u", roles="billing:read")
    billing.get("/billing/invoices").expect_ok()


def test_use_pipes_transforms_the_body(billing):
    billing.as_actor("u", roles="billing:write")
    reply = billing.post("/billing/invoices", {"reference": "  INV-2  "})
    reply.expect_status(201).expect_data({"reference": "INV-2"})


def test_set_metadata_is_readable_by_the_reflector():
    @SetMetadata("cache-ttl", 300)
    def handler() -> None: ...

    assert Reflector().get("cache-ttl", handler) == 300


# -- dynamic modules ------------------------------------------------------


@Module(providers=[])
class CacheModule:
    @classmethod
    def for_root(cls, *, url: str) -> DynamicModule:
        return DynamicModule(
            cls,
            providers=[value(CACHE_URL, url), factory(FakeCache, _build_cache, inject=[CACHE_URL])],
            exports=[CACHE_URL, FakeCache],
        )


class FakeCache:
    def __init__(self, url: str) -> None:
        self.url = url


def _build_cache(url: str) -> FakeCache:
    return FakeCache(url)


@Injectable
class SearchService:
    def __init__(self, cache: FakeCache) -> None:
        self.cache = cache


@Module(providers=[SearchService], imports=[CacheModule.for_root(url="redis://configured")])
class SearchModule: ...


@App(modules=[SearchModule], name="search")
class SearchApp: ...


async def test_a_dynamic_module_configures_its_providers():
    container = Container()
    _, containers = register_modules(container, [SearchApp])
    service = await containers[SearchModule].resolve(SearchService)
    assert service.cache.url == "redis://configured"


async def test_the_same_dynamic_module_is_registered_once():
    @Module(providers=[SearchService], imports=[CacheModule.for_root(url="redis://a")])
    class OneModule: ...

    @Module(providers=[], imports=[CacheModule.for_root(url="redis://a")])
    class TwoModule: ...

    @App(modules=[OneModule, TwoModule], name="twice")
    class TwiceApp: ...

    _, containers = register_modules(Container(), [TwiceApp])
    assert CacheModule in containers


# -- global modules -------------------------------------------------------


@Global
@Module(providers=[value(SETTINGS, {"env": "test"})], exports=[SETTINGS])
class ConfigModule: ...


@Injectable
@inject(settings=SETTINGS)
class NeedsSettings:
    def __init__(self, settings: dict) -> None:
        self.settings = settings


@Module(providers=[NeedsSettings])
class ConsumerModule: ...


@App(modules=[ConfigModule, ConsumerModule], name="globalapp")
class GlobalApp: ...


async def test_a_global_export_needs_no_import():
    container = Container()
    _, containers = register_modules(container, [GlobalApp])
    built = await containers[ConsumerModule].resolve(NeedsSettings)
    assert built.settings == {"env": "test"}


async def test_inject_names_a_provider_for_a_plain_type():
    """A dict parameter cannot identify its provider by type."""
    container = Container()
    container.register(value(SETTINGS, {"env": "x"}))
    assert (await container.resolve(NeedsSettings)).settings == {"env": "x"}


# -- aliases and forward refs ---------------------------------------------


@Injectable
class ConcreteMailer:
    def send(self) -> str:
        return "sent"


MAILER = Token("mailer")


async def test_an_alias_resolves_to_the_same_instance():
    container = Container()
    container.bind(ConcreteMailer)
    container.register(existing(MAILER, ConcreteMailer))
    assert await container.resolve(MAILER) is await container.resolve(ConcreteMailer)


async def test_a_forward_ref_breaks_an_import_cycle():
    @Module(providers=[AuditService], exports=[AuditService])
    class LateModule: ...

    @Module(providers=[], imports=[forward_ref(lambda: LateModule)])
    class EarlyModule: ...

    @App(modules=[EarlyModule], name="cycle")
    class CycleApp: ...

    _, containers = register_modules(Container(), [CycleApp])
    assert containers[EarlyModule].has(AuditService)


# -- module re-exports ----------------------------------------------------


async def test_a_module_may_re_export_a_module_it_imports():
    """One import for the consumer, two modules behind it."""

    @Module(providers=[AuditService], exports=[AuditService])
    class InnerModule: ...

    @Module(providers=[], imports=[InnerModule], exports=[InnerModule])
    class AggregateModule: ...

    @Module(providers=[], imports=[AggregateModule])
    class OuterModule: ...

    @App(modules=[OuterModule], name="agg")
    class AggApp: ...

    _, containers = register_modules(Container(), [AggApp])
    assert containers[OuterModule].has(AuditService)


# -- ModuleRef ------------------------------------------------------------


async def test_module_ref_creates_an_unregistered_class():
    class Strategy:
        def __init__(self, audit: AuditService) -> None:
            self.audit = audit

    container = Container()
    container.bind(AuditService)
    ref = ModuleRef(container)
    built = await ref.create(Strategy)
    assert isinstance(built.audit, AuditService)
    assert not ref.has(Strategy)


def test_module_ref_is_injectable():
    with testkit.build(BillingApp) as client:
        assert isinstance(client.container.get(ModuleRef), ModuleRef)


# -- APP_* global providers -----------------------------------------------


@Injectable
class CountingInterceptor:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "counting"

    async def intercept(self, ctx, call_next):
        self.calls += 1
        return await call_next()


@Controller("/widgets")
class WidgetController:
    @Get("/")
    async def index(self) -> list:
        return []


@Module(
    controllers=[WidgetController],
    providers=[
        CountingInterceptor,
        factory(APP_INTERCEPTOR, lambda i: [i], inject=[CountingInterceptor]),
    ],
)
class WidgetModule: ...


@App(modules=[WidgetModule], name="widgets")
class WidgetApp: ...


def test_app_interceptor_applies_to_every_route():
    app = create_app(service="t", configure_logging=False)
    app.mount(WidgetApp)
    with testkit.TestApp(app) as client:
        client.get("/widgets/widgets").expect_ok()
        client.get("/widgets/widgets").expect_ok()
        # Read it inside the block: shutdown disposes the container, which is
        # the behaviour tested in test_shutdown_tears_the_container_down.
        assert app.get(CountingInterceptor).calls == 2


def test_app_guard_applies_to_every_route():
    @Injectable
    class DenyAll:
        @property
        def name(self) -> str:
            return "deny"

        async def can_activate(self, ctx) -> None:
            raise errs.forbidden("nothing is allowed").with_code("DENIED")

    @Controller("/things")
    class ThingController:
        @Get("/")
        async def index(self) -> list:
            return []

    @Module(
        controllers=[ThingController],
        providers=[DenyAll, factory(APP_GUARD, lambda g: [g], inject=[DenyAll])],
    )
    class ThingModule: ...

    @App(modules=[ThingModule], name="denied")
    class DeniedApp: ...

    with testkit.build(DeniedApp) as client:
        client.get("/denied/things").expect_status(403).expect_code("DENIED")


# -- UseInterceptors ------------------------------------------------------


def test_use_interceptors_on_a_controller():
    header = interceptor.header("x-tagged", "yes")

    @Controller("/tagged")
    @UseInterceptors(header)
    class TaggedController:
        @Get("/")
        async def index(self) -> dict:
            return {}

    @Module(controllers=[TaggedController])
    class M: ...

    @App(modules=[M], name="tag")
    class TagApp: ...

    with testkit.build(TagApp) as client:
        client.get("/tag/tagged").expect_header("x-tagged", "yes")


# -- request scope --------------------------------------------------------

REQUEST_IDS: list[int] = []


@Injectable(scope=Scope.REQUEST)
class RequestTrace:
    """One per request. Reusing it across requests is the bug being prevented."""

    _counter = 0

    def __init__(self) -> None:
        RequestTrace._counter += 1
        self.serial = RequestTrace._counter
        self.closed = False

    async def on_module_destroy(self) -> None:
        self.closed = True


@Injectable
class ScopedService:
    def __init__(self, trace: RequestTrace) -> None:
        self.trace = trace


@Controller("/scoped")
class ScopedController:
    def __init__(self, service: ScopedService) -> None:
        self.service = service

    @Get("/")
    async def show(self) -> dict:
        REQUEST_IDS.append(self.service.trace.serial)
        return {"serial": self.service.trace.serial}


@Module(controllers=[ScopedController], providers=[ScopedService, RequestTrace])
class ScopedModule: ...


@App(modules=[ScopedModule], name="scoped")
class ScopedApp: ...


def test_a_request_scoped_provider_is_new_each_request():
    REQUEST_IDS.clear()
    with testkit.build(ScopedApp) as client:
        first = client.get("/scoped/scoped").expect_ok().data["serial"]
        second = client.get("/scoped/scoped").expect_ok().data["serial"]
    assert first != second
    assert [first, second] == REQUEST_IDS


def test_request_scope_bubbles_through_a_singleton_consumer():
    """ScopedService is declared singleton, but depends on a request-scoped
    provider, so it must be rebuilt per request too."""
    from inceptools.core.di import Container

    container = Container()
    container.bind(RequestTrace, scope=Scope.REQUEST)
    container.bind(ScopedService)
    container.register(class_provider(ScopedController))
    assert container.needs_request_scope(ScopedController)
    assert container.needs_request_scope(ScopedService)


def test_a_singleton_graph_is_not_request_scoped():
    from inceptools.core.di import Container

    container = Container()
    container.bind(AuditService)
    assert not container.needs_request_scope(AuditService)
