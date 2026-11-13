"""Lazy, typed providers with cycle detection and ordered teardown."""

from __future__ import annotations

from typing import Annotated

import pytest

from inceptools.core import errs
from inceptools.core.di import Container, Inject, Injectable, Scope, Token, factory, value


class Engine:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@Injectable
class Repo:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@Injectable
class Service:
    def __init__(self, repo: Repo) -> None:
        self.repo = repo


class CycleA:
    def __init__(self, b: CycleB) -> None: ...


class CycleB:
    def __init__(self, a: CycleA) -> None: ...


SETTINGS = Token("app.settings")


class NeedsMissing:
    def __init__(self, cfg: Annotated[str, Inject("missing.token")]) -> None: ...


class Cache: ...


class NeedsOptionalCache:
    def __init__(self, cache: Cache | None = None) -> None:
        self.cache = cache


class NeedsSettings:
    def __init__(self, cfg: Annotated[dict, Inject(SETTINGS)]) -> None:
        self.cfg = cfg


async def test_constructor_injection_resolves_the_graph():
    container = Container()
    service = await container.resolve(Service)
    assert isinstance(service.repo, Repo)
    assert isinstance(service.repo.engine, Engine)


async def test_a_singleton_is_built_once():
    container = Container()
    assert await container.resolve(Service) is await container.resolve(Service)


async def test_transient_providers_are_rebuilt():
    container = Container()
    container.bind(Engine, scope=Scope.TRANSIENT)
    assert await container.resolve(Engine) is not await container.resolve(Engine)


async def test_a_cycle_names_the_ring():
    container = Container()
    with pytest.raises(errs.AppError, match="dependency cycle"):
        await container.resolve(CycleA)


async def test_a_missing_named_provider_is_reported():
    with pytest.raises(errs.AppError, match=r"no provider for missing\.token"):
        await Container().resolve(NeedsMissing)


async def test_values_and_tokens():
    container = Container()
    container.register(value(SETTINGS, {"port": 8080}))
    assert (await container.resolve(NeedsSettings)).cfg == {"port": 8080}


async def test_unresolvable_annotations_fail_clearly():
    """A dependency named by a string that is not importable at module level."""

    class Local: ...

    class Needs:
        def __init__(self, thing: Local) -> None: ...

    with pytest.raises(errs.AppError, match="move the type to module level"):
        await Container().resolve(Needs)


async def test_an_async_factory_is_awaited():
    container = Container()

    async def build() -> Engine:
        return Engine()

    container.register(factory(Engine, build))
    assert isinstance(await container.resolve(Engine), Engine)


async def test_a_generator_factory_runs_its_teardown():
    container = Container()
    events: list[str] = []

    async def build():
        events.append("open")
        yield Engine()
        events.append("close")

    container.register(factory(Engine, build))
    await container.resolve(Engine)
    await container.aclose()
    assert events == ["open", "close"]


async def test_teardown_runs_in_reverse_construction_order():
    container = Container()
    service = await container.resolve(Service)
    engine, repo = service.repo.engine, service.repo
    await container.aclose()
    assert repo.closed and engine.closed


async def test_an_optional_dependency_may_be_absent():
    assert (await Container().resolve(NeedsOptionalCache)).cache is None


async def test_an_optional_dependency_is_used_when_provided():
    container = Container()
    cache = Cache()
    container.supply(cache)
    assert (await container.resolve(NeedsOptionalCache)).cache is cache


async def test_a_child_scope_shares_its_parent_singletons():
    root = Container()
    engine = await root.resolve(Engine)
    assert await root.create_scope().resolve(Engine) is engine


async def test_start_constructs_eagerly():
    container = Container()
    container.bind(Engine)
    await container.start()
    assert isinstance(container.get(Engine), Engine)


async def test_start_surfaces_a_wiring_failure():
    container = Container()

    def broken() -> Engine:
        raise RuntimeError("no DSN")

    container.register(factory(Engine, broken))
    with pytest.raises(RuntimeError, match="no DSN"):
        await container.start()


async def test_supply_registers_by_type():
    container = Container()
    engine = Engine()
    container.supply(engine)
    assert await container.resolve(Engine) is engine


async def test_an_unannotated_parameter_is_reported():
    class Bad:
        def __init__(self, thing) -> None: ...

    with pytest.raises(errs.AppError, match="no type annotation"):
        await Container().resolve(Bad)


async def test_a_primitive_must_be_asked_for_by_token():
    class Bad:
        def __init__(self, dsn: str) -> None: ...

    with pytest.raises(errs.AppError, match="annotate it with"):
        await Container().resolve(Bad)


async def test_an_optional_union_is_recognised_on_every_python():
    """`X | None` reports a different origin before and after Python 3.14.

    Matching only one spelling treats the parameter as required and named
    `X | None`, which nothing provides.
    """
    import types
    import typing

    assert typing.get_origin(Cache | None) in (typing.Union, types.UnionType)
    assert not Container().needs_request_scope(NeedsOptionalCache)
    built = await Container().resolve(NeedsOptionalCache)
    assert built.cache is None
