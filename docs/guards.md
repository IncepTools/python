# Guards and interceptors

## Order

```
middleware → guards → interceptors → handler → (unwind) → exception filters
```

Interceptors run **inside** guards: there is no point timing a request that
authorization is about to refuse, and an audit-on-success interceptor must not
record an action that never happened.

## Guards

A guard answers one question: may this caller run this handler? It returns
`None` to allow and raises to refuse — and the raised error's kind picks the
status, so a guard can legitimately answer 402.

```python
from inceptools import guard

@Post("/", guards=[guard.Tenanted, guard.has_role("catalog:write")])
async def create(self, body: CreateProduct): ...
```

| Built-in | Refuses when |
|---|---|
| `guard.Authenticated` | there is no actor — 401 `UNAUTHENTICATED` |
| `guard.Tenanted` | there is no account — 401 `NO_TENANT` |
| `guard.Operator` | the caller is not staff — 403 `OPERATOR_ONLY` |
| `guard.has_role(*roles)` | the actor holds none of them — 403 `ROLE_REQUIRED` |
| `guard.has_scope(*scopes)` | the token carries none of them — 403 `SCOPE_REQUIRED` |
| `guard.Public` | never |

`Tenanted` answers 401, not 403: there is no account on the request at all,
which means we do not know who is asking — not that we know and are refusing.

`Operator` returns deliberately vague prose, so an operator-only route does not
advertise itself.

Guards run in order and the first refusal wins, so put the cheap checks first.
The refusal is tagged with the guard that produced it, so a 403 in a log says
which rule fired.

### Attaching them to a controller

`@UseGuards` works on a class or a method, and the two merge — controller
first, so a controller-wide rule cannot be skipped by adding a narrower one:

```python
@Controller("/invoices")
@UseGuards(RolesGuard)
class InvoiceController:
    @Post("/")
    @Roles("billing:write")
    async def create(self, body: CreateInvoice): ...
```

A guard passed as a **class** is constructed by the container, which is the
only way it can reach a dependency such as the `Reflector`. See
[Metadata and the Reflector](metadata.md) for the full pattern, and for
`APP_GUARD` to apply one to every route.

## Your own

```python
def check_owns_shop(ctx: ExecutionContext) -> None:
    if ctx.path_param("shop_id") not in ctxkit.current().attributes["shops"]:
        raise errs.forbidden("not your shop").with_code("NOT_SHOP_OWNER")

OwnsShop = guard.of("owns-shop", check_owns_shop)
```

`guard.all_of(name, *guards)` and `guard.any_of(name, *guards)` compose them.
`any_of` reports the **last** refusal: it is the most specific thing we can say.

### Why per route

Guards attach per route, never to a group. A group-level guard applies to every
route registered after it, so adding a public route under a protected one
silently exposes it — and a route table that cannot show a route's guards
cannot be tested for unguarded writes.

## Interceptors

An interceptor wraps the handler. It must call `next` exactly once on any path
where the handler should run.

```python
from inceptools import interceptor

create_app(service="shop", interceptors=(interceptor.timing(1.0),))
```

| Built-in | Does |
|---|---|
| `interceptor.timing(seconds)` | warns when a handler exceeds the threshold |
| `interceptor.header(key, value)` | sets a response header after the handler |
| `interceptor.after(name, fn)` | runs `fn(ctx, error)` whether or not it raised |
| `interceptor.on_success(name, fn)` | runs only when the handler returned |

`after`'s own failure is logged and swallowed: an audit hook must not turn a
successful request into a 500.

## Identity

Guards read identity; they do not establish it. That is middleware's job:

```python
def identity(request: Request) -> dict:
    claims = decode(request.headers.get("authorization", ""))
    return {"account_id": claims.get("acc"), "actor_id": claims.get("sub"),
            "roles": frozenset(claims.get("roles", []))}

create_app(service="shop", middleware=ChainOptions(identity=identity))
```

It never authorizes — keeping the two separate is what lets one route be public
and the next one not. Raising from the resolver refuses the request outright.
