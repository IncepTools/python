# Roadmap

What is built, what is next, and what this framework will deliberately never
do. Items move up when somebody needs them; the ordering below is a judgement
about value, not a promise about dates.

Pre-1.0, the exported API may change between minor versions. Every such change
is listed in [CHANGELOG.md](CHANGELOG.md) under **Changed**, with what to do
about it.

---

## Shipped — v0.1

| Area | Capability |
|---|---|
| Architecture | Modules with enforced encapsulation, two-phase build, dynamic modules, global modules, re-exports, forward references |
| Injection | Constructor injection, four provider kinds, three scopes with bubbling, cycle detection, ordered teardown, `ModuleRef` |
| Metadata | `Reflector`, `@SetMetadata`, `@UseGuards` / `@UseInterceptors` / `@UseFilters` / `@UsePipes`, `APP_*` global bindings, `DiscoveryService` |
| HTTP | Routes as data, guards, interceptors, pipes, parameter decorators, one response envelope, exception filters, pagination |
| Middleware | Pure-ASGI chain, per-module path-scoped middleware, CORS, security headers, rate limit, timeout |
| Data | Scoped repository over SQLAlchemy 2.0 async, driver-error translation, transactions on a context |
| Platform | Cache, typed events, distributed locks, cron scheduler, bounded work queue |
| Reliability | Retry, circuit breaker, idempotency keys, transactional outbox |
| Observability | Structured logging with correlation, OpenTelemetry spans, health probes |
| Testing | Real-pipeline test client, testing module with provider overrides |

---

## Next — v0.2: contracts and the data layer

The theme is making what a service promises checkable.

- **OpenAPI breaking-change detection.** The route table is already data; the
  missing half is comparing two dumps and failing CI on a removed field or a
  narrowed type. A contract nobody checks is a contract that drifts.
- **Request-scoped unit of work.** A session opened per request, committed
  once, and injected where it is needed — rather than each service opening its
  own and two of them half-committing a single logical write.
- **Repository read/write split.** A replica for reads, the primary for
  writes, chosen by the repository rather than remembered at each call site.
- **Built-in health checks** for SQLAlchemy, Redis and an HTTP dependency, so
  `/readyz` is correct without anybody writing a probe.
- **A declarative caching interceptor.** `@CacheFor(300)` reading its TTL
  through the `Reflector`, with the invalidation hook that makes it safe.
- **Shared rate-limit store.** The current one is per process, so N replicas
  allow N times the limit. Documented, but still wrong for a real limit.

## v0.3: work that leaves the process

Everything in `platform` today is in-memory and says so. This phase adds
durability behind the same interfaces, so adopting a broker is a wiring change
rather than a rewrite.

- **Durable queue adapters** — SQS first, then Redis Streams. The existing
  `Queue` interface, the same retry and dead-letter semantics.
- **Outbox publishers** for SNS, SQS and Kafka, with the relay unchanged.
- **Consumer guards and interceptors.** An `ExecutionContext` already covers
  non-HTTP boundaries; the binding decorators should work on a consumer the
  way they work on a route.
- **Scheduler leader election** beyond the per-job lock, so a hundred jobs do
  not take a hundred locks every minute.

## v0.4: transports

- **WebSocket gateways** with the same guard and interceptor pipeline. A
  socket that bypasses authorization is the most expensive kind.
- **Server-sent events as a first-class route type**, instead of `raw=True`.
- **Service-to-service calls** with the trace context, retry policy and
  circuit breaker already attached — the three things every hand-rolled client
  forgets.

## v0.5: operating it

- **Metrics beyond spans** — OpenTelemetry counters and histograms for
  requests, queue depth, job duration and outbox lag.
- **A Prometheus exposition endpoint** for deployments that have not moved to
  OTLP.
- **Serverless adapter.** The application is already an ASGI app; this is the
  Lambda cold-start, `reload` and drain story written down and tested.
- **An introspection endpoint** — the route table, the provider graph and the
  module import graph, behind auth. Mostly for answering "what does this
  service actually expose" without reading it.

## Under consideration

Wanted by somebody, not yet justified for everybody.

- CQRS command and query buses, with sagas.
- A schema registry for event payloads, so a consumer can refuse an
  incompatible version instead of mis-parsing it.
- Feature flags with a provider interface.
- Internationalised error messages. The envelope has the hook; the question is
  who owns the catalogue.
- Lazy module loading, for a service whose cold start is dominated by a module
  one endpoint uses.
- Multi-tenant schema routing, for a database-per-tenant deployment.

---

## Deliberate non-goals

Saying no is most of a framework's design.

- **An ORM.** SQLAlchemy is excellent and the repository is a thin scope over
  it. A query builder of our own would be a worse SQLAlchemy with no community.
- **A template engine or asset pipeline.** This is for services. A service
  that renders HTML can import Jinja directly.
- **An admin UI.** It would need opinions about authentication, theming and
  schema that no two services share.
- **Exactly-once delivery.** Not offered, because it is not deliverable. The
  outbox is at-least-once and says so; consumers must be idempotent.
- **A dependency graph resolved by reflection over runtime values.** Providers
  are declared. Magic that works is magic that fails silently when it stops
  working.
- **Supporting Pydantic v1.** It is end-of-life and the migration is
  mechanical.
- **A second project CLI.** Scaffolding, architecture lint and migrations live
  in the single `icpt` tool, which covers both this framework and its Go
  sibling. This package exposes only the route-introspection seam that tool
  shells out to, because only Python can import a Python application.
- **Competing with FastAPI.** Everything here is additive. A handler is a
  FastAPI handler, a dependency is a FastAPI dependency, and an application is
  an ASGI app. If this framework is the wrong choice, the exit is deleting the
  decorators, not a rewrite.

---

## How to influence this

Open a discussion describing the failure you hit, not the feature you want. A
roadmap item earns its place by naming a thing that currently goes wrong — the
whole framework is organised around that, and so is this list.
