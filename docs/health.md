# Health checks

Three endpoints, because they answer different questions. Their bodies are
deliberately **not** enveloped: a probe is not an API client.

| Endpoint | Question | Checks | Status |
|---|---|---|---|
| `GET /livez` | is this process alive? | nothing | always 200 |
| `GET /healthz` | (compatibility alias) | nothing | always 200 |
| `GET /readyz` | should this replica get traffic? | everything | 200 for `UP`/`DEGRADED`, **503** for `DOWN` |
| `GET /health` | the detail, for a human | everything | always 200 |

A liveness probe that checks the database restarts every replica when the
database blips — which is the one moment you need them all up. That is why
`/livez` checks nothing.

Health routes sit **outside** the API prefix: a probe should not have to know
your API version.

## Registering a check

```python
from inceptools.web import health

app.health.register(
    health.of("database", engine_ping),
    health.of("cache", redis.ping, severity=health.Severity.OPTIONAL),
)
```

Or from a module:

```python
@Module(controllers=[...], health_checks=[health.of("search", client.ping)])
class SearchModule: ...
```

## Severity

| Severity | A failure means | Report |
|---|---|---|
| `CRITICAL` (default) | this replica cannot serve | `DOWN` → `/readyz` answers 503 |
| `OPTIONAL` | degraded, keep taking traffic | `DEGRADED` → `/readyz` still answers 200 |

A dead optional cache is not a reason to leave the load balancer.

## Running

Checks run **concurrently**, each under its own timeout (2s by default), so one
hanging dependency cannot hang the probe — it is reported as failed instead.

```json
{
  "status": "DEGRADED",
  "service": "shop", "version": "1.4.2", "uptime": "3h12m4s",
  "checks": {
    "database": { "name": "database", "status": "UP", "latencyMs": 2 },
    "cache": { "name": "cache", "status": "DEGRADED", "latencyMs": 2001,
               "message": "check timed out after 2s", "severity": "optional" }
  }
}
```

`/health` names your dependencies. Put it behind auth.
