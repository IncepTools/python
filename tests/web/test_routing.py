"""Routes are values, and the route table is the security control."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from inceptools import App, Controller, Get, Module, Post, Request, testkit
from inceptools.web.router import Registry, Route, controller_routes, get, post


def test_every_mutating_route_is_guarded_or_public(client):
    """The cheapest security control in the framework."""
    unguarded = [m.full_path for m in client.routes.unguarded()]
    assert unguarded == []


def test_an_unguarded_write_is_reported():
    @Controller("/things")
    class Leaky:
        @Post("/")
        async def create(self):
            return {}

    @Module(controllers=[Leaky])
    class M: ...

    @App(modules=[M], name="leaky")
    class A: ...

    with testkit.build(A) as app:
        assert [m.full_path for m in app.routes.unguarded()] == ["/leaky/things"]


def test_a_public_write_is_not_reported_as_unguarded(client):
    paths = [m.full_path for m in client.routes.all() if m.route.public]
    assert "/api/v1/catalog/products/signup" in paths


def test_the_route_table_is_sorted_and_stable(client):
    first = client.app.route_table()
    assert first == client.app.route_table()
    assert "catalog.products" in first


def test_routes_json_names_guards(client):
    rows = client.routes.as_dicts()
    create = next(r for r in rows if r["method"] == "POST" and r["full_path"].endswith("products"))
    assert create["guards"] == ["tenanted"]
    assert create["module"] == "catalog.products"


def test_route_builders_return_copies():
    base = get("/", lambda: None)
    tagged = base.summary("List").tag("products")
    assert base.doc.summary == ""
    assert tagged.doc.summary == "List"
    assert tagged.doc.tags == ("products",)


def test_a_route_normalises_its_path_and_method():
    assert Route(method="get", path="items/").path == "/items"
    assert Route(method="get", path="items/").method == "GET"


def test_mutating_methods_are_known():
    assert post("/").is_mutating
    assert not get("/").is_mutating


def test_guards_run_in_order_and_the_first_refusal_wins(client):
    """Delete needs tenant *and* role; the tenant check is listed first."""
    anonymous = client.delete("/api/v1/catalog/products/1")
    anonymous.expect_status(401).expect_code("NO_TENANT")

    tenanted = client.as_account("acc_1").delete("/api/v1/catalog/products/1")
    tenanted.expect_status(403).expect_code("ROLE_REQUIRED")


def test_a_guarded_route_runs_when_every_guard_passes(client):
    client.as_actor("u", account_id="acc_1", roles="catalog:write")
    client.delete("/api/v1/catalog/products/1").expect_status(200).expect_data({"deleted": 1})


def test_handlers_keep_their_fastapi_signature(client):
    """Query parameters, path parameters and bodies still bind."""
    client.get("/api/v1/catalog/products?limit=1").expect_ok()
    client.get("/api/v1/catalog/products/notanint").expect_status(400).expect_field_error("pid")


class Payload(BaseModel):
    name: str


def test_string_annotations_still_bind():
    """A module with `from __future__ import annotations` must still work.

    This file uses it, so `body: Payload` is the string "Payload" at runtime.
    Handing that to FastAPI unresolved silently stops the body being bound.
    """

    @Controller("/echo")
    class EchoController:
        @Post("/", public=True)
        async def echo(self, body: Payload) -> dict:
            return {"name": body.name}

    @Module(controllers=[EchoController])
    class M: ...

    @App(modules=[M], name="e")
    class A: ...

    with testkit.build(A) as app:
        app.post("/e/echo", {"name": "ok"}).expect_status(201).expect_data({"name": "ok"})


def test_a_handler_may_ask_for_the_request():
    @Controller("/raw")
    class RawController:
        @Get("/")
        async def show(self, request: Request) -> dict:
            return {"path": request.url.path}

    @Module(controllers=[RawController])
    class M: ...

    @App(modules=[M], name="r")
    class A: ...

    with testkit.build(A) as app:
        app.get("/r/raw").expect_data({"path": "/r/raw"})


def test_a_handler_returning_a_response_is_not_double_wrapped():
    from starlette.responses import JSONResponse

    @Controller("/file")
    class FileController:
        @Get("/", raw=True)
        async def download(self):
            return JSONResponse({"raw": True})

    @Module(controllers=[FileController])
    class M: ...

    @App(modules=[M], name="f")
    class A: ...

    with testkit.build(A) as app:
        assert app.get("/f/file").body == {"raw": True}


def test_an_empty_registry_says_so():
    assert Registry().table() == "no routes mounted"


async def test_mounting_a_controller_class_is_refused():
    from fastapi import APIRouter

    from inceptools.core import errs
    from inceptools.web.router import MountOptions, mount_controller

    @Controller("/x")
    class X:
        @Get("/")
        async def index(self):
            return {}

    with pytest.raises(errs.AppError, match="construct it through the container"):
        await mount_controller(APIRouter(), X, MountOptions())


def test_an_unresolvable_annotation_fails_at_mount_time():
    """A model defined inside a function cannot be named by a string
    annotation. Fail loudly at boot rather than answering 400 forever."""
    from inceptools.core import errs

    class Local(BaseModel):
        name: str

    @Controller("/local")
    class LocalController:
        @Post("/", public=True)
        async def create(self, body: Local) -> dict:
            return {}

    @Module(controllers=[LocalController])
    class M: ...

    @App(modules=[M], name="l")
    class A: ...

    with pytest.raises(errs.AppError, match="move the model out of the enclosing function"):
        testkit.build(A)


def test_controller_routes_are_sorted_deterministically(client):
    paths = [r.path for r in controller_routes(client.app._built[0].controllers[0])]
    assert paths == sorted(paths)
