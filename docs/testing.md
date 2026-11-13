# Testing

```python
import pytest
from inceptools import testkit

@pytest.fixture
def client():
    with testkit.build(CatalogApp, prefix="/api/v1") as app:
        yield app


def test_create_requires_an_account(client):
    client.post("/api/v1/catalog/products", {"name": "ab"}) \
          .expect_status(401).expect_code("NO_TENANT")
```

These go through the real middleware, the real guards and the real exception
filter — which is where most of this framework's behaviour lives, and where a
mock would quietly diverge. The thing you most want to test is that an endpoint
is *refused*, and a mock that bypasses the guard tests nothing.

## Acting as somebody

`testkit.build` wires an identity resolver that reads test headers, so you can
act as an account without standing up an auth server.

```python
client.as_account("acc_1")
client.as_actor("user_1", account_id="acc_1", roles="catalog:write")
client.anonymous()

with client.acting_as(account_id="acc_2"):
    client.get("/api/v1/catalog/products/1").expect_status(404)
```

## The reply

Every assertion returns the reply, so expectations chain.

```python
reply = client.get("/api/v1/catalog/products/1")
reply.expect_ok().expect_enveloped()
reply.data        # the payload
reply.meta        # success, statusCode, traceId, route, code…
reply.code        # "PRODUCT_ARCHIVED"
reply.fields      # ["price", "tags[0]"] from a validation failure
```

`expect_status`, `expect_ok`, `expect_failure`, `expect_code`,
`expect_message`, `expect_data`, `expect_field_error`, `expect_header`,
`expect_enveloped`.

## The assertion worth having in CI

```python
def test_every_write_is_guarded(client):
    client.expect_all_writes_guarded()
```

Or make it a boot failure for the whole service:

```python
create_app(service="shop", fail_on_unguarded=True)
```

## Routes exist before startup

`app.mount()` is synchronous, so the route table, OpenAPI generation and a
route assertion all work without starting the app:

```python
def test_the_route_table_is_stable():
    app = create_app(service="shop")
    app.mount(CatalogApp)
    assert len(app.routes) == 7
```

Inside a running event loop, use `await app.mount_async(...)`.

## Replacing a provider

```python
from inceptools.testkit.module import TestingModule

with TestingModule(CatalogApp).override(ProductRepo).use_value(fake).compile() as client:
    client.get("/api/v1/catalog/products/1").expect_data({"id": 1, "name": "fake"})
```

`.use_value`, `.use_class` and `.use_factory`; `.provide(...)` to add something
the application does not have. The override is registered on the root
container, which every module resolves through, so it wins wherever the real
provider was declared — and nothing in the application under test changes,
which is the point.

`.override_guard(guard.Tenanted, allow=False)` isolates a handler. Use it to
test the handler, never to make a failing authorization test pass: the guards
are the part most worth testing.

### Reaching the real instance

```python
repo = client.container.get(ProductRepo)     # searches every module container
repo.rows[42] = {"id": 42, "name": "Injected"}
```

Read it **inside** the `with` block: shutdown disposes the containers.

## Inside an event loop

`testkit.build` mounts synchronously. In an `async def` test, use
`await testkit.build_async(CatalogApp)`.

## Testing a service without HTTP

```python
with testkit.acting_as(account_id="acc_1"):
    await service.create(dto)
```
