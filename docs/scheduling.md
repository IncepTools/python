# Task scheduling

```python
from inceptools.platform import lock
from inceptools.platform.scheduler import Job, Scheduler

scheduler = Scheduler(locker=lock.Redis(redis))   # lock.Memory() for one replica
scheduler.add(Job(
    name="expire-abandoned-carts",
    cron="0 3 * * *",
    singleton=True,
    timeout=600,
    jitter=30,
    run=service.expire_abandoned,
))
app.lifecycle.register_object(scheduler, name="scheduler")
```

Or mark methods and collect them:

```python
from inceptools.platform.scheduler import job

class CartService:
    @job(cron="0 3 * * *", singleton=True, timeout=600)
    async def expire_abandoned(self) -> None: ...

scheduler.add_from(service)
```

## Validation is eager

A malformed cron expression fails the boot, not the night it was meant to run:

```
scheduler: job 'nightly' has an invalid cron expression '0 3 * *':
'0 3 * *' has 4 fields; a cron expression has 5 (minute hour day-of-month month day-of-week)
```

Also checked at `add`: a missing name, a missing `run`, setting both or neither
of `cron`/`every`, a duplicate name, a singleton job with no locker, and adding
a job after start.

## Cron syntax

Standard **five fields** — `minute hour day-of-month month day-of-week`, no
seconds field, because a cron line that means something different here than it
does in `crontab` is a trap. Ranges, steps, lists, and three-letter month and
day names. Descriptors: `@hourly`, `@daily`, `@midnight`, `@weekly`,
`@monthly`, `@yearly`.

Intervals use `every` instead: `every=30`, `every="30s"`, `every="1h30m"`.

The parser is written out rather than taken from a dependency: a framework's
dependencies become every importer's.

## Overlap

A run still going when the next is due is **skipped, not queued**:

```
scheduled job skipped: the previous run is still going
```

Queueing a slow job behind itself turns a slowdown into an unbounded backlog,
and the backlog is discovered at 3am by the thing it starved.

## Singleton

`singleton=True` takes a distributed lock keyed `job:<name>`, so the job runs
on one replica rather than on all of them. Not getting the lock is not an
error — it means another replica has it.

## Timeout and jitter

`timeout` defaults to five minutes. **Zero is not unlimited**; a job with no
timeout is a job that can hang forever.

`jitter` sleeps up to that many seconds before the first run, so N replicas
starting together do not all hit the database on the same second.

## Outcomes

| Condition | Level | Message |
|---|---|---|
| cancelled during shutdown | info | `scheduled job stopped for shutdown` |
| exceeded its timeout | warning | `scheduled job hit its timeout` |
| raised | error | `scheduled job failed` |
| finished | info | `scheduled job finished` |

A routine deploy must not page anyone, which is why the first is not an error.

The scheduler starts as a **ready** hook, not an init hook: a reconciler that
starts before the listener races the boot.
