"""Middleware consumer, testing module, discovery and the lifecycle order."""

from __future__ import annotations

import pytest
from starlette.types import Receive, Scope, Send

from inceptools import App, Controller, Get, Module, testkit
from inceptools.core.di import Injectable
from inceptools.core.discovery import DiscoveryService
from inceptools.core.metadata import set_metadata
from inceptools.testkit.module import TestingModule
from inceptools.web.consumer import MiddlewareConsumer

SEEN: list[str] = []
ORDER: list[str] = []


class TagMiddleware:
    def __init__(self, app, *, tag: str = "default") -> None:
        self.app = app
        self.tag = tag

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            SEEN.append(f"{self.tag}:{scope['path']}")
        await self.app(scope, receive, send)


@Injectable
class Repo:
    def __init__(self) -> None:
        self.rows = {1: "real"}


@Controller("/items")
class ItemController:
    def __init__(self, repo: Repo) -> None:
        self.repo = repo

    @Get("/{pid}")
    async def show(self, pid: int) -> dict:
        return {"value": self.repo.rows.get(pid, "missing")}

    @Get("/health/ping")
    async def ping(self) -> dict:
        return {"ok": True}


@Module(controllers=[ItemController], providers=[Repo], exports=[Repo])
class ItemsModule:
    def configure(self, consumer: MiddlewareConsumer) -> None:
        consumer.apply(TagMiddleware, tag="items").for_routes("/shop/items/*").exclude(
            "/shop/items/health/ping"
        )


@App(modules=[ItemsModule], name="shop")
class ShopApp: ...


@pytest.fixture(autouse=True)
def _clear():
    SEEN.clear()
    ORDER.clear()


# -- middleware consumer --------------------------------------------------


def test_module_middleware_applies_to_its_own_routes():
    with testkit.build(ShopApp) as client:
        client.get("/shop/items/1").expect_ok()
    assert SEEN == ["items:/shop/items/1"]


def test_module_middleware_honours_exclude():
    with testkit.build(ShopApp) as client:
        client.get("/shop/items/health/ping").expect_ok()
    assert SEEN == []


def test_module_middleware_does_not_see_other_paths():
    with testkit.build(ShopApp) as client:
        client.get("/livez").expect_status(200)
    assert SEEN == []


def test_module_middleware_runs_inside_the_request_context():
    """Outermost would mean no trace id, which is the whole reason to care."""
    from inceptools.core import ctxkit

    captured: list[str] = []

    class TraceReader:
        def __init__(self, app) -> None:
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                captured.append(ctxkit.trace_id())
            await self.app(scope, receive, send)

    @Controller("/probe")
    class ProbeController:
        @Get("/")
        async def index(self) -> dict:
            return {}

    @Module(controllers=[ProbeController])
    class ProbeModule:
        def configure(self, consumer: MiddlewareConsumer) -> None:
            consumer.apply(TraceReader)

    @App(modules=[ProbeModule], name="p")
    class ProbeApp: ...

    with testkit.build(ProbeApp) as client:
        client.get("/p/probe").expect_ok()

    assert captured and captured[0] != ""


# -- testing module -------------------------------------------------------


def test_a_provider_can_be_overridden():
    fake = Repo()
    fake.rows = {1: "fake"}

    with TestingModule(ShopApp).override(Repo).use_value(fake).compile() as client:
        client.get("/shop/items/1").expect_data({"value": "fake"})


def test_an_override_wins_over_a_module_provider():
    """The override is on the root, which every module resolves through."""

    class Replacement(Repo):
        def __init__(self) -> None:
            self.rows = {1: "replaced"}

    with TestingModule(ShopApp).override(Repo).use_class(Replacement).compile() as client:
        client.get("/shop/items/1").expect_data({"value": "replaced"})


def test_a_factory_override():
    built = (
        TestingModule(ShopApp).override(Repo).use_factory(lambda: _make_repo("factory")).compile()
    )
    with built as client:
        client.get("/shop/items/1").expect_data({"value": "factory"})


def _make_repo(marker: str) -> Repo:
    repo = Repo()
    repo.rows = {1: marker}
    return repo


def test_a_guard_instance_can_be_refused_for_a_test():
    from inceptools import guard

    @Controller("/secret")
    class SecretController:
        @Get("/", guards=[guard.Public])
        async def index(self) -> dict:
            return {}

    @Module(controllers=[SecretController])
    class M: ...

    @App(modules=[M], name="s")
    class SecretApp: ...

    built = TestingModule(SecretApp).override_guard(guard.Public, allow=False).compile()
    try:
        with built as client:
            client.get("/s/secret").expect_status(403).expect_code("TEST_REFUSED")
    finally:
        # The built-in guards are shared, so put it back.
        TestingModule(SecretApp).override_guard(guard.Public, allow=True).build()


# -- discovery ------------------------------------------------------------

JOB = "test:job"


@Injectable
class Worker:
    @set_metadata(JOB, {"every": 30})
    async def tick(self) -> None:
        ORDER.append("tick")


@Module(providers=[Worker])
class WorkerModule: ...


@App(modules=[WorkerModule], name="w")
class WorkerApp: ...


async def test_discovery_finds_marked_handlers():
    with await testkit.build_async(WorkerApp) as client:
        discovery = DiscoveryService(client.app)
        found = discovery.handlers_with(JOB)
        assert [f.name for f in found] == ["tick"]
        assert found[0].metadata[JOB] == {"every": 30}
        await found[0].method()
    assert ORDER == ["tick"]


def test_discovery_lists_controllers():
    with testkit.build(ShopApp) as client:
        names = [type(c.instance).__name__ for c in DiscoveryService(client.app).controllers()]
        assert names == ["ItemController"]


# -- lifecycle order -----------------------------------------------------


@Injectable
class Ordered:
    async def on_module_init(self) -> None:
        ORDER.append("init")

    async def on_application_bootstrap(self) -> None:
        ORDER.append("bootstrap")

    async def on_application_ready(self) -> None:
        ORDER.append("ready")

    async def before_application_shutdown(self) -> None:
        ORDER.append("before_shutdown")

    async def on_module_destroy(self) -> None:
        ORDER.append("destroy")

    async def on_application_shutdown(self) -> None:
        ORDER.append("shutdown")


@Module(providers=[Ordered])
class OrderedModule: ...


@App(modules=[OrderedModule], name="o")
class OrderedApp: ...


def test_the_documented_lifecycle_order():
    with testkit.build(OrderedApp):
        pass
    assert ORDER == [
        "init",
        "bootstrap",
        "ready",
        "before_shutdown",
        "destroy",
        "shutdown",
    ]
