# Modules and dependency injection

## Three levels, and no more

```
App       a bounded domain       catalog, billing, identity
Module    one resource in it     products, invoices, sessions
Layer     controller / service / repository / dto / entity
```

## A module declares its surface

```python
@Module(
    controllers=[ProductController],
    providers=[ProductService, ProductRepo],
    imports=[PricingModule],
    exports=[ProductService],
    entities=[Product],
    health_checks=[...],
)
class ProductsModule: ...
```

- `providers` are **private** unless also listed in `exports`.
- `imports` names the modules whose exports this one may resolve.
- Anything else is **not reachable** — asking for it fails at boot, naming both
  modules.

Each module builds in its own container, linked to exactly what its imports
expose. There is still one instance of an exported provider no matter how many
modules import it: the importer resolves it *in the owning module*.

See [Dynamic modules](dynamic-modules.md) for `for_root`, `@Global`,
re-exports and circular imports.

## Two-phase build

Every module's providers are registered before any module is built. Two modules
may therefore depend on each other's exports regardless of the order they are
listed in. A genuine cycle is reported at boot with the ring named:

```
dependency cycle: ProductService -> PricingService -> ProductService
```

## Constructor injection

```python
@Injectable
class ProductService:
    def __init__(self, repo: ProductRepo, cache: Cache | None = None) -> None:
        self.repo = repo
        self.cache = cache     # optional: stays None when nothing provides it
```

Dependencies come from type hints. Nothing is resolved by name at runtime.

### Scopes

| Scope | Lifetime |
|---|---|
| `Scope.SINGLETON` (default) | One per process. A singleton lives in the highest container that knows it, so a request scope cannot fork a connection pool. |
| `Scope.REQUEST` | One per request scope. |
| `Scope.TRANSIENT` | A new one on every resolution. Never cached, never disposed. |

### Providing something that is not a class

```python
from inceptools.core.di import Token, factory, value

SETTINGS = Token("app.settings")

app.provide(value(SETTINGS, load(AppSettings)))
app.provide(factory(Engine, build_engine))          # sync, async, or a generator

class Repo:
    def __init__(self, cfg: Annotated[AppSettings, Inject(SETTINGS)]) -> None: ...
```

A generator factory's code after `yield` runs during teardown:

```python
async def build_engine() -> AsyncEngine:
    engine = create_async_engine(dsn)
    yield engine
    await engine.dispose()
```

### Eager construction

`app.start()` constructs every singleton before the socket opens, so a missing
environment variable or an unreachable dependency fails the deploy rather than
the first request that needed it. Teardown runs in **reverse construction
order**, so a repository is disposed before the engine it borrows sessions from.

Full detail in [Providers and injection](providers.md).

## A note on string annotations

If your module uses `from __future__ import annotations` — most do — every
annotation is a string at runtime. A type named by a string must be importable
at module level. A model defined *inside a function* cannot be, and the
framework says so at mount time rather than answering `400 body is required`
forever.
