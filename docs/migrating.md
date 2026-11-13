# Migrating an existing FastAPI service

Adoption is incremental. You can take the envelope without the modules, or the
modules without the data layer. Nothing here requires a rewrite.

## The order that costs least

1. **The envelope and the exception filter.** One shape, one leak check.
2. **Settings and logging.** Correlation that survives a thread pool.
3. **Controllers and modules.** Where the structural win is.
4. **The scoped repository.** Where the correctness win is.

## 1. Keep your wire contract

A fleet already returning `{data, errors, success, statusCode, requestTime,
message, sessionId}` does not have to change it on adoption day:

```python
from inceptools import EnvelopeStyle, ResponseOptions

app = create_app(
    service="chat",
    prefix="/chat/api",
    response=ResponseOptions(style=EnvelopeStyle.FLAT),
)
```

Switch to the nested style later, deliberately, as a versioned change.

### Replace status-sniffing with typed errors

A middleware that picks a status by searching the exception message —

```python
if "timeout" in str(exc).lower():        # don't
    status = 504
```

— guesses, and guesses wrong the first time a message contains the word
"timeout" for another reason. Raise the kind instead:

```python
raise errs.timeout("the upstream model did not answer").with_code("MODEL_TIMEOUT")
raise errs.unavailable("vector store unreachable").with_code("QDRANT_DOWN")
```

### Replace the duck-typed wrap check

A middleware that re-wraps any `JSONResponse` whose body lacks a `success` key
will double-wrap a payload that happens to contain one. The framework marks its
own responses explicitly, so `response.is_enveloped(response)` is a fact rather
than a guess.

### Two rival exception classes

Where a service has grown two rival exception classes — a rich one and a thin
one — and the middleware catches only one of them: raise `errs.AppError` and
delete both. Whatever mattered on them, usually a machine-readable reason
code, becomes `.with_code(...)`.

## 2. Correlation that survives a thread pool

```python
threading.current_thread().name = trace_id      # don't
```

This works right up until anything runs in a thread pool or an
`asyncio.to_thread` call, and then it labels one request's log lines with
another request's id. The framework puts identity on a `contextvar` and
injects it with a logging **filter**, which is correct everywhere:

```python
from inceptools.core import logx

logx.configure(logx.LoggingOptions(
    service="chat", env="UAT", log_dir="/var/log/app", json_to_stream=True,
))
```

You keep the rotating human file and the ELK JSON file, including the
`@timestamp` / `logger_name` / `request.id` fields the dashboards filter on.

One trap worth knowing: `extra={"module": ...}` raises, because `module` is a
`LogRecord` attribute. Use `logx.safe_extra({...})`.

## 3. Settings

Keep whatever settings source you have — a secrets manager, a config service,
a mounted file. `Settings` is a `pydantic-settings` class, so
`settings_customise_sources` works unchanged:

```python
class AppSettings(Settings):
    model_config = Settings.configure(case_sensitive=True)

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        return (init_settings, SecretsManagerSource(settings_cls), env_settings)
```

What you gain: every problem reported at once, and `describe()` that masks
secrets so a startup summary is safe to log.

## 4. Controllers

A common pattern is a decorator that registers a class holding a
module-level `APIRouter`, with `@staticmethod` handlers — the class is a
namespace and `self` is never used. It becomes a real class:

```python
# before
@RegisterController()
class ChatRoutes:
    router = APIRouter(prefix=f"{settings.API_PREFIX}/api/chat")

    @router.post("/")
    async def chat(request: Request, response: ResponseModel = Depends(trace_request)):
        ...

# after
@Controller(f"{settings.API_PREFIX}/api/chat", tags=["chat"])
class ChatController:
    def __init__(self, service: ChatService) -> None:
        self.service = service

    @Post("/", guards=[guard.Authenticated])
    async def chat(self, body: ChatRequest):
        return await self.service.reply(body)
```

The envelope is no longer injected per handler — `trace_request` disappears,
because the funnel already has the trace id.

There is no `pkgutil` autoloader and no import-for-side-effect: modules are
listed on an `@App`. Routes therefore exist **before** startup, so
`app.routes`, OpenAPI and a `TestClient` all work without running the app —
which is what makes the `os.environ.setdefault` prologues in the current tests
unnecessary.

## 5. Services and the data layer

**Make construction lazy.** A database wrapper whose `__init__` runs a health check
and raises at import time is what forces the import-time env stuffing in tests.
Register it as a provider instead; it is constructed at startup, where failing
is the correct outcome, and tests never trigger it.

```python
app.provide(factory(Engine, build_engine))
```

**Drop the module-level singletons.** `mysql = DatabaseManager(...)` at import
time becomes a provider; anything needing it takes it on `__init__`.

Then adopt the repository where tenancy matters — see
[The scoped repository](database.md).

## 6. Keep what is deliberate

Some things look like duplication and are not. Where one service validates
against an external identity provider rather than a shared internal token,
that may be an architecture decision rather than boilerplate. Leave the
identity resolver pluggable and keep calling whatever you call now.

```python
def identity(request: Request) -> dict:
    claims = validate_with_support_api(request.headers.get("authorization", ""))
    return {"account_id": claims["account"], "actor_id": claims["user"]}

create_app(service="…", middleware=ChainOptions(identity=identity))
```

## What to check first

```python
def test_every_write_is_guarded(client):
    client.expect_all_writes_guarded()
```

Run it against the service you are migrating before you change anything else.
It is the cheapest thing in this document and it is the one most likely to find
something.
