# Pagination

```python
from inceptools.web import pagination, response

@Get("/")
async def index(self, request: Request):
    query = pagination.from_request(
        request,
        pagination.Options(sortable=("created_at", "price"), default_sort="created_at"),
    )
    page = await self.repo.paginate(query)
    return response.listing(page.items, page=page.page,
                            page_size=page.size, total=page.total)
```

Query parameters: `page`, `limit`, `sort`, `order`, `search`.

The list metadata joins the envelope's `meta`:

```json
"meta": { "success": true, "statusCode": 200, "page": 2, "pageSize": 50,
          "total": 134, "pages": 3 }
```

## Two failures this prevents

**`?limit=1000000`** turning a list endpoint into a denial of service. The
limit is **clamped, not rejected** — a client asking for too much gets the
maximum, not an error it has to learn about.

**`?sort=password`** reaching an `ORDER BY`. `sortable` is an allow-list, and
an empty one refuses sorting entirely rather than passing the column through.
The sort key is interpolated into SQL and is safe *only* because it came from
that list.

| Input | Result |
|---|---|
| `page=0`, `page=abc`, `limit=0` | 400 `INVALID_PAGINATION`, naming the parameter |
| `limit` above `max_limit` | clamped |
| `sort` outside the allow-list | 400 `INVALID_SORT`, listing what is allowed |
| `order` not `asc`/`desc` | 400 `INVALID_SORT` |
| `sort` with no `order` | defaults to ascending |

`default_sort` comes from your code, so it is not checked against the
allow-list.
