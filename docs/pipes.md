# Pipes and parameter decorators

## Pipes

Pydantic already parses and validates types, so a pipe is for what it does not
do: normalising a value, enforcing a rule that is not a type, or applying the
same transform to every parameter of a handler.

```python
@Post("/")
@UsePipes(Trim())                                        # every parameter
async def create(self, body: CreateProduct): ...

async def show(self, pid: Annotated[int, Piped(ParseInt(minimum=1))]): ...
```

A pipe's failure is an `AppError`, so it renders as a field-scoped 400 like any
other validation failure — naming the parameter and the rule it broke.

| Pipe | Does |
|---|---|
| `ParseInt(minimum=, maximum=)` | a whole number, within bounds |
| `ParseFloat()` | a number |
| `ParseBool()` | `1/t/true/yes/y/on` and their opposites |
| `ParseUUID()` | a UUID |
| `ParseEnum(*allowed)` | one of a fixed set |
| `ParseList(item=, separator=)` | a delimited string into a list |
| `DefaultValue(x)` | substitutes when absent |
| `Trim(empty_to_none=)` | strips strings, recursively through models, lists and dicts |

`Trim` is the one that earns its place: `" ada@example.com "` passes an email
rule and then does not match the row it was supposed to find.

### Your own

```python
def Clamp(low: int, high: int):
    return pipe("clamp", lambda value, meta: max(low, min(high, value)))
```

A pipe is anything with `transform(value, meta)`. `meta` carries the parameter
name and annotation, so a shared pipe can report which field it refused.

## Parameter decorators

The extraction is written once and named, so "where does the account come
from" has one answer rather than one per endpoint.

```python
CurrentAccount = create_param_decorator(lambda ctx: ctxkit.require_account())

@Get("/me")
async def me(self, account: str = CurrentAccount()): ...
```

Underneath it is a FastAPI dependency, so it composes with `Depends`, appears
in OpenAPI, and works in a plain FastAPI route too.

Shipped ready-made:

| Decorator | Gives |
|---|---|
| `CurrentAccount()` | the account, or 401 `NO_TENANT` |
| `CurrentActor()` | the actor, or 401 |
| `RequestContext()` | the whole context — trace, account, actor, roles |
| `Roles()` | the actor's roles |
| `TraceId()`, `RequestId()` | correlation ids |
| `Ip()` | the peer address — deliberately not `X-Forwarded-For` |
| `UserAgent()` | the header |
| `Req()` | the raw request, for the awkward cases |

FastAPI's own `Body`, `Query`, `Path`, `Header`, `Cookie`, `Form`, `File` and
`Depends` are re-exported from the same module, so one import covers a
handler's signature.
