# Controllers and routes

A controller is a real class with constructor-injected dependencies, so a
handler is an instance method that can reach its service without a global.

```python
@Controller("/products", tags=["products"])
class ProductController:
    def __init__(self, service: ProductService) -> None:
        self.service = service

    @Get("/{pid}", response_model=ProductOut)
    async def retrieve(self, pid: int):
        """Fetch one product."""        # becomes the OpenAPI summary
        return await self.service.get(pid)
```

`@Get`, `@Post`, `@Put`, `@Patch`, `@Delete`, `@Head`, `@Options`.

Handlers keep their FastAPI signature, so query parameters, path parameters,
bodies, `Depends` and OpenAPI generation all behave exactly as if the method
had been registered directly. Ask for the request when you need it:

```python
@Get("/")
async def index(self, request: Request, limit: int = 20): ...
```

## Decorator options

| Option | Effect |
|---|---|
| `guards=[...]` | Authorization for this route. Visible in the route table. |
| `interceptors=[...]` | Around-the-handler concerns. |
| `dependencies=[Depends(...)]` | FastAPI dependencies, run before the guards. |
| `status_code=` | Overrides the default (201 for POST, 200 otherwise). |
| `public=True` | Marks a mutating route as deliberately unauthenticated. |
| `raw=True` | Return the handler's value untouched — a file, a redirect, SSE. |
| `response_model=` | Documented under `data` in the OpenAPI envelope. |
| `deprecated=True`, `include_in_schema=False` | As FastAPI. |

A handler returning a `Response` is never wrapped again, so a file download or
an SSE stream is not corrupted.

## Routes are values

`@Controller` collects every declared route onto the class, and mounting
records each one in a registry:

```python
app.route_table()      # aligned, sorted, diffable
app.routes_json()      # the machine-readable dump icpt reads
app.routes.unguarded() # mutating routes with no guard and no public marker
```

Because a route is a value rather than a side effect, the same declaration
mounts it, prints it, documents it and lets a test assert on it.

## Sending a reply yourself

Most handlers return a value and the funnel wraps it. When you need control:

```python
from inceptools.web import response

return response.created(product, location=f"/products/{product.id}")
return response.listing(items, page=1, page_size=20, total=134)
return response.no_content()
```
