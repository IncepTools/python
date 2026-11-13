# Dynamic modules

A module that reads its own configuration can only be configured one way per
process. A dynamic module takes its configuration from the importer, so the
same module can serve two databases, or a test can configure it differently
without touching it.

```python
CACHE_URL = Token("cache.url")


@Module(providers=[])
class CacheModule:
    @classmethod
    def for_root(cls, *, url: str) -> DynamicModule:
        return DynamicModule(
            cls,
            providers=[
                value(CACHE_URL, url),
                factory(Cache, build_cache, inject=[CACHE_URL]),
            ],
            exports=[Cache],
            is_global=True,
        )


@Module(imports=[CacheModule.for_root(url=settings.redis_url)])
class SearchModule: ...
```

Any classmethod returning a `DynamicModule` works; `for_root` and `for_feature`
are conventions, not requirements.

Importing the same dynamic module from two places registers it **once** — the
configurations merge rather than producing two instances of the client.

## Transitive registration

A module reached only through `imports` does not have to be listed on its
`@App`. Importing it is enough:

```python
@Module(providers=[SearchService], imports=[CacheModule.for_root(url=…)])
class SearchModule: ...

@App(modules=[SearchModule], name="search")
class SearchApp: ...        # CacheModule is registered, once
```

That is how a shared `ConfigModule` gets registered exactly once no matter how
many modules import it.

## Global modules

```python
@Global
@Module(providers=[value(SETTINGS, load(AppSettings))], exports=[SETTINGS])
class ConfigModule: ...
```

Its exports are visible everywhere, without an import.

Use it sparingly, and only for genuinely cross-cutting things — configuration,
a logger, a cache client. Every global module is a dependency that no longer
appears in any import list, so nothing records which modules actually use it.
Three global modules is a design; thirty is a service with no boundaries.

## Re-exporting a module

```python
@Module(providers=[Connection], exports=[Connection])
class ConnectionModule: ...

@Module(imports=[ConnectionModule], exports=[ConnectionModule])
class DatabaseModule: ...        # one import for consumers, two modules behind it

@Module(imports=[DatabaseModule])
class OrdersModule: ...          # can resolve Connection
```

Exporting a module you imported re-exports everything it exports, so an
aggregate module stays a single import for its consumers.

## Circular imports

Two modules that genuinely need each other cannot both import the other at
module scope:

```python
@Module(providers=[...], imports=[forward_ref(lambda: OrdersModule)])
class InvoicesModule: ...
```

`forward_ref` defers the lookup to the moment it is needed, by which time both
modules exist. A circular pair is usually a sign that a third module is hiding
inside them, so treat it as a question before reaching for this.
