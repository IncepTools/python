# The scoped repository

```python
from inceptools.data.repo import Repository

repo = Repository(
    Product,
    entity_name="product",
    tenanted=True,                      # scoped on account_id
    sortable=("created_at", "price"),   # the allow-list for ORDER BY
    default_sort="created_at",
)
```

Needs the `sql` extra: `uv add "inceptools[sql]"`.

## What it guarantees

**Every read is filtered.** The account comes from the context, not from an
argument you might forget.

**Every create takes the account from the context**, not from the payload.
Taking it from the payload lets a caller write into another account by sending
its id.

**A request with no account is a 401**, never "all accounts". That is the
failure mode this exists to prevent: forgetting the filter does not raise, it
returns another customer's rows.

```python
await repo.find_all()        # 401 NO_TENANT with no account on the context
await repo.get_by_id(pid)    # 404 if it belongs to someone else — never 403
```

## Reaching wider

```python
async with repo.unscoped():          # a nightly report across every account
    total = await repo.count()
```

Deliberate, visible, greppable.

## Updates take a mapping

```python
await repo.update_by_id(pid, {"archived": False})
```

Not a model instance. A model carries defaults for every field you did not set,
so a model-based update cannot distinguish "leave it alone" from "set it to
false" — and the PATCH that disables something appears to succeed while
changing nothing.

`update_fields` refuses an empty `values` **and** an empty filter: an update
with no `WHERE` rewrites every row the account owns. `delete` refuses an empty
filter for the same reason.

## Sessions and transactions

```python
from inceptools.data import sqlx

async with sqlx.session_scope(session_factory) as session:
    async with sqlx.transaction(session):
        await repo.create(order)
        await outbox.enqueue(table, "order.placed", payload)
```

The session lives on a context variable, so a repository does not need it
passed down through every call. Nesting `transaction` reuses the outer one, so
a service calling another service does not commit half of it.

A **driver** failure inside a transaction is translated; your own exception is
re-raised untouched, so `except PaymentDeclined` upstream keeps matching.

## Driver errors become your vocabulary

| Driver says | You get |
|---|---|
| duplicate key / unique violation | 409 `DUPLICATE`, with `meta.constraint` |
| foreign key violation | 409 `FOREIGN_KEY` |
| not-null violation | 400 `NOT_NULL` |
| check violation | 400 `CHECK_FAILED` |
| deadlock / serialization failure | 409 `CONCURRENT_MODIFICATION` — and this one *is* retryable |
| connection refused | 503 `DB_UNAVAILABLE` |
| statement timeout | 504 `DB_TIMEOUT` |
| anything else | redacted 500, original in the log |

Postgres, MySQL and SQLite message forms are all recognised.

## Pagination

```python
query = pagination.from_request(
    request, pagination.Options(sortable=("created_at", "price"), max_limit=100)
)
page = await repo.paginate(query)
return response.listing(page.items, page=page.page, page_size=page.size, total=page.total)
```

An oversized `limit` is clamped, not rejected — a client asking for too much
gets the maximum, not an error it has to learn about. A `sort` outside the
allow-list *is* rejected: the sort key is interpolated into SQL and is safe
only because it came from that list.
