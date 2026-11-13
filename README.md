<div align="center">

# inceptools

**An opinionated application framework for Python services, on top of FastAPI.**

Modules, dependency injection, guards, interceptors, typed errors, one response
envelope, a scoped data layer, scheduling, caching, locks, idempotency and a
transactional outbox. A module system with real encapsulation, on top of
FastAPI, sharing its contracts with the [Go edition](https://github.com/IncepTools/go).

[![ci](https://github.com/IncepTools/python/actions/workflows/ci.yml/badge.svg)](https://github.com/IncepTools/python/actions/workflows/ci.yml)
[![docs](https://github.com/IncepTools/python/actions/workflows/docs.yml/badge.svg)](https://inceptools.github.io/python/)
[![PyPI](https://img.shields.io/pypi/v/inceptools.svg)](https://pypi.org/project/inceptools/)
[![Python 3.11–3.14](https://img.shields.io/badge/python-3.11%20%E2%80%93%203.14-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/IncepTools/python/blob/main/LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/IncepTools/python/badge)](https://scorecard.dev/viewer/?uri=github.com/IncepTools/python)

[Documentation](https://inceptools.github.io/python/) ·
[First steps](https://github.com/IncepTools/python/blob/main/docs/first-steps.md) ·
[Examples](https://github.com/IncepTools/python/tree/main/examples) ·
[Roadmap](https://github.com/IncepTools/python/blob/main/ROADMAP.md) ·
[Changelog](https://github.com/IncepTools/python/blob/main/CHANGELOG.md)

</div>

## In one minute

```python
from inceptools import App, Controller, Get, Module, Post, create_app, errs, guard

@Controller("/products", tags=["products"])
class ProductController:
    def __init__(self, service: ProductService) -> None:   # constructor injection
        self.service = service

    @Get("/{pid}", response_model=ProductOut)
    async def retrieve(self, pid: int):
        return await self.service.get(pid)                  # raises errs.not_found

    @Post("/", guards=[guard.Tenanted], response_model=ProductOut)
    async def create(self, body: CreateProduct):
        return await self.service.create(body)

@Module(controllers=[ProductController], providers=[ProductService], exports=[ProductService])
class ProductsModule: ...

@App(modules=[ProductsModule], name="catalog")
class CatalogApp: ...

app = create_app(service="shop", version="1.0.0", prefix="/api/v1")
app.mount(CatalogApp)
```

`app` is an ASGI application, so `uvicorn main:app` serves it and a Lambda
adapter can take it directly.

You now have `GET|POST /api/v1/catalog/products`, `/livez`, `/readyz`,
`/health`, a request id on every response, one JSON envelope on every reply,
recovered exceptions, access logging, graceful shutdown, and OpenAPI that
documents the envelope rather than a shape no client ever receives.

## Why

FastAPI is excellent and deliberately unopinionated. That is the right default
for one service and the wrong one for the twentieth, because by then every team
has answered the same questions differently: where business logic lives, what
an error looks like on the wire, who applies the ownership filter on a query,
whether a scheduled job runs on one replica or all of them.

inceptools answers them once.

| | |
|---|---|
| **An ownership filter you cannot forget** | A scoped repository applies it and *refuses* a query with no account on the context. Remembering `.where(account_id == ...)` in every query fails silently — by returning another customer's rows. |
| **One response shape, one leak check** | Every reply goes through one funnel, so "did a driver error just reach a client" is checked once rather than at four hundred call sites. |
| **Routes as data** | A controller declares its routes. The same declaration mounts them, prints them, documents them — and lets a test assert that every mutating route has a guard. |
| **Boot-time failure** | Config is loaded once and validated; providers are constructed eagerly before the socket opens. A missing environment variable fails the deploy, not the first request that needed it. |

## Install

```bash
uv add inceptools                 # core: FastAPI, Starlette, Pydantic
uv add "inceptools[sql]"          # the scoped repository and the outbox
uv add "inceptools[server,redis]" # uvicorn, and Redis-backed cache and locks
```

From this checkout:

```bash
cd python && uv sync --all-extras && uv run pytest
```

## The shape

Three levels, and no more:

```
App       a bounded domain       catalog, billing, identity
Module    one resource in it     products, invoices, sessions
Layer     controller / service / repository / dto / entity
```

A module owns exactly one resource and declares what it **provides**,
**exports**, **imports**, routes, owns and needs checked. Encapsulation is
real, not advisory: each module builds in its own child container, its
providers are private, and only what it lists in `exports` is resolvable by a
module that `imports` it.

Registration is two-phase — every module's providers are registered before any
module is built — so two modules may depend on each other's exports regardless
of the order they are listed in. A genuine cycle is reported at boot, with the
ring named.

The controller prefix is the path. A module adds no URL segment unless you
give it `path=`.

### Errors know their own status

A service never imports `fastapi`:

```python
async def get(self, pid: int) -> Product:
    product = await self.repo.get_by_id(pid)          # 404, already scoped
    if product.archived:
        raise errs.conflict("this product is archived").with_code("PRODUCT_ARCHIVED")
    return product
```

The exception filter catches whatever escaped, and the funnel decides what a
client may see:

```json
{
  "data": null,
  "meta": {
    "success": false,
    "statusCode": 409,
    "message": "this product is archived",
    "traceId": "0b2f…",
    "requestTime": 12,
    "route": "/api/v1/catalog/products/9c1e…",
    "code": "PRODUCT_ARCHIVED"
  }
}
```

An error that is **not** an `AppError` becomes a generic 500 and the original
goes to the log — because an unclassified error is by definition one nobody
checked for leaks. Field detail on an internal error is withheld too: it can
name a column.

Already returning a different envelope? `ResponseOptions(style=EnvelopeStyle.FLAT)`
renders `{data, errors, success, statusCode, requestTime, message, sessionId}`
instead, so a fleet can adopt the framework without changing its wire contract
on the same day.

### Routes are values

Because a route is a value rather than a side effect, this works:

```python
def test_every_write_is_guarded(client):
    assert client.routes.unguarded() == []
```

That is the cheapest security control here. The failure it catches — a new
write endpoint shipped without authentication — is both easy to commit and
expensive to find. A mutating route is either guarded or explicitly
`public=True`; there is no third state, because the third state is "nobody
checked".

```
METHOD  PATH                            MODULE            GUARDS
------  ------------------------------  ----------------  ------------------------------
GET     /api/v1/catalog/products        catalog.products  -
POST    /api/v1/catalog/products        catalog.products  tenanted
DELETE  /api/v1/catalog/products/{pid}  catalog.products  tenanted, role:<catalog:write>
```

### Validation says which field

```json
"errors": [
  { "field": "price",   "message": "price must be 0 or more",    "rule": "gte" },
  { "field": "tags[0]", "message": "tags[0] is required",        "rule": "required" }
]
```

Field names are the ones the client sent, and a nested path reads as
`rules[0].protocol`, not `body -> rules -> 0 -> protocol`. A DTO can implement
`normalize()`, `set_defaults()` and `validate_self()`; they run in that order,
so a default can satisfy `required` and a cross-field rule can assume the
individual fields are well-formed.

### The ownership filter is not yours to remember

```python
repo = Repository(Product, entity_name="product",
                  tenanted=True, sortable=("created_at", "price"))
```

Every read is filtered. Every create takes the account from the context, not
the payload. A request with no account is a 401, never "all accounts".
Reaching wider has to be spelled out:

```python
async with repo.unscoped():     # a nightly report across every account
    ...                         # deliberate, visible, greppable
```

`update_fields` refuses an empty condition, because an update with no `WHERE`
rewrites every row the account owns. It takes a **mapping** rather than a model
instance, because a model carries defaults for fields you did not set — so a
model-based update cannot tell "leave it alone" from "set it to false", and the
PATCH that disables something appears to succeed while changing nothing.

### Work that outlives the request

```python
@bus.subscribe(mode=events.Mode.ASYNC, name="audit")
async def record(event: OrderPlaced) -> None:
    await audit.write(event)
```

Async handlers get a **detached** context: it keeps the trace id and the
account and drops the cancellation. Handing a handler the request's task means
the audit write is cancelled the moment the client disconnects — and nothing
tells you.

When the effect must survive a crash, write it in the same transaction as the
state change:

```python
async with sqlx.transaction(session):
    await repo.create(order)
    await outbox.enqueue(table, "order.placed", order_payload)
```

`enqueue` *refuses* to run outside a transaction. Outside one it is just a
slower publish with the same hole in the middle.

### Scheduled jobs

```python
scheduler.add(Job(
    name="expire-abandoned-carts",
    cron="0 3 * * *",
    singleton=True,        # one replica, via the distributed lock
    timeout=600,
    jitter=30,
    run=service.expire_abandoned,
))
```

A malformed cron expression fails the boot rather than the night it was meant
to run. A run still going when the next is due is **skipped, not queued** —
queueing a slow job behind itself turns a slowdown into an unbounded backlog.

The five-field parser is written out rather than taken from a dependency: a
framework's dependencies become every importer's.

## Testing

```python
def test_create_requires_an_account(client):
    client.post("/api/v1/catalog/products", {"name": "ab"}) \
          .expect_status(401).expect_code("NO_TENANT")
```

These go through the real middleware, the real guards and the real exception
filter — which is where most of this framework's behaviour lives, and where a
mock would quietly diverge.

```bash
make test        # 380 tests
make check       # ruff, format, mypy, workflow lint
make ci          # everything CI runs, in the same order
```

## Packages

| Package | What it is for |
|---|---|
| `inceptools` | The application factory. Assembles, serves, drains. |
| `core.di` | Lazy, typed providers with cycle detection and ordered teardown. |
| `core.module` | Apps, modules, exports, the two-phase build. |
| `core.errs` | One error type: a transport-neutral kind, a safe message, an internal cause. |
| `core.config` | Environment into an object, every problem at once, secrets masked. |
| `core.logx` | Structured logging with the request's trace, account and actor attached. |
| `core.telemetry` | OpenTelemetry traces for HTTP, jobs and queues. Optional, and a no-op when off. |
| `core.ctxkit` | Request identity on a context, and `detach` for background work. |
| `core.metadata` | The `Reflector`, and the metadata every `@Use*` decorator writes. |
| `core.discovery` | Find every provider or handler carrying a given marker. |
| `core.lifecycle` | Init, ready, reload, shutdown — reverse order, real drain deadline. |
| `web.router` | Routes as data, plus the registry that makes them assertable. |
| `web.guard` | Authorization, per route, visible in the route table. |
| `web.interceptor` | Around-the-handler concerns: timing, headers, audit on success. |
| `web.binding` | Bind-and-validate with field-scoped errors. |
| `web.extract` | Typed extraction from a request, that cannot silently yield a zero value. |
| `web.pipes` | Transform and validate a bound parameter — `ParseInt`, `Trim`, your own. |
| `web.params` | Custom parameter decorators: `CurrentAccount()`, `TraceId()`, your own. |
| `web.consumer` | Per-module, path-scoped middleware through `configure(consumer)`. |
| `web.response` | The envelope, and the one place a message is scrubbed. |
| `web.filter` | Exception filters; nothing reaches a client unclassified. |
| `web.pagination` | Bounded page requests with an allow-listed sort. |
| `web.middleware` | Request id, recover, timeout, CORS, security headers, rate limit — pure ASGI. |
| `web.health` | Liveness and readiness as the different questions they are. |
| `web.sse` | Heartbeats, flushing and disconnect-aware SSE. |
| `web.webhook` | Constant-time HMAC signing and verification, with a replay window. |
| `data.repo` | The scoped generic repository, over SQLAlchemy 2.0 async. |
| `data.sqlx` | Driver errors into typed errors; the session and transaction on a context. |
| `platform.cache` | Memory, Redis or noop; a cache failure is never a request failure. |
| `platform.events` | Typed in-process events; exceptions contained, contexts detached. |
| `platform.lock` | Distributed locks that only release their own. |
| `platform.scheduler` | Cron and intervals, singleton and overlap-safe. |
| `platform.queue` | Bounded in-process workers with retries and dead letters. |
| `reliability.retry` | Backoff with jitter, retrying only what is safe to repeat. |
| `reliability.breaker` | Fail fast on a dependency that is down. |
| `reliability.idempotency` | `Idempotency-Key`, so a retried POST does not charge twice. |
| `reliability.outbox` | A side effect as durable as the write that caused it. |
| `testkit` | The real pipeline, in memory, with the envelope decoded. |

## Examples

```bash
uv run python examples/minimal/main.py   # :8080  the floor — one route
uv run python examples/todo/main.py      # :8080  CRUD, tenancy, events, a scheduled purge
uv run python examples/worker/main.py    # :8081  no API at all — a queue and a schedule
```

[`worker`](https://github.com/IncepTools/python/tree/main/examples/worker) is the one to read if you want to see that a module
does not have to serve routes.

## Tooling

Project tooling — scaffolding, architecture lint, migrations — lives in the
single **`icpt`** CLI in the [Go repository](https://github.com/IncepTools/go), which covers both
languages. Only Python can import a Python application, so this package exposes
the one seam `icpt` shells out to:

```bash
python -m inceptools routes myservice.main:app --json
python -m inceptools routes myservice.main:app --unguarded   # exits 1 if any
```

## Documentation

[docs/](https://github.com/IncepTools/python/tree/main/docs) — one page per capability.

## Contributing

[CONTRIBUTING.md](https://github.com/IncepTools/python/blob/main/CONTRIBUTING.md). The short version: explain the failure a
line prevents, not the mechanism; `make ci` must pass; a fix comes with a test
that fails without it.

Governance is in [GOVERNANCE.md](https://github.com/IncepTools/python/blob/main/GOVERNANCE.md), and the bar for a new core
dependency is deliberately high — a framework's dependencies become every
importer's.

| | |
|---|---|
| Questions | [Discussions → Q&A](https://github.com/IncepTools/python/discussions/categories/q-a) |
| Bugs | [Open an issue](https://github.com/IncepTools/python/issues/new/choose) |
| Security | **Not a public issue** — [SECURITY.md](https://github.com/IncepTools/python/blob/main/SECURITY.md) |
| Everything else | [SUPPORT.md](https://github.com/IncepTools/python/blob/main/SUPPORT.md) |

## Status

**v0.1.0.** Pre-1.0: the exported API may change between minor versions, and
every such change is listed in [CHANGELOG.md](https://github.com/IncepTools/python/blob/main/CHANGELOG.md) under **Changed**
with what to do about it. After 1.0, no exported identifier changes
incompatibly within a major version.

380 tests, `mypy` clean, and the examples are covered by the suite so they
cannot rot. Built on FastAPI and Starlette, both stable; SQLAlchemy, Redis and
OpenTelemetry are optional extras that only the relevant packages need.

Releases are published through PyPI trusted publishing with build provenance
attestation and an SBOM — see [RELEASING.md](https://github.com/IncepTools/python/blob/main/RELEASING.md).

## Acknowledgements

The module and provider model is inspired by
[NestJS](https://nestjs.com), whose three-level structure and metadata-driven
binding are the clearest answer anyone has given to "where does this code
go". The failure-mode framing, the response envelope and the scoped repository
come from its [Go sibling](https://github.com/IncepTools/go).

Everything here is additive to [FastAPI](https://fastapi.tiangolo.com): a
handler is a FastAPI handler, a dependency is a FastAPI dependency, and an
application is an ASGI app. If this framework turns out to be the wrong
choice, the exit is deleting the decorators, not a rewrite.

## License

[Apache 2.0](https://github.com/IncepTools/python/blob/main/LICENSE).
