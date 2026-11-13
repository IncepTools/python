# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org).

Pre-1.0, the exported API may change between minor versions. Every such change
appears under **Changed** with what to do about it.

## [Unreleased]

### Fixed

- Every GitHub Actions reference now points at a published tag. `scorecard`
  referenced a major tag that was never published and failed on its first run;
  `setup-uv` stopped publishing floating major tags after v7, so it is pinned
  to an exact release.

### Added

- `scripts/check_workflows.py --online` asks GitHub whether every `uses:` ref
  actually exists, and CI runs it. A ref can be perfectly well-formed and have
  never been published, which is the one failure the offline checks cannot
  see.

## [0.1.0] — 2026-11-13

First release.

### Added

**Architecture**

- `@Module` with `controllers`, `providers`, `imports`, `exports`, `entities`,
  `guards`, `interceptors` and `health_checks`. Encapsulation is enforced at
  resolution: a module reaches its own providers, the exports of modules it
  imports, and nothing else.
- Two-phase build — every module's providers are registered before any module
  is built, so two modules may depend on each other's exports regardless of
  declaration order.
- `DynamicModule` for `for_root`-style configuration, `@Global` for
  cross-cutting exports, module re-exports, and `forward_ref` for circular
  imports.
- Transitive registration: a module reached only through `imports` does not
  have to be listed on its `@App`.

**Injection**

- Constructor injection from type hints, with `Annotated[X, Inject(TOKEN)]` and
  `@inject(param=TOKEN)` for providers a type cannot identify.
- Four provider kinds: `class_provider`, `value`, `factory` (sync, async or
  generator with teardown) and `existing` aliases.
- Three scopes — singleton, request and transient — with scope *bubbling*, so a
  provider or controller whose graph reaches a request-scoped provider is built
  per request.
- Cycle detection naming the ring, eager singleton construction at boot, and
  teardown in reverse construction order.
- `ModuleRef` for the three cases constructor injection cannot cover.

**Metadata**

- `Reflector`, `@SetMetadata`, and `@UseGuards` / `@UseInterceptors` /
  `@UseFilters` / `@UsePipes` at class and method level.
- `@Public`, `@Roles` and `@Scopes` as ready-made metadata decorators.
- `APP_GUARD`, `APP_INTERCEPTOR`, `APP_FILTER` and `APP_PIPE` for global
  bindings that need dependencies of their own.
- `DiscoveryService` for finding every method carrying a given marker.

**HTTP**

- Routes as data: a controller declares them, and the same declaration mounts,
  prints, documents and tests them. `routes.unguarded()` reports mutating
  routes with no guard.
- Guards, interceptors, pipes and parameter decorators, all injectable.
- `web.extract` for reading one typed value from a request, separate from
  `web.pipes`, which transforms a value already bound.
- One response envelope — `{"data": …, "meta": {…}}` — with a `flat` style for
  fleets that cannot change their wire contract on adoption day.
- Exception filters, including class-based `@Catch`. Anything unclassified
  becomes a redacted 500.
- Field-scoped validation messages using the names the client sent.
- Pure-ASGI middleware chain, and per-module path-scoped middleware through
  `configure(consumer)`.
- Bounded pagination with an allow-listed sort.

**Data**

- Scoped repository over SQLAlchemy 2.0 async: every read filtered, every
  create stamped from the context, a query with no account refused, and
  `unscoped()` as the explicit way out.
- Driver errors translated to the application's error vocabulary.
- Session and transaction on a context, with nesting that reuses the outer
  transaction.

**Platform and reliability**

- Cache (memory, Redis, noop) where a failure degrades to a miss.
- Typed in-process events with detached async contexts.
- Distributed locks with compare-and-delete release.
- Cron scheduler with a self-contained five-field parser, singleton jobs and
  skip-not-queue overlap handling.
- Bounded work queue with retries and dead letters.
- Retry with jitter, circuit breaker, idempotency keys and a transactional
  outbox.

**Operations**

- Structured logging with correlation on a contextvar, and a JSON formatter.
- OpenTelemetry spans for HTTP, with graceful degradation when the extra is
  absent.
- `/livez`, `/readyz`, `/healthz` and `/health`, with critical and optional
  checks.
- Configuration that reports every problem at once and masks secrets.
- A test client that goes through the real pipeline, and a testing module for
  overriding providers.
- `python -m inceptools routes` for external tooling.

[Unreleased]: https://github.com/IncepTools/python/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/IncepTools/python/releases/tag/v0.1.0
