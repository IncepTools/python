"""Lazy, typed providers with cycle detection and ordered teardown.

Dependencies are declared on a constructor and resolved from type hints — no
service locator, no import-time globals, no ``get_db()`` reaching into a
module-level singleton.

Three guarantees:

* **A cycle is a boot failure**, naming the ring, not a recursion error at 2am.
* **Singletons can be constructed eagerly**, so a missing dependency fails the
  deploy rather than the first request that needed it.
* **Teardown runs in reverse construction order**, so a repository is disposed
  before the engine it holds a session from.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import types
import typing
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, TypeVar, get_args, get_origin

from inceptools.core import errs

__all__ = [
    "Container",
    "ForwardRef",
    "Inject",
    "Injectable",
    "ModuleRef",
    "Optional",
    "Provider",
    "Scope",
    "Token",
    "alias",
    "class_provider",
    "existing",
    "factory",
    "forward_ref",
    "inject",
    "is_injectable",
    "token_name",
    "value",
]

T = TypeVar("T")


class Scope(str, Enum):
    """How long an instance lives."""

    #: One instance for the process. The default, and what you almost always want.
    SINGLETON = "singleton"
    #: One instance per request scope. Use for anything carrying request identity.
    REQUEST = "request"
    #: A new instance on every resolution. Never cached, never disposed by the container.
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class Token:
    """A named provider key, for things that are not a class.

    A ``str`` is also accepted as a token everywhere; this type exists so a
    token can be exported as a constant and typo-checked by an editor.
    """

    name: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


TokenType = type | Token | str


@dataclass(frozen=True, slots=True)
class ForwardRef:
    """A token named lazily, to break an import cycle.

    Two modules that genuinely need each other cannot both import the other at
    module scope. ``forward_ref(lambda: OrdersModule)`` defers the lookup to
    the moment it is needed, by which time both modules exist.
    """

    resolve: Callable[[], Any]

    def get(self) -> Any:
        return self.resolve()


def forward_ref(resolve: Callable[[], Any]) -> ForwardRef:
    """``imports=[forward_ref(lambda: OrdersModule)]``."""
    return ForwardRef(resolve)


def deref(token: Any) -> Any:
    """Resolve a :class:`ForwardRef`, or return *token* unchanged."""
    return token.get() if isinstance(token, ForwardRef) else token


@dataclass(frozen=True, slots=True)
class Inject:
    """Annotate a parameter whose provider is keyed by something other than its type.

    def __init__(self, cfg: Annotated[Settings, Inject("app.settings")]) -> None:
    """

    token: TokenType


@dataclass(frozen=True, slots=True)
class Optional:
    """Annotate a parameter that may stay ``None`` when nothing provides it.

    def __init__(self, cache: Annotated[Cache | None, Optional()] = None) -> None:
    """

    default: Any = None


#: Parameter name -> token, written by :func:`inject` and read when the
#: container builds the class.
INJECT_ATTR = "__inceptools_inject__"


def inject(**tokens: TokenType) -> Callable[[type], type]:
    """Name the provider for one or more constructor parameters.

        @Injectable()
        @inject(settings=SETTINGS, cache="cache.redis")
        class ProductRepo:
            def __init__(self, settings: AppSettings, cache: Cache) -> None: ...

    The ``Annotated[X, Inject(TOKEN)]`` form does the same thing inline. This
    one keeps a long constructor readable, and is what you want when the
    parameter's type is shared by several providers.
    """

    def decorate(cls: type) -> type:
        merged = dict(getattr(cls, INJECT_ATTR, {}) or {})
        merged.update(tokens)
        setattr(cls, INJECT_ATTR, merged)
        return cls

    return decorate


# -- marking -------------------------------------------------------------

_INJECTABLE = "__inceptools_injectable__"


def Injectable(_cls: type | None = None, *, scope: Scope = Scope.SINGLETON) -> Any:
    """Mark a class as constructible by the container.

    Marking is not strictly required — any class with resolvable hints can be
    built — but it makes the intent greppable and carries the scope.
    """

    def decorate(cls: type) -> type:
        setattr(cls, _INJECTABLE, scope)
        return cls

    return decorate(_cls) if _cls is not None else decorate


def is_injectable(obj: Any) -> bool:
    return hasattr(obj, _INJECTABLE)


def _declared_scope(cls: type) -> Scope:
    return getattr(cls, _INJECTABLE, Scope.SINGLETON)


# -- providers -----------------------------------------------------------


@dataclass
class Provider:
    """How to build one token."""

    token: TokenType
    scope: Scope = Scope.SINGLETON
    use_class: type | None = None
    use_value: Any = None
    has_value: bool = False
    use_factory: Callable[..., Any] | None = None
    #: Another token this one is an alias for.
    use_existing: TokenType | None = None
    #: Explicit tokens for a factory's parameters. Falls back to type hints.
    inject: tuple[TokenType, ...] | None = None

    def __post_init__(self) -> None:
        provided = sum(
            (
                self.use_class is not None,
                self.has_value,
                self.use_factory is not None,
                self.use_existing is not None,
            )
        )
        if provided != 1:
            raise ValueError(
                f"provider for {token_name(self.token)} must set exactly one of "
                "use_class, use_value, use_factory or use_existing"
            )


def class_provider(
    token: TokenType, use_class: type | None = None, *, scope: Scope | None = None
) -> Provider:
    """Provide *token* by constructing a class.

    With one argument the class is both the token and the implementation.
    """
    impl = use_class or token
    if not isinstance(impl, type):
        raise TypeError(f"use_class for {token_name(token)} must be a class")
    return Provider(token=token, use_class=impl, scope=scope or _declared_scope(impl))


def value(token: TokenType, use_value: Any) -> Provider:
    """Provide an already-constructed object (a settings object, a pool, a client)."""
    return Provider(token=token, use_value=use_value, has_value=True, scope=Scope.SINGLETON)


def factory(
    token: TokenType,
    use_factory: Callable[..., Any],
    *,
    inject: Iterable[TokenType] | None = None,
    scope: Scope = Scope.SINGLETON,
) -> Provider:
    """Provide *token* from a callable.

    The callable may be sync or async, and may be a generator/async-generator —
    in which case the part after ``yield`` runs during teardown, like a
    FastAPI dependency.
    """
    return Provider(
        token=token,
        use_factory=use_factory,
        inject=tuple(inject) if inject is not None else None,
        scope=scope,
    )


def existing(token: TokenType, target: TokenType) -> Provider:
    """Provide *token* as another name for *target*.

    One instance under two names — useful when an interface token and a
    concrete class must both resolve to the same object.
    """
    return Provider(token=token, use_existing=target, scope=Scope.SINGLETON)


#: Both names are exported; ``alias`` reads better at a call site.
alias = existing


def token_name(token: TokenType) -> str:
    if isinstance(token, type):
        return token.__name__
    return str(token)


# -- resolution ----------------------------------------------------------


@dataclass
class _Dep:
    name: str
    token: TokenType
    optional: bool
    default: Any


class Container:
    """A provider registry and the instances it has built.

    A *child* container (see :meth:`create_scope`) resolves singletons from its
    parent and keeps request-scoped instances of its own, so a request scope
    never duplicates a connection pool.
    """

    def __init__(self, parent: Container | None = None, *, name: str = "root") -> None:
        self._parent = parent
        self._name = name
        #: Containers this one may borrow from, with the token keys each
        #: exposes. This is what makes a module boundary real: a token a
        #: module did not export is simply not reachable from here.
        self._links: list[tuple[Container, frozenset[Any]]] = []
        self._request_scoped_cache: dict[Any, bool] = {}
        self._providers: dict[Any, Provider] = {}
        self._instances: dict[Any, Any] = {}
        #: Construction order, for reverse teardown.
        self._built: list[Any] = []
        self._finalizers: list[Callable[[], Awaitable[None]]] = []
        self._resolving: list[Any] = []
        self._lock = asyncio.Lock()
        self._closed = False

    # -- registration ----------------------------------------------------

    def register(self, provider: Provider) -> Container:
        """Add a provider. A later registration for the same token wins."""
        self._providers[_key(deref(provider.token))] = provider
        return self

    def register_many(self, providers: Iterable[Provider]) -> Container:
        for p in providers:
            self.register(p)
        return self

    def supply(self, token: TokenType, instance: Any = None) -> Container:
        """Register an already-built object.

        ``supply(engine)`` keys it on its own class; ``supply("db", engine)``
        keys it on a name.
        """
        if instance is None:
            instance = token
            token = type(instance)
        return self.register(value(token, instance))

    def bind(self, cls: type, *, scope: Scope | None = None) -> Container:
        """Register a class as its own token."""
        return self.register(class_provider(cls, scope=scope))

    def link(self, other: Container, tokens: Iterable[TokenType]) -> Container:
        """Expose *tokens* from *other* to this container.

        Used for module imports: an importer may resolve exactly what the
        imported module exported, and nothing else.
        """
        self._links.append((other, frozenset(_key(deref(t)) for t in tokens)))
        return self

    def _linked_for(self, key: Any) -> Container | None:
        for other, exposed in self._links:
            if key in exposed:
                return other
        return None

    def has(self, token: TokenType) -> bool:
        key = _key(deref(token))
        if key in self._providers:
            return True
        if self._linked_for(key) is not None:
            return True
        return self._parent.has(token) if self._parent else False

    def provider_for(self, token: TokenType) -> Provider | None:
        key = _key(deref(token))
        found = self._providers.get(key)
        if found is not None:
            return found
        linked = self._linked_for(key)
        if linked is not None:
            return linked.provider_for(token)
        return self._parent.provider_for(token) if self._parent else None

    def _request_scoped(self, token: TokenType) -> bool:
        """:meth:`needs_request_scope`, memoised — it is asked per resolution."""
        key = _key(deref(token))
        cached = self._request_scoped_cache.get(key)
        if cached is None:
            cached = self.needs_request_scope(token)
            self._request_scoped_cache[key] = cached
        return cached

    def needs_request_scope(self, token: TokenType, _seen: set[Any] | None = None) -> bool:
        """True when *token*'s dependency graph contains a request-scoped provider.

        Scope bubbles: a provider that depends — at any depth — on something
        request-scoped must itself be built per request, or it would capture
        the first request's instance and serve it to every later one.
        """
        seen = _seen if _seen is not None else set()
        key = _key(deref(token))
        if key in seen:
            return False
        seen.add(key)

        provider = self.provider_for(token)
        if provider is None:
            return False
        if provider.scope is Scope.REQUEST:
            return True

        if provider.use_existing is not None:
            return self.needs_request_scope(provider.use_existing, seen)

        target = provider.use_class
        if target is None and provider.use_factory is not None:
            if provider.inject:
                return any(self.needs_request_scope(t, seen) for t in provider.inject)
            deps = _dependencies_of_callable(provider.use_factory)
            return any(self.needs_request_scope(d.token, seen) for d in deps)
        if target is None:
            return False

        try:
            deps = _dependencies_of(target)
        except errs.AppError:
            return False
        return any(self.needs_request_scope(dep.token, seen) for dep in deps)

    def own_tokens(self) -> list[TokenType]:
        """Tokens registered on *this* container, excluding ancestors and links."""
        return [p.token for p in self._providers.values()]

    def __contains__(self, token: Any) -> bool:
        """``token in container`` — registered here, not inherited."""
        return _key(deref(token)) in self._providers

    def instances(self) -> list[Any]:
        """Everything this container has built, in construction order."""
        return [self._instances[key] for key in self._built if key in self._instances]

    def tokens(self) -> list[TokenType]:
        """Every token registered here and in ancestors."""
        seen: dict[Any, TokenType] = {}
        if self._parent:
            for t in self._parent.tokens():
                seen[_key(t)] = t
        for k, p in self._providers.items():
            seen[k] = p.token
        return list(seen.values())

    # -- resolution ------------------------------------------------------

    async def resolve(self, token: type[T] | TokenType) -> T:
        """Build or fetch the instance for *token*."""
        if self._closed:
            raise errs.internal(f"container {self._name!r} is closed")
        return await self._resolve(token, [])

    async def try_resolve(self, token: type[T] | TokenType, default: Any = None) -> Any:
        if not self.has(token):
            return default
        return await self.resolve(token)

    def get(self, token: type[T] | TokenType) -> T:
        """The already-built instance for *token*.

        Synchronous, and therefore only valid after the container has been
        started — which is exactly when a route handler runs.
        """
        key = _key(token)
        if key in self._instances:
            return typing.cast(T, self._instances[key])
        if self._parent is not None:
            return self._parent.get(token)
        raise errs.internal(
            f"{token_name(token)} has not been constructed; "
            "await container.resolve() or start the application first"
        )

    async def _resolve(self, token: TokenType, stack: list[Any]) -> Any:
        token = deref(token)
        key = _key(token)

        if key in self._instances:
            return self._instances[key]

        provider = self._providers.get(key)
        if provider is None:
            inherited = self.provider_for(token)
            # A request-scoped graph is built *here*, even when an ancestor
            # declared it. Delegating upwards would return the instance that
            # ancestor cached — which, for a controller resolved once at boot,
            # is the boot-time instance for every later request.
            if inherited is not None and self._parent is not None and self._request_scoped(token):
                provider = inherited
            else:
                # An imported module owns it: resolve *there*, so the instance
                # is the one that module built and there is exactly one of it.
                linked = self._linked_for(key)
                if linked is not None:
                    return await linked._resolve(token, stack)
                if self._parent is not None and self._parent.has(token):
                    return await self._parent._resolve(token, stack)
        if provider is None:
            # An unregistered class with a resolvable constructor is built
            # implicitly; anything else is a wiring mistake worth naming.
            if isinstance(token, type):
                provider = class_provider(token)
                self._providers[key] = provider
            else:
                raise errs.internal(
                    f"no provider for {token_name(token)}"
                    + (f" (resolving {' -> '.join(_names(stack))})" if stack else "")
                )

        # A singleton whose graph reaches a request-scoped provider is not
        # really a singleton: hoisting it would capture the first request's
        # dependency and hand it to every later request. It is built here
        # instead, and cached only for this scope's lifetime.
        if (
            provider.scope is Scope.SINGLETON
            and self._parent is not None
            and not self._request_scoped(token)
        ):
            owner = self._singleton_owner(key)
            if owner is not self:
                return await owner._resolve(token, stack)

        if key in stack:
            ring = [*_names(stack[stack.index(key) :]), token_name(token)]
            raise errs.internal("dependency cycle: " + " -> ".join(ring))

        instance = await self._construct(provider, [*stack, key])

        if provider.scope is not Scope.TRANSIENT:
            self._instances[key] = instance
            self._built.append(key)
        return instance

    def _singleton_owner(self, key: Any) -> Container:
        """The highest container that knows this token — where a singleton lives."""
        if self._parent is not None and self._parent.provider_for_key(key) is not None:
            return self._parent._singleton_owner(key)
        return self

    def provider_for_key(self, key: Any) -> Provider | None:
        found = self._providers.get(key)
        if found is not None:
            return found
        linked = self._linked_for(key)
        if linked is not None:
            return linked.provider_for_key(key)
        return self._parent.provider_for_key(key) if self._parent else None

    async def _construct(self, provider: Provider, stack: list[Any]) -> Any:
        if provider.has_value:
            return provider.use_value

        if provider.use_existing is not None:
            return await self._resolve(deref(provider.use_existing), stack)

        if provider.use_factory is not None:
            fn = provider.use_factory
            args = await self._factory_args(provider, stack)
            return await self._call_and_register(fn, args)

        cls = provider.use_class
        assert cls is not None
        deps = _dependencies_of(cls)
        named = getattr(cls, INJECT_ATTR, None) or {}
        kwargs: dict[str, Any] = {}
        for dep in deps:
            if dep.name in named:
                dep = _Dep(
                    name=dep.name,
                    token=named[dep.name],
                    optional=dep.optional,
                    default=dep.default,
                )
            kwargs[dep.name] = await self._resolve_dep(dep, stack)
        instance = cls(**kwargs)
        if inspect.isawaitable(instance):  # pragma: no cover - unusual but legal
            instance = await instance
        return instance

    async def _factory_args(self, provider: Provider, stack: list[Any]) -> dict[str, Any]:
        fn = provider.use_factory
        assert fn is not None
        if provider.inject is not None:
            params = list(inspect.signature(fn).parameters)
            if len(provider.inject) > len(params):
                raise errs.internal(
                    f"factory for {token_name(provider.token)} declares "
                    f"{len(provider.inject)} injected tokens for {len(params)} parameters"
                )
            return {
                name: await self._resolve(tok, stack)
                for name, tok in zip(params, provider.inject, strict=False)
            }
        return {
            dep.name: await self._resolve_dep(dep, stack) for dep in _dependencies_of_callable(fn)
        }

    async def _resolve_dep(self, dep: _Dep, stack: list[Any]) -> Any:
        if dep.optional and not self.has(dep.token):
            return dep.default
        try:
            return await self._resolve(dep.token, stack)
        except errs.AppError:
            if dep.optional:
                return dep.default
            raise

    async def _call_and_register(self, fn: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
        """Call a provider factory, honouring generator-style teardown."""
        if inspect.isasyncgenfunction(fn):
            agen = fn(**kwargs)
            instance = await agen.__anext__()

            async def close_async() -> None:
                with contextlib.suppress(StopAsyncIteration):
                    await agen.__anext__()

            self._finalizers.append(close_async)
            return instance

        if inspect.isgeneratorfunction(fn):
            gen = fn(**kwargs)
            instance = next(gen)

            async def close_sync() -> None:
                with contextlib.suppress(StopIteration):
                    next(gen)

            self._finalizers.append(close_sync)
            return instance

        result = fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Construct every eager singleton.

        Called before the socket opens, so a missing environment variable or an
        unreachable dependency fails the deploy, not the first request.
        """
        for provider in list(self._providers.values()):
            if provider.scope is Scope.SINGLETON:
                # The instance may be recorded on an ancestor rather than here:
                # a singleton lives in the highest container that knows it, so
                # a request scope cannot fork a connection pool.
                await self._resolve(provider.token, [])

    def create_scope(self, name: str = "request") -> Container:
        """A child container for one request, job, or message."""
        return Container(parent=self, name=name)

    async def aclose(self) -> None:
        """Dispose everything this container built, newest first."""
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []

        for finalize in reversed(self._finalizers):
            try:
                await finalize()
            except Exception as exc:
                errors.append(exc)
        self._finalizers.clear()

        for key in reversed(self._built):
            instance = self._instances.get(key)
            try:
                await _dispose(instance)
            except Exception as exc:
                errors.append(exc)

        self._built.clear()
        self._instances.clear()
        if errors:
            raise errs.internal(f"{len(errors)} provider(s) failed to dispose").with_cause(
                errors[0]
            )

    def __repr__(self) -> str:
        return f"<Container {self._name!r} providers={len(self._providers)}>"

    async def __aenter__(self) -> Container:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


async def _dispose(instance: Any) -> None:
    for name in ("on_module_destroy", "aclose", "close", "dispose", "shutdown"):
        hook = getattr(instance, name, None)
        if hook is None or not callable(hook):
            continue
        result = hook()
        if inspect.isawaitable(result):
            await result
        return


def _key(token: TokenType) -> Any:
    if isinstance(token, Token):
        return ("token", token.name)
    if isinstance(token, str):
        return ("token", token)
    return token


def _names(stack: Iterable[Any]) -> list[str]:
    out = []
    for key in stack:
        if isinstance(key, tuple) and key and key[0] == "token":
            out.append(str(key[1]))
        elif isinstance(key, type):
            out.append(key.__name__)
        else:  # pragma: no cover
            out.append(str(key))
    return out


_PRIMITIVES = {str, int, float, bool, bytes, type(None), dict, list, tuple, set}


def _dependencies_of(cls: type) -> list[_Dep]:
    init = cls.__init__  # type: ignore[misc]
    if init is object.__init__:
        return []
    named = getattr(cls, INJECT_ATTR, None) or {}
    return _dependencies_of_callable(init, owner=cls, skip_self=True, named=tuple(named))


def _dependencies_of_callable(
    fn: Callable[..., Any],
    *,
    owner: type | None = None,
    skip_self: bool = False,
    named: tuple[str, ...] = (),
) -> list[_Dep]:
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except NameError as exc:
        # The module uses `from __future__ import annotations`, so every
        # annotation is a string, and one of them names something that is not
        # importable at module level. Nothing downstream can resolve it, and a
        # string silently becomes an unfindable provider token. Say so here.
        where = f"{owner.__name__}.__init__" if owner else getattr(fn, "__qualname__", str(fn))
        raise errs.internal(
            f"cannot resolve the type annotations of {where}: {exc}. "
            "Annotations in this module are strings; move the type to module "
            "level so it can be imported."
        ).with_cause(exc) from exc
    except Exception:
        hints = getattr(fn, "__annotations__", {})

    sig = inspect.signature(fn)
    deps: list[_Dep] = []
    for index, (name, param) in enumerate(sig.parameters.items()):
        if skip_self and index == 0:
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue

        annotation = hints.get(name, param.annotation)
        token, optional, default = _token_from_annotation(annotation, param)

        if token is inspect.Parameter.empty:
            if name in named:
                # Its provider is named by @inject; the annotation is free to
                # describe the shape rather than identify the provider.
                deps.append(_Dep(name=name, token=name, optional=optional, default=default))
                continue
            if param.default is not inspect.Parameter.empty:
                continue
            where = f"{owner.__name__}.__init__" if owner else getattr(fn, "__qualname__", str(fn))
            raise errs.internal(
                f"{where} parameter {name!r} has no type annotation, so nothing can be injected"
            )

        if isinstance(token, type) and token in _PRIMITIVES and name not in named:
            # A plain str/int is configuration, not a dependency. Only inject
            # it when it was asked for by name.
            if param.default is not inspect.Parameter.empty:
                continue
            raise errs.internal(
                f"parameter {name!r} is a {token.__name__}; annotate it with "
                f"Annotated[{token.__name__}, Inject('token')], or name its "
                f"provider with @inject({name}=TOKEN)"
            )

        deps.append(_Dep(name=name, token=token, optional=optional, default=default))
    return deps


def _token_from_annotation(annotation: Any, param: inspect.Parameter) -> tuple[Any, bool, Any]:
    optional = param.default is not inspect.Parameter.empty
    default = param.default if optional else None

    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        base = args[0]
        token: Any = base
        for extra in args[1:]:
            if isinstance(extra, Inject):
                token = extra.token
            elif isinstance(extra, Optional):
                optional = True
                default = extra.default if extra.default is not None else default
            elif extra is Optional:  # bare marker
                optional = True
        annotation = token

    # X | None -> X, optional.
    #
    # Both spellings are needed: for `X | None`, get_origin returns
    # types.UnionType up to Python 3.13 and typing.Union from 3.14, where the
    # two were unified. Checking only one silently treats an optional
    # dependency as a required one named `X | None`, which no provider has.
    origin = get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        members = [a for a in get_args(annotation) if a is not type(None)]
        if len(members) == 1:
            optional = True
            annotation = members[0]

    if annotation is inspect.Parameter.empty:
        return inspect.Parameter.empty, optional, default
    return annotation, optional, default


class ModuleRef:
    """Runtime access to the container, for the cases constructor injection
    cannot cover.

    Three of them, and they are the only good reasons to reach for this:

    * a dependency chosen by a value only known at request time;
    * a circular pair that genuinely cannot be broken;
    * instantiating a class the container does not own (a strategy object).

    Everything else should be a constructor parameter. ``ModuleRef`` hides the
    dependency from the graph, so a missing provider becomes a runtime failure
    instead of a boot failure — which is exactly the trade this framework
    exists to avoid making by accident.
    """

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        return self._container

    def get(self, token: type[T] | TokenType) -> T:
        """An already-constructed singleton. Raises if it was never built."""
        return self._container.get(token)

    async def resolve(self, token: type[T] | TokenType) -> T:
        """Build or fetch *token*, constructing it if needed."""
        return await self._container.resolve(token)

    async def try_resolve(self, token: type[T] | TokenType, default: Any = None) -> Any:
        return await self._container.try_resolve(token, default)

    def has(self, token: TokenType) -> bool:
        return self._container.has(token)

    async def create(self, cls: type[T]) -> T:
        """Construct *cls* with injected dependencies, without registering it.

        For a strategy or handler class the container should not keep alive.
        """
        scope = self._container.create_scope(f"create:{cls.__name__}")
        scope.register(class_provider(cls, scope=Scope.TRANSIENT))
        return await scope.resolve(cls)

    def scope(self, name: str = "request") -> Container:
        """A child container, for one request, job or message."""
        return self._container.create_scope(name)

    def __repr__(self) -> str:
        return f"<ModuleRef {self._container!r}>"
