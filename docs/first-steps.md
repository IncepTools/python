# First steps

```bash
uv add inceptools
```

## A service in one file

```python
from inceptools import App, Controller, Get, Module, create_app
from inceptools.core.di import Injectable


@Injectable
class GreeterService:
    def greet(self, name: str) -> dict:
        return {"greeting": f"Hello, {name}"}


@Controller("/hello", tags=["greeter"])
class HelloController:
    def __init__(self, service: GreeterService) -> None:
        self.service = service

    @Get("/{name}")
    async def greet(self, name: str) -> dict:
        """Greet somebody."""
        return self.service.greet(name)


@Module(controllers=[HelloController], providers=[GreeterService])
class HelloModule: ...


@App(modules=[HelloModule], name="greeter")
class GreeterApp: ...


app = create_app(service="minimal", version="1.0.0", prefix="/api/v1")
app.mount(GreeterApp)
```

```bash
uvicorn main:app
curl localhost:8000/api/v1/greeter/hello/Ada
```

```json
{
  "data": { "greeting": "Hello, Ada" },
  "meta": {
    "success": true, "statusCode": 200, "message": "Success",
    "traceId": "9b1e…", "requestTime": 1, "route": "/api/v1/greeter/hello/Ada"
  }
}
```

## What you got without asking

- `GET /livez`, `GET /healthz`, `GET /readyz`, `GET /health`
- `X-Request-Id` and `X-Trace-Id` on every response, honouring an inbound
  `traceparent`
- One envelope on every reply, including failures
- Exceptions recovered and classified; nothing unclassified reaches a client
- Access logging for failures and slow requests
- Eager provider construction at startup, and ordered teardown at shutdown
- OpenAPI at `/api/v1/docs` that documents the envelope

## Where the URL comes from

```
create_app(prefix=…)   @App(name=…)   @Module(path=…)   @Controller(prefix)
   /api/v1          +    /greeter   +      (none)     +     /hello
```

The controller prefix is the path. A module contributes no segment unless you
give it `path=`, so `@Controller("/products")` inside `ProductsModule` serves
`/products`, not `/products/products`.

## Next

- [Modules and DI](modules.md) — what a module may reach into
- [Errors and the envelope](errors.md) — the thing you will touch most
- [Testing](testing.md) — through the real pipeline, not around it
