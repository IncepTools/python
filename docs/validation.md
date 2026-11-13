# Validation

Validation failures are 400 with `code: "VALIDATION_FAILED"` and one entry per
bad field.

```json
"errors": [
  { "field": "price",             "message": "price must be 0 or more", "rule": "gte" },
  { "field": "rules[0].protocol", "message": "rules[0].protocol is required", "rule": "required" }
]
```

Field names are the ones **the client sent**, and nested paths read as
`rules[0].protocol`, not `body -> rules -> 0 -> protocol`. A message pointing
at something the caller cannot find in their payload costs a support ticket.

Nothing from Pydantic reaches the client: not the exception type, not the
`input` value, not the docs URL.

## DTO hooks

A DTO may implement any of three methods. They run in this order:

```python
class CreateProduct(BaseModel):
    name: str = Field(min_length=2)
    slug: str = ""
    price: float = Field(ge=0)

    def normalize(self) -> None:      # 1. trim, lowercase, canonicalise
        self.name = self.name.strip()

    def set_defaults(self) -> None:   # 2. so a default can satisfy `required`
        self.slug = self.slug or slugify(self.name)

    def validate_self(self) -> None:  # 3. cross-field rules
        if self.price == 0 and not self.free_tier:
            raise ValueError("a zero price requires the free tier")
```

The order matters: a default can satisfy `required`, and a cross-field rule can
assume the individual fields are already well-formed.

`validate_self` raising `ValueError` becomes one field error carrying its own
message.

## Binding by hand

```python
from inceptools.web import binding

dto = await binding.body(request, CreateProduct)
dto = binding.query(request, ListProducts)
dto = binding.params(request, ProductPath)
dto = binding.header(request, RequiredHeaders)
```

Each raises an `AppError` with per-field detail and a code of
`MALFORMED_BODY`, `MALFORMED_QUERY`, `MALFORMED_PARAMS` or `MALFORMED_HEADERS`.

## Single values

When a whole DTO is too much, `web.extract` reads one value and cannot
silently yield a zero:

```python
from inceptools.web import extract

pid = extract.uuid_param(request, "pid")           # required; 400 naming "pid"
limit = extract.int_query(request, "limit", 20)    # default when absent
active = extract.bool_query(request, "active", True)
status = extract.enum_query(request, "status", "draft", "live")
since = extract.datetime_query(request, "since")   # RFC 3339 or None
tags = extract.csv_query(request, "tags")          # never fails
```

## A PATCH field that may be false

Make it optional, and write with a mapping:

```python
class UpdateProduct(BaseModel):
    archived: bool | None = None       # None means "leave it alone"

await repo.update_by_id(pid, body.model_dump(exclude_unset=True))
```

A required field must carry its constraint explicitly; a bare `str` accepts
`""`.
