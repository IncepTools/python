# Examples

Three runnable services. Each is covered by
[`tests/app/test_examples.py`](../tests/app/test_examples.py), which asserts
that every one of them builds, serves, and has no unguarded mutating routes —
so documentation that stops being true fails CI rather than misleading
somebody.

```bash
uv run python examples/minimal/main.py   # :8080
uv run python examples/todo/main.py      # :8080
uv run python examples/worker/main.py    # :8081
```

## `minimal` — the floor

One app, one module, one route, about fifty lines. Read this first to see
where the pieces go without anything else in the way.

```bash
curl localhost:8080/api/v1/greeter/hello/Ada
```

Shows: `@App` / `@Module` / `@Controller`, constructor injection, and what you
get for free — the envelope, `/livez`, `/readyz`, a request id on every
response.

## `todo` — a resource, properly

In-memory CRUD with tenant scoping, pagination, typed events and a scheduled
purge.

```bash
curl -H 'x-account: acc_1' localhost:8080/api/v1/todo/tasks

curl -H 'x-account: acc_1' -H 'content-type: application/json' \
     -d '{"title":"Write the docs"}' \
     localhost:8080/api/v1/todo/tasks
```

The thing worth trying: **drop the header.**

```bash
curl -i localhost:8080/api/v1/todo/tasks
# 401, code NO_TENANT
```

Not an empty list — a refusal. That is the whole point of the scoped
repository: forgetting the ownership filter cannot return an empty page, and
it cannot return somebody else's rows either.

Also worth trying:

```bash
# A validation failure names the field the client sent
curl -H 'x-account: acc_1' -H 'content-type: application/json' \
     -d '{"title":"a"}' localhost:8080/api/v1/todo/tasks

# Pagination metadata rides in the envelope's meta
curl -H 'x-account: acc_1' 'localhost:8080/api/v1/todo/tasks?page=2&limit=2'

# A sort outside the allow-list is refused, not passed to ORDER BY
curl -H 'x-account: acc_1' 'localhost:8080/api/v1/todo/tasks?sort=account_id'
```

Shows: guards, DTO validation, pagination with an allow-listed sort, an async
event subscriber on a detached context, and a singleton cron job.

## `worker` — no API at all

A service with zero routes. It schedules work and drains a queue, and still
gets the container, the lifecycle, health probes and graceful shutdown.

```bash
curl localhost:8081/readyz
```

Shows: that a module does not have to serve HTTP, a `Queue` with retries and a
dead-letter handler, and a `singleton=True` job that runs on one replica via
the distributed lock.

## Inspecting any of them

```bash
PYTHONPATH=examples/todo uv run python -m inceptools routes main:app
PYTHONPATH=examples/todo uv run python -m inceptools routes main:app --unguarded
```

The second exits non-zero if a mutating route has no guard, which is what the
CI job runs against all three.
