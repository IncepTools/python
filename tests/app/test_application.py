"""Boot, drain, and the things that should fail at boot."""

from __future__ import annotations

import pytest

from inceptools import App, Controller, Get, Module, Options, Post, create_app, testkit
from inceptools.core import errs
from inceptools.core.di import Injectable
from inceptools.web import middleware as mw


@Injectable
class Tracked:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def on_module_init(self) -> None:
        self.events.append("init")

    async def on_application_ready(self) -> None:
        self.events.append("ready")

    async def on_module_destroy(self) -> None:
        self.events.append("destroy")


@Controller("/things")
class ThingsController:
    def __init__(self, tracked: Tracked) -> None:
        self.tracked = tracked

    @Get("/")
    async def index(self) -> dict:
        return {"events": self.tracked.events}


@Module(controllers=[ThingsController], providers=[Tracked], exports=[Tracked])
class ThingsModule: ...


@App(modules=[ThingsModule], name="demo")
class DemoApp: ...


class SearchClient: ...


def test_routes_exist_before_startup():
    """OpenAPI, the route table and a route assertion must not need a running app."""
    app = create_app(Options(service="t", configure_logging=False))
    app.mount(DemoApp)
    assert len(app.routes) == 1
    assert "/demo/things" in app.route_table()
    assert app.fastapi.openapi()["paths"]


def test_lifecycle_hooks_run_in_order():
    with testkit.build(DemoApp) as client:
        assert client.get("/demo/things").data == {"events": ["init", "ready"]}


async def test_shutdown_tears_the_container_down():
    app = create_app(Options(service="t", configure_logging=False))
    await app.mount_async(DemoApp)
    await app.start()
    tracked = app.get(Tracked)
    await app.stop()
    assert tracked.events == ["init", "ready", "destroy"]


async def test_a_wiring_failure_fails_the_boot_not_the_first_request():
    from inceptools.core.di import factory

    app = create_app(Options(service="t", configure_logging=False))

    def build() -> SearchClient:
        raise RuntimeError("DATABASE_URL is not set")

    app.container.register(factory(SearchClient, build))
    with pytest.raises(errs.AppError, match=r"wiring failed.*DATABASE_URL"):
        await app.start()


def test_unguarded_writes_can_be_made_fatal():
    @Controller("/leaky")
    class LeakyController:
        @Post("/")
        async def create(self) -> dict:
            return {}

    @Module(controllers=[LeakyController])
    class M: ...

    @App(modules=[M], name="leaky")
    class LeakyApp: ...

    app = create_app(Options(service="t", configure_logging=False, fail_on_unguarded=True))
    app.mount(LeakyApp)
    with (
        pytest.raises(errs.AppError, match="mutating routes with no guard"),
        testkit.TestApp(app),
    ):
        pass


def test_cors_refuses_a_wildcard_with_credentials():
    """Browsers ignore the combination, which makes it look configured."""
    with pytest.raises(errs.AppError, match="cannot be combined"):
        create_app(
            Options(
                service="t",
                configure_logging=False,
                cors=mw.CORSOptions(allow_origins=("*",), allow_credentials=True),
            )
        )


def test_health_routes_sit_outside_the_api_prefix():
    with testkit.build(DemoApp, prefix="/api/v1") as client:
        client.get("/livez").expect_status(200)
        client.get("/api/v1/livez").expect_status(404)


def test_docs_are_served_under_the_prefix():
    with testkit.build(DemoApp, prefix="/api/v1") as client:
        client.get("/api/v1/docs").expect_status(200)
        client.get("/api/v1/openapi.json").expect_status(200)


def test_the_route_dump_is_json():
    import json

    app = create_app(Options(service="shop", version="1.0", configure_logging=False))
    app.mount(DemoApp)
    dump = json.loads(app.routes_json())
    assert dump["service"] == "shop"
    assert dump["routes"][0]["module"] == "demo.things"


async def test_reload_never_takes_the_process_down():
    app = create_app(Options(service="t", configure_logging=False))
    app.on_reload(lambda: (_ for _ in ()).throw(RuntimeError("bad config")))
    assert len(await app.reload()) == 1


def test_mount_inside_a_loop_is_refused():
    import asyncio

    async def inside() -> None:
        app = create_app(Options(service="t", configure_logging=False))
        with pytest.raises(errs.AppError, match="await mount_async"):
            app.mount(DemoApp)

    asyncio.run(inside())
