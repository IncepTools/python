# Idempotency and the transactional outbox

## Idempotency

A client that times out does not know whether the charge happened. It retries.
Without a key it is charged twice, and the second charge is indistinguishable
from a legitimate second purchase.

```python
from inceptools.reliability.idempotency import IdempotencyMiddleware, RedisStore

app.fastapi.add_middleware(
    IdempotencyMiddleware, store=RedisStore(redis), ttl=86400
)
```

A repeat of the same `Idempotency-Key` replays the stored response rather than
running the handler again.

Three cases, and the third is the one people miss:

| Situation | Answer |
|---|---|
| The first request finished | Replay it, with `Idempotent-Replay: true` |
| The first request is still running | 409 `IDEMPOTENCY_IN_PROGRESS` — not a second concurrent copy |
| Same key, different body | 422 `IDEMPOTENCY_KEY_REUSED` — not a response describing a different request |

A 5xx is **not** stored: the retry should actually retry. A handler that raises
drops its claim, so a retry is not met with a 409 for the next day.

## The transactional outbox

You write the order, then publish `order.placed`. If the process dies between
the two, the order exists and nothing downstream knows. If you publish first
and the write fails, the reverse. There is no ordering of two systems that
fixes this.

```python
from inceptools.reliability import outbox

TABLE = outbox.outbox_table(metadata)

async with sqlx.transaction(session):
    await repo.create(order)
    await outbox.enqueue(TABLE, "order.placed", payload)   # same transaction
```

`enqueue` **refuses** to run outside a transaction. Outside one it is just a
slower publish with the same hole in the middle, and the hole is invisible.

A relay then moves rows to the real broker:

```python
relay = outbox.Relay(TABLE, publish_to_sns, session_factory=sessions,
                     batch=50, interval=5.0, max_attempts=10, backoff=10.0)
app.lifecycle.register_object(relay, name="outbox-relay")
```

It runs on every replica; rows are claimed with `SKIP LOCKED` where the
database supports it, so N relays share the work instead of fighting over the
same batch. A failed publish backs off exponentially, capped at an hour so a
poison message does not schedule itself out of sight. Return `outbox.DROP` from
a publisher to discard a message permanently.

Delivery is **at-least-once**. Consumers must be idempotent; exactly-once is
not on offer here or anywhere else.

`relay.pending()` is worth a health check.
