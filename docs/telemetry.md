# Telemetry

OpenTelemetry traces for HTTP requests, scheduled jobs, queue attempts and any
function you wrap. Optional: `uv add "inceptools[otel]"`.

```python
from inceptools.core.telemetry import Telemetry

telemetry = Telemetry.configure(service="shop", version="1.4.2", environment="prod")
app = create_app(service="shop", telemetry=telemetry)
```

Set the standard environment variables and it exports:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://ingest.example.com
OTEL_EXPORTER_OTLP_HEADERS='{"signoz-access-token":"…"}'
```

Any OTLP backend works — SigNoz, an OpenTelemetry Collector, Elastic APM,
Jaeger, Grafana, Honeycomb.

## It is a no-op when off

Without the extra installed, or with no endpoint configured, every function
here still works and does nothing:

```python
with telemetry.span("rebuild-index", shard=7):
    await index.rebuild(7)
```

That matters more than it sounds. The alternative is `if telemetry:` at every
call site, and the one place somebody forgets it is the path that was worth
tracing.

A broken exporter configuration logs a warning and the service starts anyway.
Losing traces is bad; losing the deployment is worse.

## Spans carry the request's identity

The current trace id, request id, account and actor are attached
automatically, so a span is correlatable with the log lines beside it without
anyone passing them along.

## HTTP spans are named after the route template

`GET /api/v1/catalog/products/{pid}`, not `GET /api/v1/catalog/products/9c1e…`.
Naming a span after the path produces one unique span name per product and an
unusable trace view.

A failed request's span carries `error.code`, `error.kind` and
`http.response.status_code`, and is marked `ERROR` for a 5xx. The *message* is
not attached: an internal error's message is redacted for the client and should
not leak into a trace either.

## Your own spans

```python
from inceptools.core import telemetry

with telemetry.span("charge-card", gateway="stripe"):
    await gateway.charge(amount)
```

The module-level `span` uses whatever the application installed, so a service
does not have to thread a `Telemetry` through its constructors.

## Shutdown

The application registers the telemetry it created as a lifecycle hook, so
providers are flushed during drain. A short-lived process does not lose its
last spans.
