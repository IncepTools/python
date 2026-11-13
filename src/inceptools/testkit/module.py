"""A testing module builder, for replacing providers without touching the app.

    app = (
        TestingModule(CatalogApp)
        .override(ProductRepo).use_value(FakeProductRepo())
        .override(Cache).use_class(InMemoryCache)
        .override_guard(guard.Tenanted)
        .compile()
    )

The override is registered on the **root** container, which every module
resolves through, so it wins wherever the real provider was declared. Nothing
in the application under test changes — which is the point: a test that has to
restructure the thing it is testing is testing something else.
"""

from __future__ import annotations

from typing import Any

from inceptools.application import Application, Options, create_app
from inceptools.core.di import Provider, Scope, class_provider, factory, value
from inceptools.web import middleware as middleware_mod

__all__ = ["Override", "TestingModule"]


class Override:
    """One pending replacement. Returned by :meth:`TestingModule.override`."""

    __slots__ = ("_builder", "_token")

    def __init__(self, builder: TestingModule, token: Any) -> None:
        self._builder = builder
        self._token = token

    def use_value(self, instance: Any) -> TestingModule:
        return self._builder._record(value(self._token, instance))

    def use_class(self, cls: type, *, scope: Scope = Scope.SINGLETON) -> TestingModule:
        return self._builder._record(class_provider(self._token, cls, scope=scope))

    def use_factory(self, fn: Any, *, inject: list[Any] | None = None) -> TestingModule:
        return self._builder._record(factory(self._token, fn, inject=inject))


class TestingModule:
    """Assembles an application with providers replaced."""

    def __init__(self, *apps: Any, options: Options | None = None, **overrides: Any) -> None:
        self._apps = apps
        self._providers: list[Provider] = []
        self._guard_overrides: list[Any] = []

        if options is None:
            from inceptools.testkit import identity_from_headers

            defaults: dict[str, Any] = {
                "service": "test",
                "configure_logging": False,
                "middleware": middleware_mod.ChainOptions(identity=identity_from_headers),
            }
            defaults.update(overrides)
            options = Options(**defaults)
        self._options = options

    def _record(self, provider: Provider) -> TestingModule:
        self._providers.append(provider)
        return self

    def override(self, token: Any) -> Override:
        """Replace whatever provides *token*."""
        return Override(self, token)

    def provide(self, *providers: Provider | type) -> TestingModule:
        """Add a provider the application does not have."""
        from inceptools.core.module import as_provider

        self._providers.extend(as_provider(p) for p in providers)
        return self

    def override_guard(self, target: Any, *, allow: bool = True) -> TestingModule:
        """Neuter a guard for this test.

        Use it to isolate the handler under test, never to make a failing
        authorization test pass: the guards are the part most worth testing.
        """
        self._guard_overrides.append((target, allow))
        return self

    def build(self) -> Application:
        app = create_app(self._options)
        for provider in self._providers:
            app.container.register(provider)
        for target, allow in self._guard_overrides:
            _replace_guard(target, allow)
        if self._apps:
            app.mount(*self._apps)
        return app

    def compile(self) -> Any:
        """The application wrapped in a :class:`~inceptools.testkit.TestApp`."""
        from inceptools.testkit import TestApp

        return TestApp(self.build())


def _replace_guard(target: Any, allow: bool) -> None:
    """Swap a guard instance's check in place.

    Only possible for the built-in guard instances, which are
    :class:`~inceptools.web.guard.GuardFunc` values shared by every route that
    names them. A guard *class* is overridden with ``override()`` instead.
    """
    from inceptools.core import errs
    from inceptools.web.guard import GuardFunc

    if not isinstance(target, GuardFunc):
        raise errs.internal(
            f"{target!r} is not a guard instance; override a guard class with "
            ".override(TheGuard).use_value(...)"
        )

    def check(ctx: Any) -> None:
        if not allow:
            raise errs.forbidden("refused by the test").with_code("TEST_REFUSED")

    target._check = check
