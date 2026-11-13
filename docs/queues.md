# Queues

A bounded in-process work queue with retries and a dead-letter callback.

```python
from inceptools.platform.queue import Queue

emails = Queue(
    "emails",
    handler=service.send,
    workers=4,
    buffer=128,
    max_attempts=3,
    retry_delay=2.0,
    on_dead_letter=lambda d: log.error("gave up on %s: %s", d.message.body, d.error),
)
app.lifecycle.register_object(emails, name="emails")

await emails.enqueue({"to": "ada@example.com"})
```

**Explicitly not durable.** Messages live in memory and are lost when the
process exits. That is a deliberate scope: it covers "do this just after the
response, with a couple of retries" without pulling in a broker. When the
effect must survive a crash, use the [outbox](durability.md).

## What it guarantees

- **Enqueue works before start**, so boot-time producers are fine.
- **A full buffer blocks** — backpressure, rather than an unbounded queue that
  becomes an out-of-memory kill. `enqueue_nowait` raises 429 `QUEUE_FULL`
  instead.
- **Identity travels** with the message: the account and trace id of whoever
  enqueued it are on the context inside the worker.
- **One bad message does not block the others.** A retrying message occupies
  its own worker, not the queue.
- **Shutdown drains.** The input is closed and the workers finish what is
  already queued; cancellation is reserved for the deadline path, so a normal
  deploy never abandons accepted work.

## Dead letters

After `max_attempts`, `on_dead_letter` receives the message and the last
exception. Nothing persists it — if you do not supply a handler, the log line
is the only record.
