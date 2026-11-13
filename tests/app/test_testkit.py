"""The testkit goes through the real pipeline."""

from __future__ import annotations

import pytest


def test_identity_headers_drive_the_guards(client):
    client.post("/api/v1/catalog/products", {"name": "ab", "price": 1}).expect_status(401)
    client.as_account("acc_1").post(
        "/api/v1/catalog/products", {"name": "ab", "price": 1}
    ).expect_status(201)


def test_acting_as_is_scoped(client):
    with client.acting_as(account_id="acc_1", roles=["catalog:write"]):
        client.delete("/api/v1/catalog/products/1").expect_status(200)
    client.delete("/api/v1/catalog/products/1").expect_status(401)


def test_anonymous_clears_identity(client):
    client.as_account("acc_1").anonymous()
    client.post("/api/v1/catalog/products", {"name": "ab", "price": 1}).expect_status(401)


def test_reply_exposes_the_envelope(client):
    reply = client.get("/api/v1/catalog/products/1")
    assert reply.data["name"] == "Desk lamp"
    assert reply.meta["success"] is True
    assert reply.code == ""


def test_expectations_fail_loudly(client):
    with pytest.raises(AssertionError, match="expected status 500"):
        client.get("/api/v1/catalog/products/1").expect_status(500)


def test_all_writes_guarded_assertion(client):
    client.expect_all_writes_guarded()


def test_the_container_is_reachable_for_stubbing(client):
    from tests.conftest import ProductRepo

    repo = client.container.get(ProductRepo)
    repo.rows[42] = {"id": 42, "name": "Injected", "archived": False}
    client.get("/api/v1/catalog/products/42").expect_data(
        {"id": 42, "name": "Injected", "archived": False}
    )
