# Events

Decouples a side effect from its cause. Dispatch is keyed on the event's
**class**, so a handler declares what it listens for by type annotation rather
than by a string nobody checks.

```python
from dataclasses import dataclass
from inceptools.platform import events

@dataclass
class OrderPlaced:
    order_id: int

bus = events.Bus()
app.supply(bus)

@bus.subscribe(mode=events.Mode.ASYNC, name="audit")
async def record(event: OrderPlaced) -> None:
    await audit.write(event)

await bus.publish(OrderPlaced(order_id=7))
```

## Async versus sync

| Mode | Runs | `publish` |
|---|---|---|
| `Mode.ASYNC` (default) | own task, **detached** context | neither waits nor sees the error |
| `Mode.SYNC` | inline | waits and raises the combined failure |

An async handler keeps the trace id and the account and drops the
cancellation. Handing a handler the request's task means the audit write is
cancelled the moment the client disconnects — and nothing tells you.

A **sync** subscriber cannot veto the event: async subscribers registered
before it have already started. A prerequisite belongs in the service *before*
`publish`, not in a subscriber.

One failing subscriber never stops its siblings.

## Dispatch is exact

A handler for `Order` does not receive an `ExpressOrder`. Explicit beats
surprising.

## Draining

```python
await bus.wait(timeout=10)       # every async handler started so far
```

`Bus` implements `on_module_destroy`, so registering it with the lifecycle
drains it at shutdown.

Not durable. When the effect must survive a crash, use the
[outbox](durability.md).
