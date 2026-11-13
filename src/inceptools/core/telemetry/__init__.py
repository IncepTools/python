"""OpenTelemetry traces and metrics for HTTP, jobs, queues and events.

Optional: without the ``otel`` extra installed, every function here is a
working no-op, so instrumented code runs unchanged in a service that has not
turned telemetry on. That matters more than it sounds — the alternative is
``if telemetry:`` at every call site, and the one place somebody forgets it is
the path that was worth tracing.

    telemetry = Telemetry.configure(service="shop", version="1.4.2")
    app = create_app(service="shop", telemetry=telemetry)

Set the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` and optional
``OTEL_EXPORTER_OTLP_HEADERS`` to send to SigNoz, an OpenTelemetry Collector,
Elastic APM, Jaeger, Grafana, Honeycomb or anything else that speaks OTLP.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, TypeVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from inceptools.core import ctxkit, errs

__all__ = [
    "INSTRUMENTATION_NAME",
    "Telemetry",
    "TelemetryMiddleware",
    "available",
    "span",
]

INSTRUMENTATION_NAME = "inceptools"

_log = logging.getLogger("inceptools.telemetry")
T = TypeVar("T")

try:  # pragma: no cover - exercised by whether the extra is installed
    from opentelemetry import metrics as _otel_metrics
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import SpanKind, Status, StatusCode

    _AVAILABLE = True
except ImportError:  # pragma: no cover
    _otel_metrics = None  # type: ignore[assignment]
    _otel_trace = None  # type: ignore[assignment]
    SpanKind = Status = StatusCode = None  # type: ignore[assignment,misc]
    _AVAILABLE = False


def available() -> bool:
    """True when the OpenTelemetry SDK is importable."""
    return _AVAILABLE


@dataclass
class Telemetry:
    """Providers, plus the shutdown that flushes them.

    Nothing is global here: the application holds the instance and flushes it
    during drain, so a short-lived process does not lose its last spans.
    """

    service: str = "service"
    version: str = ""
    environment: str = ""
    tracer_provider: Any = None
    meter_provider: Any = None
    _tracer: Any = field(default=None, repr=False)
    _meter: Any = field(default=None, repr=False)
    _own_providers: bool = field(default=False, repr=False)

    # -- construction ----------------------------------------------------

    @classmethod
    def configure(
        cls,
        *,
        service: str,
        version: str = "",
        environment: str = "",
        endpoint: str = "",
        headers: dict[str, str] | None = None,
        tracer_provider: Any = None,
        meter_provider: Any = None,
        console: bool = False,
    ) -> Telemetry:
        """Build providers, or adopt the ones you pass.

        With no endpoint configured and no provider supplied, this returns a
        no-op instance rather than failing: telemetry being unconfigured is a
        normal state, not an error.
        """
        if not _AVAILABLE:
            _log.debug("opentelemetry is not installed; telemetry is a no-op")
            return cls(service=service, version=version, environment=environment)

        if tracer_provider is not None or meter_provider is not None:
            instance = cls(
                service=service,
                version=version,
                environment=environment,
                tracer_provider=tracer_provider,
                meter_provider=meter_provider,
            )
            instance._bind()
            return instance

        resolved_endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if not resolved_endpoint and not console:
            return cls(service=service, version=version, environment=environment)

        try:
            instance = cls(
                service=service,
                version=version,
                environment=environment,
                **_build_providers(
                    service, version, environment, resolved_endpoint, headers, console
                ),
            )
        except Exception as exc:
            # A broken exporter configuration must not stop the service
            # starting. Losing traces is worse than nothing; losing the
            # deployment is worse than that.
            _log.warning("telemetry could not be configured; continuing without it", exc_info=exc)
            return cls(service=service, version=version, environment=environment)

        instance._own_providers = True
        instance._bind()
        return instance

    def _bind(self) -> None:
        if not _AVAILABLE:
            return
        if self.tracer_provider is not None:
            self._tracer = self.tracer_provider.get_tracer(INSTRUMENTATION_NAME)
        if self.meter_provider is not None:
            self._meter = self.meter_provider.get_meter(INSTRUMENTATION_NAME)

    @property
    def enabled(self) -> bool:
        return _AVAILABLE and self.tracer_provider is not None

    # -- spans -----------------------------------------------------------

    @contextlib.contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Any]:
        """A span around a block. A no-op when telemetry is off.

        The current request's trace, account and actor are attached
        automatically, so a span is correlatable with the log lines beside it.
        """
        if self._tracer is None:
            yield None
            return

        context = ctxkit.current()
        merged = {k: v for k, v in attributes.items() if v is not None}
        merged.update({f"inceptools.{k}": v for k, v in context.log_fields().items() if v})

        with self._tracer.start_as_current_span(name, kind=SpanKind.INTERNAL) as active:
            for key, value in merged.items():
                active.set_attribute(key, value)
            try:
                yield active
            except BaseException as exc:
                self.record(active, exc)
                raise
            else:
                active.set_status(Status(StatusCode.OK))

    def record(self, active: Any, exc: BaseException) -> None:
        """Mark a span failed, without putting a redacted message on it."""
        if active is None or not _AVAILABLE:
            return
        error = errs.from_exception(exc)
        with contextlib.suppress(Exception):
            active.set_attribute("error.code", error.code)
            active.set_attribute("error.kind", error.kind.value)
            active.set_attribute("http.status_code", error.status_code)
            active.record_exception(exc)
            active.set_status(Status(StatusCode.ERROR, error.code))

    async def run(self, name: str, fn: Callable[[], Awaitable[T]], **attributes: Any) -> T:
        """Run *fn* inside a span."""
        with self.span(name, **attributes):
            return await fn()

    # -- lifecycle -------------------------------------------------------

    async def on_module_destroy(self) -> None:
        """Flush and shut down the providers this instance created."""
        if not self._own_providers:
            return
        for provider in (self.tracer_provider, self.meter_provider):
            if provider is None:
                continue
            with contextlib.suppress(Exception):
                shutdown = getattr(provider, "shutdown", None)
                if shutdown is not None:
                    shutdown()

    def middleware(self) -> tuple[Any, dict[str, Any]]:
        """The ASGI middleware class and its arguments."""
        return TelemetryMiddleware, {"telemetry": self}


def _build_providers(
    service: str,
    version: str,
    environment: str,
    endpoint: str,
    headers: dict[str, str] | None,
    console: bool,
) -> dict[str, Any]:  # pragma: no cover - requires the optional extra
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    attributes = {"service.name": service}
    if version:
        attributes["service.version"] = version
    if environment:
        attributes["deployment.environment"] = environment
    resource = Resource.create(attributes)

    tracer_provider = TracerProvider(resource=resource)
    meter_provider = None

    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers))
        )
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", headers=headers)
                )
            ],
        )
    if console:
        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    return {"tracer_provider": tracer_provider, "meter_provider": meter_provider}


class TelemetryMiddleware:
    """A span per HTTP request.

    The route **template** is used as the span name, not the path: naming a
    span after `/products/9c1e…` produces one unique span name per product and
    an unusable trace view.
    """

    def __init__(self, app: ASGIApp, *, telemetry: Telemetry) -> None:
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.telemetry.enabled:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        status = 500

        async def recording_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        route = scope.get("path", "")
        with self.telemetry.span(
            f"{method} {route}",
            **{"http.request.method": method, "url.path": scope.get("path", "")},
        ) as active:
            try:
                await self.app(scope, receive, recording_send)
            except BaseException as exc:
                if isinstance(exc, Exception):
                    self.telemetry.record(active, exc)
                raise
            if active is not None:
                with contextlib.suppress(Exception):
                    template = _route_template(scope) or route
                    active.update_name(f"{method} {template}")
                    active.set_attribute("http.route", template)
                    active.set_attribute("http.response.status_code", status)
                    if status >= 500:
                        active.set_status(Status(StatusCode.ERROR))


def _route_template(scope: Scope) -> str:
    """``/api/v1/catalog/products/{pid}``, once the route has matched.

    The router publishes the full template, because Starlette's own matched
    route only knows its router-local path.
    """
    from inceptools.web.router import ROUTE_TEMPLATE_KEY

    published = scope.get(ROUTE_TEMPLATE_KEY)
    if published:
        return str(published)
    matched = scope.get("route")
    return getattr(matched, "path", "") if matched is not None else ""


#: A module-level convenience for code that has no Telemetry to hand. It is a
#: no-op unless an application installed one.
_active: Telemetry | None = None


def set_active(telemetry: Telemetry | None) -> None:
    global _active
    _active = telemetry


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span on the application's telemetry, or a no-op."""
    if _active is None:
        yield None
        return
    with _active.span(name, **attributes) as active:
        yield active
