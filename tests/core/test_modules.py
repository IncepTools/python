"""Modules encapsulate; exports are the only way out."""

from __future__ import annotations

import pytest

from inceptools import App, Controller, Get, Module, testkit
from inceptools.core import errs
from inceptools.core.di import Container, Injectable
from inceptools.core.module import descriptor_of, register_modules


@Injectable
class PrivateHelper:
    value = "private"


@Injectable
class SharedService:
    value = "shared"


@Module(providers=[PrivateHelper, SharedService], exports=[SharedService])
class ProvidersModule: ...


@Injectable
class Consumer:
    def __init__(self, shared: SharedService) -> None:
        self.shared = shared


@Controller("/consume")
class ConsumerController:
    def __init__(self, consumer: Consumer) -> None:
        self.consumer = consumer

    @Get("/")
    async def show(self) -> dict:
        return {"value": self.consumer.shared.value}


@Module(controllers=[ConsumerController], providers=[Consumer], imports=[ProvidersModule])
class ConsumerModule: ...


@App(modules=[ProvidersModule, ConsumerModule], name="shop")
class ShopApp: ...


def test_a_module_name_is_derived_from_its_class():
    assert descriptor_of(ProvidersModule).name == "providers"


def test_an_export_is_resolvable_by_an_importer():
    with testkit.build(ShopApp) as app:
        app.get("/shop/consume").expect_data({"value": "shared"})


async def test_an_export_is_a_single_shared_instance():
    container = Container()
    register_modules(container, [ShopApp])
    assert await container.resolve(SharedService) is await container.resolve(SharedService)


async def test_a_private_provider_is_invisible_to_an_importer():
    """An export is reachable; anything else is not, at resolution time."""
    container = Container()
    _, containers = register_modules(container, [ShopApp])
    consumer = containers[ConsumerModule]

    assert consumer.has(SharedService)
    assert not consumer.has(PrivateHelper)

    # Not on the root either: a module boundary is not advisory.
    assert not container.has(PrivateHelper)
    assert not container.has(SharedService)


def test_exporting_something_you_do_not_provide_is_refused():
    @Module(providers=[], exports=[SharedService])
    class Bad: ...

    @App(modules=[Bad], name="bad")
    class BadApp: ...

    with pytest.raises(errs.AppError, match="which it does not provide"):
        register_modules(Container(), [BadApp])


def test_an_imported_module_is_registered_transitively():
    """A module does not have to be listed on its app; importing it is enough."""

    @Module(providers=[SharedService], exports=[SharedService])
    class SharedModule: ...

    @Module(providers=[Consumer], imports=[SharedModule])
    class NeedyModule: ...

    @App(modules=[NeedyModule], name="needy")
    class NeedyApp: ...

    _, containers = register_modules(Container(), [NeedyApp])
    assert SharedModule in containers
    assert containers[NeedyModule].has(SharedService)


def test_importing_a_non_module_is_refused():
    class NotAModule: ...

    @Module(providers=[], imports=[NotAModule])
    class Needy: ...

    @App(modules=[Needy], name="needy2")
    class NeedyApp: ...

    with pytest.raises(errs.AppError, match="not a module"):
        register_modules(Container(), [NeedyApp])


def test_two_modules_with_the_same_name_are_refused():
    @Module(name="dup", providers=[])
    class One: ...

    @Module(name="dup", providers=[])
    class Two: ...

    @App(modules=[One, Two], name="dupapp")
    class DupApp: ...

    with pytest.raises(errs.AppError, match="two modules named"):
        register_modules(Container(), [DupApp])


def test_a_module_may_add_its_own_url_segment():
    @Controller("/items")
    class ItemsController:
        @Get("/")
        async def index(self) -> list:
            return []

    @Module(controllers=[ItemsController], path="v2")
    class NestedModule: ...

    @App(modules=[NestedModule], name="n")
    class NestedApp: ...

    with testkit.build(NestedApp) as app:
        assert [m.full_path for m in app.routes.all()] == ["/n/v2/items"]


def test_an_app_may_mount_at_the_root():
    @Controller("/ping")
    class PingController:
        @Get("/")
        async def ping(self) -> dict:
            return {"ok": True}

    @Module(controllers=[PingController])
    class M: ...

    @App(modules=[M], name="root", path="")
    class RootApp: ...

    with testkit.build(RootApp) as app:
        assert [m.full_path for m in app.routes.all()] == ["/ping"]
