# Metadata and the Reflector

A decorator records a decision; something else reads it back and acts. That is
how a single `RolesGuard` serves a hundred endpoints without knowing any of
them.

## Declaring

```python
from inceptools import SetMetadata

CACHE_TTL = "myapp:cache-ttl"

def CacheFor(seconds: int):
    return SetMetadata(CACHE_TTL, seconds)


@Controller("/reports")
class ReportController:
    @Get("/daily")
    @CacheFor(300)
    async def daily(self): ...
```

`Roles`, `Scopes` and `Public` are three of these shipped ready-made.

## Reading

```python
@Injectable()
class CachingInterceptor:
    def __init__(self, reflector: Reflector, cache: Cache) -> None:
        self.reflector = reflector
        self.cache = cache

    @property
    def name(self) -> str:
        return "caching"

    async def intercept(self, ctx, call_next):
        ttl = self.reflector.get_all_and_override(CACHE_TTL, ctx.handler, ctx.controller)
        if not ttl:
            return await call_next()
        ...
```

`Reflector` is injectable; ask for it and the container supplies it.

| Method | Use for |
|---|---|
| `get(key, target)` | one target |
| `get_all_and_override(key, handler, controller)` | a value that is a **decision** — one role set, one TTL. The most specific wins. |
| `get_all_and_merge(key, handler, controller)` | a value that is a **list of things to do** — every level contributes, outer first. |

Metadata lives on the function or class object, so a subclass never writes into
its parent's, and reading costs nothing at request time.

## Attaching behaviour

Four decorators, each usable on a **class** or a **method**. The two levels
merge, controller first:

```python
@Controller("/invoices")
@UseGuards(RolesGuard)
@UseInterceptors(AuditInterceptor)
@Roles("billing:read")
class InvoiceController:
    @Post("/")
    @Roles("billing:write")          # overrides the controller's
    @UsePipes(Trim())
    async def create(self, body: CreateInvoice): ...

    @Get("/ping")
    @Public()                        # a global guard stands down
    async def ping(self): ...
```

A method-level `@UseGuards` **adds to** the controller's rather than replacing
it, so a controller-wide rule cannot be skipped by adding a narrower one.

Every entry may be a class or an instance. A class is constructed by the
container at boot, which is the only way a guard can reach a dependency — and
means a missing provider fails the deploy, not the first guarded request.

## Order

```
global (APP_GUARD / create_app) → controller @UseGuards → route guards=[…]
```

Interceptors follow the same order and run **inside** the guards. They see the
rendered response, so an interceptor can set a header or replace the body.

## Global bindings with dependencies

Passing a guard to `create_app` suits a guard that needs nothing. One with
dependencies goes through a token, and any module may register it:

```python
@Module(
    providers=[AuthGuard, factory(APP_GUARD, lambda g: [g], inject=[AuthGuard])],
)
class SecurityModule: ...
```

`APP_GUARD`, `APP_INTERCEPTOR`, `APP_FILTER` and `APP_PIPE`. All registrations
are collected, from every module.

## Discovery

For the pattern where a decorator marks methods and something at boot has to
find all of them:

```python
for found in DiscoveryService(app).handlers_with("myapp:job"):
    scheduler.add(Job(run=found.method, **found.metadata["myapp:job"]))
```

Without it, each module registers its own — and the module that forgets is the
one whose jobs never run.
