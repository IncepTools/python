# Middleware

## The chain

Fixed order, and it is correctness rather than taste:

```
RequestContext → Recover → SecureHeaders → AccessLog → Identity → Timeout
```

- **RequestContext first**, so everything below logs the request id.
- **Recover second**, so an exception anywhere below carries that id.
- **Identity before Timeout**, so an auth failure is not reported as a timeout.

```python
create_app(
    service="shop",
    middleware=ChainOptions(
        timeout=15.0,
        identity=resolve_identity,
        secure_headers=SecureHeadersOptions(hsts_max_age=31536000),
        access_log=AccessLogOptions(slow_threshold=1.0),
    ),
    cors=CORSOptions(allow_origins=("https://app.example.com",)),
    rate_limit=RateLimitOptions(max_requests=100, window=60),
)
```

## Pure ASGI, on purpose

Every middleware here is a plain ASGI callable, not a `BaseHTTPMiddleware`
subclass. That is a correctness decision: `BaseHTTPMiddleware` runs the rest of
the application in a separate task, so a `contextvars` value set in one is
invisible to the endpoint. Request identity set that way would be silently
absent from every log line and every scoped query.

If you write your own middleware, write it the same way:

```python
class TenantFromSubdomain:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            host = dict(scope["headers"]).get(b"host", b"").decode()
            ctxkit.bind(account_id=host.split(".")[0])
        await self.app(scope, receive, send)
```

## Module middleware

ASGI middleware added to the app sees every request. That is wrong for anything
belonging to one area — a legacy-header shim, a body-size cap on uploads only.
A module declares what it wants and where:

```python
@Module(controllers=[UploadController])
class UploadsModule:
    def configure(self, consumer: MiddlewareConsumer) -> None:
        consumer.apply(BodyLimit, max_bytes=50_000_000) \
                .for_routes("/api/v1/uploads/*") \
                .exclude("/api/v1/uploads/status")
```

Patterns are globs: `*` matches within a segment, `**` across segments, and a
pattern with no wildcard must match exactly. `exclude` is checked first.

These run **inside** the framework chain, so a trace id is already on the
context when they execute — which is the reason they are not simply added to
the app.

## Identity

```python
def resolve_identity(request: Request) -> dict:
    claims = decode(request.headers.get("authorization", ""))
    return {
        "account_id": claims.get("acc"),
        "actor_id": claims.get("sub"),
        "roles": frozenset(claims.get("roles", [])),
    }
```

It never authorizes — that is a guard's job, and keeping them separate is what
lets one route be public and the next one not. Raising from the resolver
refuses the request outright.

## What is not trusted by default

- **`X-Forwarded-For`** is caller-supplied; trusting it turns a rate limit into
  a suggestion. Behind a known proxy, install `ProxyHeadersMiddleware`
  explicitly.
- **A wildcard CORS origin with credentials** is refused at boot. Browsers
  ignore the combination, which makes it look configured when it is not.
- **An inbound `X-Request-Id`** *is* trusted — it is how a caller correlates
  its logs with yours — but regenerated when absent or implausibly long, so a
  client cannot push 8KB through every log line.
