# Resilience

## Retry

```python
from inceptools.reliability import retry

result = await retry.call(lambda: client.fetch(url), retry.Policy(max_attempts=3))
```

The package **defaults to not retrying**. Retrying something you have not
reasoned about turns one failure into three — and if the operation was not
idempotent, into three side effects.

Retried by default: `unavailable`, `timeout`, and a `conflict` whose code is
`CONCURRENT_MODIFICATION`. A uniqueness conflict is also a conflict and will
fail identically forever, so it is not retried. A cancelled caller is never
retried: there is no time left to retry *in*.

Backoff doubles from `base_delay`, is capped at `max_delay`, and is jittered by
±50%. Without jitter, N callers that failed together retry together, and the
dependency you are backing off from receives a synchronised stampede at every
interval.

The **last** failure is raised, not the first: it is the one that describes the
state you ended in.

## Circuit breaker

```python
from inceptools.reliability import breaker

payments = breaker.Breaker("payments", threshold=5, cooldown=30.0)
charge = await payments.call(lambda: gateway.charge(amount))
```

One breaker per **dependency**, never one per service: a single breaker in
front of everything means a sick payment provider stops your product catalogue.

Only genuine unavailability counts — `unavailable`, `timeout`, `internal`, and
any unclassified exception out of a client call. A 404 from a dependency is a
healthy answer; counting it would open the breaker on a workload that merely
asks for things that do not exist.

In half-open, **exactly one probe** is allowed through at a time. Letting
everyone through on the cooldown boundary is a stampede onto a dependency that
has just come back, which usually puts it straight back down.

Rejection is `503` with code `CIRCUIT_OPEN`.

## Timeouts

```python
create_app(service="shop", middleware=ChainOptions(timeout=15.0))
```

504 `REQUEST_TIMEOUT` rather than holding a connection open forever.

## Rate limiting

```python
create_app(service="shop", rate_limit=RateLimitOptions(max_requests=100, window=60))
```

`RateLimit-Limit`, `-Remaining` and `-Reset` on every allowed request;
`Retry-After` and 429 `RATE_LIMITED` over budget. The default store is a fixed
window **per process**, so N replicas allow N× the limit — a deliberate trade
for a default with no dependencies. A real limit uses a shared store.

The key is the peer address, deliberately **not** `X-Forwarded-For`: that
header is caller-supplied, and trusting it by default turns a rate limit into a
suggestion. Behind a known proxy, install `ProxyHeadersMiddleware` explicitly.

`fail_open` is `True` by default — a broken limiter should not become an
outage. Set it to `False` deliberately in front of something expensive.
