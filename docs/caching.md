# Caching and distributed locks

## Cache

One invariant: **a cache failure is never a request failure.**

```python
from inceptools.platform import cache

store = cache.Redis(redis, prefix="shop")     # or cache.Memory(), cache.Noop()

product = await cache.get_or_set(
    store, cache.key("catalog", "product", pid), ttl=300,
    load=lambda: repo.get_by_id(pid),
)
```

- A read error degrades to a **miss** — latency, not availability.
- A write error is swallowed and logged.
- A value that no longer decodes is **deleted and treated as a miss**, so a
  shape changed by a deploy self-heals instead of failing forever.
- `load`'s exception propagates **uncached**. Caching a failure turns one bad
  minute into a bad TTL.

`delete` is the deliberate exception: its error *is* raised. A silently failed
invalidation leaves the cache ahead of the database, and the caller may want to
refuse the write rather than serve a lie.

### Keys

`cache.key("catalog", "product", pid)` → `catalog:product:<pid>`. Always
namespace by module; an un-namespaced key collides across modules, and the
collision looks like data corruption rather than a cache bug.

### Backends

| Backend | Use |
|---|---|
| `cache.Memory(sweep_interval)` | one process. With N replicas you have N caches, and a delete on one leaves the others stale until the TTL. |
| `cache.Redis(client, prefix)` | shared. `delete_prefix` uses `SCAN`, never `KEYS` — `KEYS` blocks the whole server and turns an invalidation into an outage of everything else. |
| `cache.Noop()` | caching off, without nil checks. |

## Locks

"Only one replica runs this at a time."

```python
from inceptools.platform import lock

ran, result = await lock.run_locked(
    lock.Redis(redis, prefix="shop"), "nightly-report",
    lambda: service.build_report(), ttl=300,
)
if not ran:
    log.debug("another replica has it")
```

Four properties, all load-bearing:

1. **A TTL**, so a replica that dies holding the lock does not hold it forever.
2. **A unique token** per acquisition.
3. **Compare-and-delete on release**, in one atomic Lua script. Releasing by
   key alone means a slow holder whose TTL expired deletes the *next* holder's
   lock — and now two replicas run at once, which is the exact thing the lock
   exists to prevent.
4. **Refresh**, for work that may outlive its TTL.

Not getting the lock is not an error. Release runs shielded, so a cancelled
request still releases rather than leaving the lock to sit until its TTL.

A documented limitation: this is not safe across a Redis failover. Use it to
prevent duplicate **work**, not to protect an invariant — an invariant wants a
database row lock.
