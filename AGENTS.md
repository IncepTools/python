# Working on inceptools

This repository is a Python application framework. Changes should preserve its
central promise: **a service's important failure modes are visible at boot, in
a test, or in a type check — not discovered by a production request.**

## Repository map

```
src/inceptools/        the framework
  application.py       the application factory and runtime lifecycle
  core/                di · module · errs · config · logx · ctxkit ·
                       lifecycle · metadata · discovery · telemetry
  web/                 router · guard · interceptor · binding · pipes ·
                       params · response · filter · pagination · middleware ·
                       consumer · health · sse · webhook · schema
  data/                repo (scoped repository) · sqlx (driver errors)
  platform/            cache · events · lock · scheduler · queue
  reliability/         retry · breaker · idempotency · outbox
  testkit/             the real-pipeline test client and testing module
  __main__.py          route introspection, for external tooling only
tests/                 mirrors src/, named after guarantees
examples/              three runnable services, all covered by tests
docs/                  one page per capability
scripts/               repository maintenance helpers
```

## Required checks

Run the narrowest relevant thing while iterating, then the full suite:

```bash
uv run pytest tests/web            # the area you are changing
make ci                            # lint, format, types, tests, doc links
```

`make ci` is exactly what CI runs, in the same order. If it passes locally and
fails there, that difference is a bug in the `Makefile`.

For dependency or release changes also run `make audit` and
`gh workflow run release.yml -f dry-run=true`.

## Conventions that are not negotiable

- **A test that fails without the change.** Name it after the guarantee:
  `test_a_query_with_no_account_is_refused`, not `test_scope_adds_where`.
- **Comments say what goes wrong without the line**, not what the line does.
  If you cannot name the failure, the line may not be needed.
- **No new core dependency** without justifying it for every consumer. New
  capabilities go in an optional extra.
- **Nothing new in `__all__`** unless it is meant to be supported forever.
- **`# noqa` and `# type: ignore` carry a reason** on the same line.
- **Four things move together** for a public change: code, test, the guide
  under `docs/`, and `CHANGELOG.md` under *Unreleased*.

## Invariants to preserve

Breaking one of these is a bug even if every test passes:

1. **One envelope.** Every reply goes through `web.response`. An unclassified
   exception becomes a redacted 500; a driver message, constraint name or
   stack trace never reaches a client.
2. **The ownership filter.** A tenanted repository filters every read, stamps
   every create from the context, and refuses a query with no account. Only
   `unscoped()` widens it.
3. **Routes are data.** A controller declares routes; mounting records them.
   `routes.unguarded()` must keep working, and a mutating route is guarded or
   explicitly public.
4. **Boot-time failure.** Providers are constructed eagerly, config is
   validated once, cron expressions are parsed at registration. Move failures
   earlier, never later.
5. **Reverse-order teardown.** The container disposes in reverse construction
   order. Do not move `on_module_destroy` onto the lifecycle registry.
6. **Pure-ASGI middleware.** Never `BaseHTTPMiddleware`: it runs downstream in
   a separate task, so a contextvar set in it is invisible to the endpoint.
7. **Module encapsulation.** A module resolves its own providers, its imports'
   exports, and global exports. Do not promote a private provider to the root
   to make something work.
8. **Annotations.** The router and container resolve string annotations with
   `eval_str`. A type a string annotation cannot name fails at **mount** time
   with a message saying what to do.

## Traps specific to this codebase

- **`extra={"module": ...}`** raises — `module` is a `LogRecord` attribute.
  Use `logx.safe_extra()`.
- **`value.model_fields`** on a Pydantic *instance* is deprecated and the test
  suite turns that warning into an error. Use `type(value).model_fields`.
- **`from __future__ import annotations`** is on in most modules, so a model
  defined inside a function cannot be named by an annotation.
- **One pending `__anext__`** in the SSE loop. Starting a second while the
  first is live raises "asynchronous generator is already running".
- **Teardown clears containers**, so read a provider inside the test's `with`
  block, not after it.
- **`mount()` is synchronous** and refuses to run inside an event loop. In an
  async test use `await testkit.build_async(...)`.

## Safe behaviour

- Check `git status` before editing and preserve unrelated changes.
- Do not rewrite history, delete tags, or publish anything without being asked.
- Never commit secrets, database URLs, tokens or generated distributions.
- Prefer additive, reversible changes, and report the commands you ran.
- Ask before anything that reaches an external service.
