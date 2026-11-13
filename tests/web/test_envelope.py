"""Every reply leaves through one funnel, in one shape."""

from __future__ import annotations

from inceptools.core import ctxkit, errs
from inceptools.web import response


def test_success_is_enveloped(client):
    reply = client.get("/api/v1/catalog/products/1")
    reply.expect_status(200).expect_ok().expect_enveloped()
    assert reply.data == {"id": 1, "name": "Desk lamp", "archived": False}
    assert reply.meta["statusCode"] == 200
    assert reply.meta["route"] == "/api/v1/catalog/products/1"
    assert reply.meta["traceId"]


def test_a_typed_failure_carries_its_code_and_status(client):
    client.get("/api/v1/catalog/products/99").expect_status(404).expect_failure().expect_code(
        "PRODUCT_NOT_FOUND"
    ).expect_message("no such product")


def test_a_failure_never_carries_data(client):
    assert client.get("/api/v1/catalog/products/99").data is None


def test_an_unclassified_error_is_redacted(client):
    """The one that matters: a driver error must not reach a client."""
    reply = client.get("/api/v1/catalog/products/crash/now")
    reply.expect_status(500).expect_code("INTERNAL_ERROR")
    assert "driver error" not in reply.message
    assert reply.message == errs.OPAQUE_MESSAGE


def test_internal_errors_withhold_field_detail():
    error = errs.internal().with_details(errs.FieldError("password_hash", "too short", "min"))
    assert response.fail_envelope(error).meta.errors == []


def test_post_answers_201_by_default(client, tenant):
    tenant.post("/api/v1/catalog/products", {"name": "Chair", "price": 1}).expect_status(201)


def test_trace_id_is_echoed_in_headers(client):
    reply = client.get("/api/v1/catalog/products/1", headers={"x-request-id": "req-abc"})
    reply.expect_header("x-request-id", "req-abc")
    assert reply.meta["traceId"] == "req-abc"


def test_a_w3c_traceparent_supplies_the_trace_id(client):
    parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    reply = client.get("/api/v1/catalog/products/1", headers={"traceparent": parent})
    assert reply.meta["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_an_invalid_traceparent_is_ignored(client):
    bad = "00-" + "0" * 32 + "-00f067aa0ba902b7-01"
    reply = client.get("/api/v1/catalog/products/1", headers={"traceparent": bad})
    assert reply.meta["traceId"] != "0" * 32


def test_the_flat_style_renders_the_legacy_shape():
    options = response.ResponseOptions(style=response.EnvelopeStyle.FLAT)
    with ctxkit.use(trace_id="t1"):
        body = response.envelope_of_flat = response.Envelope(
            data={"a": 1}, meta=response.build_meta(200, "Success", options=options)
        ).as_dict(options)
    assert sorted(body) == [
        "data",
        "errors",
        "message",
        "requestTime",
        "sessionId",
        "statusCode",
        "success",
    ]


def test_204_is_not_enveloped():
    assert response.no_content().status_code == 204
    assert response.no_content().body == b""
