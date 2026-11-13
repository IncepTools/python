# Errors and the envelope

## One error type

A service never imports `fastapi`. It raises an `AppError`, which carries a
transport-neutral *kind*; the kind picks the status.

```python
from inceptools import errs

raise errs.conflict("this product is archived").with_code("PRODUCT_ARCHIVED")
```

| Constructor | Kind | Status |
|---|---|---|
| `errs.invalid` | `invalid` | 400 |
| `errs.unauthorized` (alias `unauthenticated`) | `unauthorized` | 401 |
| `errs.payment_required` | `payment` | 402 |
| `errs.forbidden` | `forbidden` | 403 |
| `errs.not_found` | `not_found` | 404 |
| `errs.conflict` | `conflict` | 409 |
| `errs.precondition_failed` | `precondition_failed` | 412 |
| `errs.unprocessable` | `unprocessable` | 422 |
| `errs.exhausted` (alias `rate_limited`) | `exhausted` | 429 |
| `errs.internal` | `internal` | 500 |
| `errs.not_supported` (alias `not_implemented`) | `not_supported` | 501 |
| `errs.bad_gateway` | `bad_gateway` | 502 |
| `errs.unavailable` | `unavailable` | 503 |
| `errs.timeout` | `timeout` | 504 |

Why a service does not raise `HTTPException`: a service that speaks HTTP cannot
be called from a scheduled job, a CLI or a queue consumer without faking a
request.

### Builders return copies

```python
base = errs.conflict("archived")
tagged = base.with_code("PRODUCT_ARCHIVED").with_meta(product_id=pid)
# base is unchanged — a shared sentinel cannot be mutated by one caller
```

`with_cause(exc)` attaches the original exception. It is **logged and never
serialized**, so a driver message naming a column, a constraint or a hostname
cannot reach a client.

### `not_found`, not `forbidden`

A row that exists but belongs to another account is *not found*. A 403 confirms
the id exists, which is itself a leak.

## The envelope

Every reply leaves through one funnel.

```json
{
  "data": { "id": "9c1e…", "name": "Desk lamp" },
  "meta": {
    "success": true, "statusCode": 200, "message": "Success",
    "traceId": "0b2f…", "requestTime": 12,
    "route": "/api/v1/catalog/products/9c1e…"
  }
}
```

A failure sets `data` to `null` and adds `code`, plus `errors` when the failure
is field-scoped. A list adds `page`, `pageSize`, `total` and `pages` to `meta`.
`204` is the only reply that escapes the envelope, because `204` may not carry
a body.

### What a client may see

- An `AppError` of any kind but `internal` shows its message.
- An `internal` error shows `"an internal error occurred"`, always.
- Any exception that is *not* an `AppError` becomes a redacted 500, because an
  unclassified error is by definition one nobody checked for leaks.
- Field detail on an internal error is withheld — it can name a column.

### The sanitizer

A last-chance rewrite of any outbound message:

```python
def scrub(message: str, scope: str, route: str) -> str:
    return EMAIL.sub("[redacted]", message)

create_app(service="shop", sanitizer=scrub)
```

Process-global and set once at boot, like logging configuration. A per-request
sanitizer would be a per-request chance to forget one.

### The flat style

For a fleet that already returns a different shape:

```python
from inceptools import EnvelopeStyle, ResponseOptions

create_app(service="shop", response=ResponseOptions(style=EnvelopeStyle.FLAT))
```

renders `{data, errors, success, statusCode, requestTime, message, sessionId}`
instead, so adopting the framework is not also a wire-contract change.

## Exception filters

Filters sit outside everything — middleware, guards, interceptors and the
handler have all unwound by the time one runs.

```python
from inceptools import filters

def catch_stripe(request, exc):
    if isinstance(exc, stripe.error.CardError):
        return filters.response_mod.fail(
            errs.payment_required(exc.user_message).with_code("CARD_DECLINED")
        )
    return None    # decline it; the next filter, then the built-in mapping

create_app(service="shop", filters=(filters.of("stripe", catch_stripe),))
```

4xx are not logged by default: logging them turns a scanner into a log bill.
`log_client_errors=True` while debugging a client integration.
