# Providers

A provider is anything the container can hand to a constructor: a service, a
repository, a client, a settings object, a plain value.

## The four kinds

```python
from inceptools import Token, class_provider, existing, factory, value

SETTINGS = Token("app.settings")

@Module(
    providers=[
        ProductService,                                   # a class
        value(SETTINGS, {"region": "ap-south-1"}),         # an existing object
        factory(Engine, build_engine, inject=[SETTINGS]),  # a callable
        existing(MAILER, SesMailer),                       # another name for one
    ],
    exports=[ProductService, MAILER],
)
class ProductsModule: ...
```

`existing` is an alias: one instance under two names, for when an interface
token and a concrete class must both resolve to the same object.

A factory may be sync, async, or a generator — in which case the part after
`yield` runs during teardown:

```python
async def build_engine(settings: dict) -> AsyncEngine:
    engine = create_async_engine(settings["dsn"])
    yield engine
    await engine.dispose()
```

## Injection

Dependencies come from type hints:

```python
@Injectable()
class ProductService:
    def __init__(self, repo: ProductRepo, cache: Cache | None = None) -> None:
        self.repo = repo
        self.cache = cache        # stays None when nothing provides it
```

A parameter whose type does not identify its provider — a `dict`, a `str`, or a
type several providers share — names it instead. Two equivalent forms:

```python
@Injectable()
class Mailer:
    def __init__(self, cfg: Annotated[dict, Inject(SETTINGS)]) -> None: ...


@Injectable()
@inject(cfg=SETTINGS)                     # keeps a long constructor readable
class Billing:
    def __init__(self, cfg: dict) -> None: ...
```

Python has no parameter decorators, so those are the two ways to say it.

A `str` or `int` parameter with no name and no default is refused at boot
rather than injected by accident — configuration is not a dependency.

## Scopes

| Scope | Lifetime |
|---|---|
| `Scope.SINGLETON` (default) | One per process. Lives in the highest container that knows it, so a request scope cannot fork a connection pool. |
| `Scope.REQUEST` | One per request, job or message. Disposed when it ends. |
| `Scope.TRANSIENT` | A new one every resolution. Never cached, never disposed. |

### Scope bubbles

```python
@Injectable(scope=Scope.REQUEST)
class RequestTrace: ...

@Injectable()                        # declared singleton…
class Auditor:
    def __init__(self, trace: RequestTrace) -> None: ...
```

`Auditor` is built per request anyway, and so is any controller that depends on
it. It has to be: a singleton holding a request-scoped object would capture the
first request's instance and serve it to every later one, and nothing would
report it.

Use request scope sparingly. It is the right answer for per-request identity or
a per-request unit of work, and the wrong one for anything expensive.

## Eager construction

Every singleton is constructed at startup, before the socket opens, so a
missing environment variable or an unreachable dependency fails the deploy
rather than the first request that needed it.

Teardown runs in reverse **construction** order, which is why a repository is
always disposed before the engine it borrowed sessions from.

## ModuleRef

```python
@Injectable()
class Dispatcher:
    def __init__(self, ref: ModuleRef) -> None:
        self.ref = ref

    async def handle(self, kind: str) -> None:
        handler = await self.ref.resolve(HANDLERS[kind])
        await handler.run()
```

`get` for something already built, `resolve` to build it, `create` to construct
a class the container does not own, `scope` for a child container.

Reach for it in three cases only: a dependency chosen by a value known at
request time, a circular pair that genuinely cannot be broken, and
instantiating a strategy object. Everything else should be a constructor
parameter — `ModuleRef` hides the dependency from the graph, so a missing
provider becomes a runtime failure instead of a boot failure.
