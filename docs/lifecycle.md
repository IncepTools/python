# Lifecycle

Six hooks, in this order:

```
on_module_init → on_application_bootstrap → on_application_ready
    … serving …
before_application_shutdown → on_module_destroy → on_application_shutdown
```

| Phase | Question | Order | On failure |
|---|---|---|---|
| `init` | can we connect? | registration | **stops at the first failure** |
| `ready` | everything is up; start background work | registration | runs all, reports all |
| `reload` | re-read configuration | registration | runs all; the old config keeps running |
| `before_shutdown` | stop accepting new work | **reverse** | runs all, within the deadline |
| *disposal* | release what was built | **reverse construction** | runs all |
| `shutdown` | the last word | **reverse** | runs all, within the deadline |

`before_application_shutdown` runs before anything is disposed, so a queue can
close its input while its handler's dependencies are all still alive.

## Hooks on a provider

```python
@Injectable()
class SearchClient:
    async def on_module_init(self) -> None:
        await self.connect()

    async def on_application_bootstrap(self) -> None:
        self.start_warmup()

    async def before_application_shutdown(self) -> None:
        self.stop_accepting()

    async def on_module_destroy(self) -> None:
        await self.close()
```

Every provider the container builds gets its `init`, `ready` and `reload` hooks
run. **Disposal is the container's job**, in reverse construction order — so a
repository is disposed before the engine it borrows sessions from, which is a
stronger guarantee than registration order can give.

## Hooks on the application

```python
@app.on_ready
async def warm_the_cache() -> None: ...

@app.on_shutdown
async def flush_metrics() -> None: ...
```

## Boot order

```
container registered → modules built → routes mounted → health routes
→ startup: eager construction → init → ready
→ shutdown: drain (reverse) → container teardown (reverse construction order)
```

Providers are constructed **eagerly**, so a missing environment variable or an
unreachable dependency fails the deploy rather than the first request that
needed it.

## The drain deadline

`drain_timeout` (25s by default) is the whole shutdown budget. A hook that
hangs is abandoned and reported, not awaited forever:

```
lifecycle hook abandoned: shutdown:SearchClient.on_module_destroy: TimeoutError('drain deadline exceeded before this hook')
```

## Serverless

`Application` is an ASGI app, so a Lambda adapter takes it directly. Call
`await app.start()` on cold start and `await app.reload()` from a control
plane, since there are no signals.
