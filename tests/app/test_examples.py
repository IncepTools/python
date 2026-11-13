"""The examples must keep working, or they are documentation that lies."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from inceptools.testkit import TestApp

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def load(name: str):
    sys.path.insert(0, str(EXAMPLES / name))
    try:
        for stale in [m for m in sys.modules if m == "main"]:
            del sys.modules[stale]
        import main

        return main
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize(("name", "routes"), [("minimal", 1), ("todo", 5), ("worker", 0)])
def test_every_example_builds(name, routes):
    assert len(load(name).app.routes) == routes


def test_minimal_serves_its_one_route():
    with TestApp(load("minimal").app) as client:
        client.get("/api/v1/greeter/hello/Ada").expect_ok().expect_data({"greeting": "Hello, Ada"})


def test_todo_refuses_an_anonymous_caller():
    """Not an empty list — a 401. That is the scoped repository working."""
    with TestApp(load("todo").app) as client:
        client.get("/api/v1/todo/tasks").expect_status(401).expect_code("NO_TENANT")


def test_todo_isolates_accounts():
    with TestApp(load("todo").app) as client:
        acc1 = {"x-account": "acc_1"}
        acc2 = {"x-account": "acc_2"}

        created = client.post("/api/v1/todo/tasks", {"title": "Write the docs"}, headers=acc1)
        created.expect_status(201)
        task_id = created.data["id"]

        assert client.get("/api/v1/todo/tasks", headers=acc1).data[0]["id"] == task_id
        assert client.get("/api/v1/todo/tasks", headers=acc2).data == []
        client.get(f"/api/v1/todo/tasks/{task_id}", headers=acc2).expect_status(404)


def test_todo_validates_its_dto():
    with TestApp(load("todo").app) as client:
        reply = client.post("/api/v1/todo/tasks", {"title": "a"}, headers={"x-account": "acc_1"})
        reply.expect_status(400).expect_field_error("title")


def test_todo_paginates():
    with TestApp(load("todo").app) as client:
        headers = {"x-account": "acc_p"}
        for n in range(5):
            client.post("/api/v1/todo/tasks", {"title": f"task {n}"}, headers=headers)
        reply = client.get("/api/v1/todo/tasks?page=2&limit=2", headers=headers)
        reply.expect_ok()
        assert reply.meta["total"] == 5
        assert reply.meta["pages"] == 3
        assert len(reply.data) == 2


def test_todo_can_set_done_to_false_again():
    """The PATCH that un-does something must actually change a column."""
    with TestApp(load("todo").app) as client:
        headers = {"x-account": "acc_b"}
        task_id = client.post("/api/v1/todo/tasks", {"title": "flip"}, headers=headers).data["id"]
        assert (
            client.patch(f"/api/v1/todo/tasks/{task_id}", {"done": True}, headers=headers).data[
                "done"
            ]
            is True
        )
        assert (
            client.patch(f"/api/v1/todo/tasks/{task_id}", {"done": False}, headers=headers).data[
                "done"
            ]
            is False
        )


def test_every_example_guards_its_writes():
    for name in ("minimal", "todo", "worker"):
        app = load(name).app
        assert not app.routes.unguarded(), name
