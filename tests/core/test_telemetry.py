"""Telemetry is optional, and its absence must change nothing."""

from __future__ import annotations

import pytest

from inceptools import ChainOptions, Options, create_app, testkit
from inceptools.core import errs
from inceptools.core.telemetry import Telemetry, available


def test_an_unconfigured_telemetry_is_a_working_no_op():
    """No `if telemetry:` at the call site — that is the one people forget."""
    telemetry = Telemetry.configure(service="shop")
    assert telemetry.enabled is False
    with telemetry.span("work", customer="acc_1") as active:
        assert active is None


async def test_run_still_runs_without_a_provider():
    telemetry = Telemetry.configure(service="shop")
    assert await telemetry.run("work", lambda: _answer()) == 42


async def _answer() -> int:
    return 42


async def test_an_exception_propagates_through_a_span():
    telemetry = Telemetry.configure(service="shop")
    with pytest.raises(errs.AppError), telemetry.span("work"):
        raise errs.conflict("nope")


@pytest.mark.skipif(not available(), reason="the otel extra is not installed")
def test_a_configured_telemetry_produces_spans():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    telemetry = Telemetry.configure(service="shop", tracer_provider=provider)
    assert telemetry.enabled

    with telemetry.span("do-work", customer="acc_1"):
        pass

    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["do-work"]
    assert spans[0].attributes["customer"] == "acc_1"


@pytest.mark.skipif(not available(), reason="the otel extra is not installed")
def test_http_spans_are_named_after_the_route_template():
    """Naming a span after the path gives one span name per product id."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from tests.conftest import CatalogApp

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Telemetry.configure(service="shop", tracer_provider=provider)

    app = create_app(
        Options(
            service="shop",
            prefix="/api/v1",
            configure_logging=False,
            telemetry=telemetry,
            middleware=ChainOptions(identity=testkit.identity_from_headers),
        )
    )
    app.mount(CatalogApp)

    with testkit.TestApp(app) as client:
        client.get("/api/v1/catalog/products/1").expect_ok()

    names = [s.name for s in exporter.get_finished_spans()]
    assert "GET /api/v1/catalog/products/{pid}" in names


@pytest.mark.skipif(not available(), reason="the otel extra is not installed")
def test_a_failed_request_marks_its_span():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Telemetry.configure(service="shop", tracer_provider=provider)

    with pytest.raises(errs.AppError), telemetry.span("charge"):
        raise errs.unavailable("gateway down").with_code("GATEWAY_DOWN")

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.code"] == "GATEWAY_DOWN"
