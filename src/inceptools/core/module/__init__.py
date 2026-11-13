"""Apps, modules, and the two-phase build.

Three levels, and no more::

    App       a bounded domain       catalog, billing, identity
    Module    one resource in it     products, invoices, sessions
    Layer     controller / service / repository / dto / entity

A module owns exactly one resource and declares what it *provides*, *exports*,
*imports*, routes, owns and needs checked.

Encapsulation is real, not advisory. Each module builds in its own child
container: its providers are private, and only what it lists in ``exports``
becomes resolvable by a module that ``imports`` it. A module reaching into
another's service therefore fails at boot with a message naming both modules,
rather than compiling fine and coupling two teams forever.

Registration is two-phase — **every** module's providers are registered before
**any** module is built — so two modules may depend on each other's exports
regardless of the order they are listed in. A genuine cycle is reported by the
container, with the ring named.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from inceptools.core import errs
from inceptools.core.di import Container, Provider, Scope, class_provider, deref, token_name

__all__ = [
    "App",
    "AppDescriptor",
    "BuiltModule",
    "DynamicModule",
    "Global",
    "Module",
    "ModuleDescriptor",
    "app_descriptor_of",
    "as_provider",
    "descriptor_of",
    "is_app",
    "is_global",
    "is_module",
    "modules_of",
    "owned_entities",
    "seed_all",
]

_MODULE_ATTR = "__inceptools_module__"
_APP_ATTR = "__inceptools_app__"


@dataclass(slots=True)
class ModuleDescriptor:
    """What a module declares about itself."""

    name: str
    cls: type | None = None
    #: Controller classes. Constructed through the module's container, so a
    #: controller gets its service by constructor injection.
    controllers: tuple[type, ...] = ()
    #: Classes or :class:`~inceptools.core.di.Provider` values. Private unless
    #: also listed in :attr:`exports`.
    providers: tuple[Any, ...] = ()
    #: Modules whose exports this one may resolve.
    imports: tuple[Any, ...] = ()
    #: The subset of :attr:`providers` other modules may resolve.
    exports: tuple[Any, ...] = ()
    #: ORM entities this module owns. Nothing outside it should query them.
    entities: tuple[Any, ...] = ()
    guards: tuple[Any, ...] = ()
    interceptors: tuple[Any, ...] = ()
    health_checks: tuple[Any, ...] = ()
    #: Scheduled jobs, queue consumers and event subscribers this module owns.
    jobs: tuple[Any, ...] = ()
    subscribers: tuple[Any, ...] = ()
    #: Extra URL segment for this module. ``None`` adds nothing, which is the
    #: default: the controller's own prefix is the path, so
    #: ``@Controller("/products")`` inside ``ProductsModule`` serves
    #: ``/products`` and not ``/products/products``. Set it to nest a module
    #: under a segment of its own.
    path: str | None = None
    #: Idempotent seed routine, run by ``app.seed()``.
    seed: Callable[[], Awaitable[None] | None] | None = None

    @property
    def mount_path(self) -> str:
        return self.path or ""

    def __repr__(self) -> str:
        return f"<module {self.name}>"


@dataclass(slots=True)
class AppDescriptor:
    """A bounded domain: a name and the modules inside it."""

    name: str
    cls: type | None = None
    modules: tuple[Any, ...] = ()
    #: Providers shared by every module in this app.
    providers: tuple[Any, ...] = ()
    guards: tuple[Any, ...] = ()
    interceptors: tuple[Any, ...] = ()
    #: URL segment for this domain. Defaults to :attr:`name`; pass ``""`` to
    #: mount the app's modules directly under the application prefix.
    path: str | None = None

    @property
    def mount_path(self) -> str:
        return self.name if self.path is None else self.path

    def __repr__(self) -> str:
        return f"<app {self.name}>"


@dataclass(slots=True)
class DynamicModule:
    """A module configured at import time.

        @Module()
        class CacheModule:
            @classmethod
            def for_root(cls, *, url: str) -> DynamicModule:
                return DynamicModule(
                    cls,
                    providers=[factory(Cache, lambda: Redis.from_url(url))],
                    exports=[Cache],
                    is_global=True,
                )

        @Module(imports=[CacheModule.for_root(url=settings.redis_url)])
        class AppModule: ...

    The alternative — a module that reads its own configuration — can only be
    configured one way per process. A dynamic module is what lets the same
    module serve two databases, or a test configure it differently.
    """

    module: Any
    providers: tuple[Any, ...] = ()
    controllers: tuple[type, ...] = ()
    imports: tuple[Any, ...] = ()
    exports: tuple[Any, ...] = ()
    #: Exports are visible everywhere, without being imported.
    is_global: bool = False

    def __post_init__(self) -> None:
        self.providers = tuple(self.providers)
        self.controllers = tuple(self.controllers)
        self.imports = tuple(self.imports)
        self.exports = tuple(self.exports)

    def __repr__(self) -> str:
        name = getattr(self.module, "__name__", self.module)
        return f"<dynamic {name}>"


_GLOBAL_ATTR = "__inceptools_global__"


def Global(cls: type) -> type:
    """Make a module's exports visible everywhere, without an import.

    Use it sparingly, and for genuinely cross-cutting things — configuration,
    a logger, a cache client. Every global module is a dependency that no
    longer appears in any import list, so nothing records which modules use it.
    """
    setattr(cls, _GLOBAL_ATTR, True)
    return cls


def is_global(obj: Any) -> bool:
    if isinstance(obj, DynamicModule):
        return obj.is_global or is_global(obj.module)
    return bool(getattr(obj, _GLOBAL_ATTR, False))


def Module(
    *,
    name: str = "",
    controllers: Sequence[type] = (),
    providers: Sequence[Any] = (),
    imports: Sequence[Any] = (),
    exports: Sequence[Any] = (),
    entities: Sequence[Any] = (),
    guards: Sequence[Any] = (),
    interceptors: Sequence[Any] = (),
    health_checks: Sequence[Any] = (),
    jobs: Sequence[Any] = (),
    subscribers: Sequence[Any] = (),
    path: str | None = None,
) -> Callable[[type], type]:
    """Declare a module.

    ``name`` defaults to the class name with a trailing ``Module`` stripped and
    lower-cased, so ``class ProductsModule`` is named ``products``. The name
    identifies the module in the route table, logs and lint output; it does
    **not** become a URL segment unless you pass ``path``.
    """

    def decorate(cls: type) -> type:
        resolved = name or _derive_name(cls.__name__, "Module")
        descriptor = ModuleDescriptor(
            name=resolved,
            cls=cls,
            controllers=tuple(controllers),
            providers=tuple(providers),
            imports=tuple(imports),
            exports=tuple(exports),
            entities=tuple(entities),
            guards=tuple(guards),
            interceptors=tuple(interceptors),
            health_checks=tuple(health_checks),
            jobs=tuple(jobs),
            subscribers=tuple(subscribers),
            path=path,
        )
        seed = getattr(cls, "seed", None)
        if callable(seed):
            descriptor.seed = seed
        setattr(cls, _MODULE_ATTR, descriptor)
        return cls

    return decorate


def App(
    *,
    name: str = "",
    modules: Sequence[Any] = (),
    providers: Sequence[Any] = (),
    guards: Sequence[Any] = (),
    interceptors: Sequence[Any] = (),
    path: str | None = None,
) -> Callable[[type], type]:
    """Declare a bounded domain holding several modules."""

    def decorate(cls: type) -> type:
        resolved = name or _derive_name(cls.__name__, "App")
        setattr(
            cls,
            _APP_ATTR,
            AppDescriptor(
                name=resolved,
                cls=cls,
                modules=tuple(modules),
                providers=tuple(providers),
                guards=tuple(guards),
                interceptors=tuple(interceptors),
                path=path,
            ),
        )
        return cls

    return decorate


def _derive_name(class_name: str, suffix: str) -> str:
    base = class_name.removesuffix(suffix) or class_name
    # CamelCase -> snake_case, so OrderItemsModule mounts at /order_items.
    out: list[str] = []
    for index, char in enumerate(base):
        if char.isupper() and index and not base[index - 1].isupper():
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def is_module(obj: Any) -> bool:
    return hasattr(obj, _MODULE_ATTR) or isinstance(obj, ModuleDescriptor)


def is_app(obj: Any) -> bool:
    return hasattr(obj, _APP_ATTR) or isinstance(obj, AppDescriptor)


def descriptor_of(obj: Any) -> ModuleDescriptor:
    """The descriptor for a module class, instance or descriptor."""
    if isinstance(obj, ModuleDescriptor):
        return obj
    found = getattr(obj, _MODULE_ATTR, None)
    if found is None:
        raise errs.internal(f"{_describe(obj)} is not a module; decorate it with @Module(...)")
    return found


def app_descriptor_of(obj: Any) -> AppDescriptor:
    if isinstance(obj, AppDescriptor):
        return obj
    found = getattr(obj, _APP_ATTR, None)
    if found is None:
        raise errs.internal(f"{_describe(obj)} is not an app; decorate it with @App(...)")
    return found


def _describe(obj: Any) -> str:
    return getattr(obj, "__name__", None) or type(obj).__name__


def modules_of(app: Any) -> list[ModuleDescriptor]:
    return [descriptor_of(m) for m in app_descriptor_of(app).modules]


# -- the two-phase build -------------------------------------------------


@dataclass(slots=True)
class BuiltModule:
    """One module, after its container exists and its controllers are built."""

    descriptor: ModuleDescriptor
    app: AppDescriptor
    container: Container
    controllers: list[Any] = field(default_factory=list)
    instance: Any = None

    @property
    def qualified_name(self) -> str:
        return f"{self.app.name}.{self.descriptor.name}"


def as_provider(item: Any) -> Provider:
    """Coerce a class or provider value into a :class:`Provider`."""
    if isinstance(item, Provider):
        return item
    if isinstance(item, type):
        return class_provider(item)
    raise errs.internal(
        f"{item!r} is not a provider; pass a class, or di.value()/di.factory()/"
        "di.class_provider()/di.existing()"
    )


#: Internal alias kept for callers that imported the private spelling.
_as_provider = as_provider


def _token_of(item: Any) -> Any:
    return item.token if isinstance(item, Provider) else deref(item)


@dataclass(slots=True)
class _Node:
    """One module in the flattened graph."""

    key: Any  # the module class, for identity
    descriptor: ModuleDescriptor
    extra_providers: tuple[Any, ...] = ()
    extra_controllers: tuple[type, ...] = ()
    extra_imports: tuple[Any, ...] = ()
    extra_exports: tuple[Any, ...] = ()
    is_global: bool = False
    app: AppDescriptor | None = None

    @property
    def providers(self) -> tuple[Any, ...]:
        return (*self.descriptor.providers, *self.extra_providers)

    @property
    def controllers(self) -> tuple[type, ...]:
        return (*self.descriptor.controllers, *self.extra_controllers)

    @property
    def imports(self) -> tuple[Any, ...]:
        return (*self.descriptor.imports, *self.extra_imports)

    @property
    def exports(self) -> tuple[Any, ...]:
        return (*self.descriptor.exports, *self.extra_exports)


def _node_for(item: Any) -> _Node:
    """Normalise a module class, instance or :class:`DynamicModule`."""
    if isinstance(item, DynamicModule):
        base = deref(item.module)
        return _Node(
            key=base,
            descriptor=descriptor_of(base),
            extra_providers=item.providers,
            extra_controllers=item.controllers,
            extra_imports=item.imports,
            extra_exports=item.exports,
            is_global=is_global(item),
        )
    resolved = deref(item)
    return _Node(
        key=resolved,
        descriptor=descriptor_of(resolved),
        is_global=is_global(resolved),
    )


def _flatten(apps: Sequence[AppDescriptor]) -> list[_Node]:
    """Every module reachable from the apps, including through imports.

    A module does not have to be listed on its app: importing it is enough,
    which is how a shared ``ConfigModule`` gets registered exactly once no
    matter how many modules import it.
    """
    nodes: dict[Any, _Node] = {}
    order: list[_Node] = []

    def visit(item: Any, app: AppDescriptor | None) -> _Node:
        node = _node_for(item)
        existing_node = nodes.get(node.key)
        if existing_node is not None:
            # Seen already. Merge a dynamic configuration onto it, and keep
            # the first app that claimed it so routes mount once.
            if isinstance(item, DynamicModule):
                existing_node.extra_providers += node.extra_providers
                existing_node.extra_controllers += node.extra_controllers
                existing_node.extra_imports += node.extra_imports
                existing_node.extra_exports += node.extra_exports
                existing_node.is_global = existing_node.is_global or node.is_global
            if existing_node.app is None:
                existing_node.app = app
            return existing_node

        node.app = app
        nodes[node.key] = node
        order.append(node)
        for imported in node.imports:
            visit(imported, None)
        return node

    for app in apps:
        seen_names: set[str] = set()
        for listed in app.modules:
            node = visit(listed, app)
            if node.descriptor.name in seen_names:
                # The second module's routes would be silently unreachable.
                raise errs.internal(
                    f"app {app.name!r} registers two modules named {node.descriptor.name!r}"
                )
            seen_names.add(node.descriptor.name)

    # A module reached only through an import still belongs to an app, for
    # naming and for route mounting (it usually has no routes).
    fallback = apps[0] if apps else None
    for node in order:
        if node.app is None:
            node.app = fallback
    return order


def register_modules(
    root: Container, apps: Sequence[Any]
) -> tuple[list[AppDescriptor], dict[Any, Container]]:
    """Phase one: give every module a container and register its providers.

    Nothing is constructed here. Registration completes for every module
    before any module is built, which is what lets two modules depend on each
    other's exports without caring who was listed first.
    """
    app_descriptors = [app_descriptor_of(a) for a in apps]
    nodes = _flatten(app_descriptors)

    for app in app_descriptors:
        for provider in app.providers:
            root.register(as_provider(provider))

    containers: dict[Any, Container] = {}
    by_key: dict[Any, _Node] = {node.key: node for node in nodes}

    for node in nodes:
        assert node.app is not None
        container = root.create_scope(f"{node.app.name}.{node.descriptor.name}")
        for provider in node.providers:
            container.register(as_provider(provider))
        for controller in node.controllers:
            container.register(class_provider(controller, scope=Scope.SINGLETON))
        containers[node.key] = container

    # Validate every module's exports now, not only the ones somebody
    # imports: a module that exports what it does not provide is broken
    # whether or not anyone has noticed yet.
    for node in nodes:
        _exported_tokens(node, by_key)

    _wire_imports(root, nodes, by_key, containers)
    return app_descriptors, containers


def _exported_tokens(
    node: _Node, by_key: dict[Any, _Node], seen: set[Any] | None = None
) -> list[Any]:
    """The tokens *node* exposes, following re-exported modules.

    Exporting a module you imported re-exports everything it exports. That is
    how an aggregate module ("DatabaseModule exports ConnectionModule") stays
    one import for its consumers.
    """
    seen = seen if seen is not None else set()
    if node.key in seen:
        return []
    seen.add(node.key)

    tokens: list[Any] = []
    own = {_key_of(p) for p in node.providers}

    for item in node.exports:
        resolved = deref(item)
        if isinstance(resolved, DynamicModule) or is_module(resolved):
            target = by_key.get(_node_for(resolved).key)
            if target is None:
                raise errs.internal(
                    f"module {node.descriptor.name!r} exports "
                    f"{getattr(resolved, '__name__', resolved)!r}, which it does not import"
                )
            tokens.extend(_exported_tokens(target, by_key, seen))
            continue

        token = _token_of(item)
        if _key_of(item) not in own:
            raise errs.internal(
                f"module {node.descriptor.name!r} exports {token_name(token)}, "
                "which it does not provide"
            )
        tokens.append(token)

    return tokens


def _key_of(item: Any) -> Any:
    """The identity a provider is registered under."""
    return _token_of(item)


def _wire_imports(
    root: Container,
    nodes: Sequence[_Node],
    by_key: dict[Any, _Node],
    containers: dict[Any, Container],
) -> None:
    """Link each module to exactly what its imports expose."""
    # Globals first: their exports go on the root, so every module sees them
    # without an import.
    for node in nodes:
        if not node.is_global:
            continue
        container = containers[node.key]
        for token in _exported_tokens(node, by_key):
            provider = container.provider_for(token)
            if provider is not None:
                root.register(provider)

    for node in nodes:
        container = containers[node.key]
        for imported in node.imports:
            target_node = by_key.get(_node_for(imported).key)
            if target_node is None:  # pragma: no cover - _flatten visits them all
                raise errs.internal(
                    f"module {node.descriptor.name!r} imports something that is "
                    "not registered in this application"
                )
            if target_node.key is node.key:
                raise errs.internal(f"module {node.descriptor.name!r} imports itself")
            container.link(containers[target_node.key], _exported_tokens(target_node, by_key))


async def build_modules(
    root: Container, apps: Sequence[AppDescriptor], containers: dict[Any, Container]
) -> list[BuiltModule]:
    """Phase two: construct each module's controllers from its own container."""
    built: list[BuiltModule] = []
    nodes = _flatten(apps)

    for node in nodes:
        assert node.app is not None
        container = containers[node.key]

        controllers: list[Any] = []
        for controller_cls in node.controllers:
            try:
                controllers.append(await container.resolve(controller_cls))
            except errs.AppError as exc:
                raise errs.internal(
                    f"module {node.app.name}.{node.descriptor.name}: building controller "
                    f"{controller_cls.__name__}: {exc.message}"
                ).with_cause(exc) from exc

        instance: Any = None
        if node.descriptor.cls is not None:
            try:
                instance = await container.resolve(node.descriptor.cls)
            except errs.AppError:
                # A module class is a declaration holder; it does not have to
                # be constructible.
                instance = None

        built.append(
            BuiltModule(
                descriptor=node.descriptor,
                app=node.app,
                container=container,
                controllers=controllers,
                instance=instance,
            )
        )

    return built


async def seed_all(built: Sequence[BuiltModule]) -> None:
    """Run every module's seed routine, in declaration order, stopping on the first failure."""
    import inspect as _inspect

    for item in built:
        seed = item.descriptor.seed
        if seed is None:
            continue
        target = seed
        if item.instance is not None and not _inspect.ismethod(seed):
            bound = getattr(item.instance, seed.__name__, None)
            if bound is not None:
                target = bound
        try:
            result = target()
            if _inspect.isawaitable(result):
                await result
        except Exception as exc:
            raise errs.internal(f"module {item.qualified_name}: seeding failed").with_cause(
                exc
            ) from exc


def owned_entities(built: Sequence[BuiltModule]) -> dict[str, list[Any]]:
    """``{"catalog.products": [Product, ...]}`` — who owns which table."""
    return {
        item.qualified_name: list(item.descriptor.entities)
        for item in built
        if item.descriptor.entities
    }


#: Public alias; :func:`_as_provider` is the internal spelling.
as_provider = _as_provider
