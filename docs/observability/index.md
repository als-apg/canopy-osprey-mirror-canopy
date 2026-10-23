# Observability

Canopy ships a lightweight observability pipeline:

```
canopy-api / canopy-worker write JSON log lines
    -> Promtail tails the log files
    -> Loki stores + indexes them
    -> Grafana dashboards query Loki

canopy-api exposes Prometheus metrics at /metrics
    -> Prometheus scrapes them
    -> Grafana queries Prometheus
```

Everything runs from one compose overlay — see the [setup guide](setup_guide.md).

## Structured logging

Both process types call `canopy.core.logging.configure_logging()` at startup.
Configuration comes from core settings:

| Env var | Default | Meaning |
|---|---|---|
| `CANOPY_LOG_FORMAT` | `text` | `text` (human-readable) or `json` (one JSON object per line) |
| `CANOPY_LOG_PATH` | unset | When set, also log to this file (directory auto-created, rotated at 50 MiB with 3 backups kept); Promtail tails it |
| `CANOPY_LOG_LEVEL` | `INFO` | Root log level |

In `json` mode each line carries `ts`, `level`, `logger`, `message`, plus any
`extra={...}` attributes from the call site as first-class fields. Exceptions
are rendered into an `error` field. Those five names are reserved: an `extra`
field that collides is written as `extra_<name>` rather than overwriting. The
docker compose stack enables JSON logging to `/tmp/canopy-logs/` by default.

Framework loggers are folded in: uvicorn's access and error lines and arq's
worker lines go through the same handlers, so HTTP access logs reach the file
too, and `CANOPY_LOG_LEVEL` governs them.

## Frontend analytics + error ingestion

Frontends POST usage and error events to the **analytics** module:

```
POST /analytics/v1/events
Content-Type: application/json

{
  "event": "screen_opened",
  "sessionId": "abc123",          // optional
  "route": "/screens/demo",       // optional
  "path": "/screens/demo?x=1",    // optional
  "clientTs": "2026-07-20T12:00:00Z",  // optional
  "properties": { "screen": "demo" }
}
```

- **Auth is optional**: anonymous events are accepted; a valid `X-API-Key`
  only enriches the event with scope metadata (`has_api_key`,
  `api_key_scopes`). The key token itself is never logged. An invalid key
  does not cause a 4xx — ingestion must never break a client workflow.
- Returns `204 No Content`. Malformed bodies get a standard `422`; bodies over
  `CANOPY_ANALYTICS__MAX_EVENT_BYTES` (default 64 KiB) get `413`, so keep
  stack traces trimmed.
- `sessionId` and `properties` are stored verbatim in the log. Treat
  `sessionId` as a pseudonymous identifier and keep personal data (names,
  email addresses, free-text user input) out of `properties`.
- Error reporting convention: send `event: "frontend_error"` with
  `properties.error` / `properties.stack` — the *Canopy Error Reporting*
  dashboard keys off that name.
- Kill switch: `CANOPY_ANALYTICS__ENABLED=false` accepts but drops events.

Each accepted event becomes one structured log line from the
`canopy.analytics` logger with `type="analytics"`, which is how dashboards
select analytics traffic.

## Label discipline

Promtail indexes only low-cardinality fields as Loki labels: `app`, `job`,
`level`, `type`, `source`. Unbounded fields (`event`, `route`, `session_id`,
`properties.*`) are parsed at query time with LogQL's `| json` — keeping
Loki's index small. Follow the same rule when adding new log fields.

## Metrics

The parent app serves an unauthenticated `GET /metrics` in Prometheus
exposition format: request counts and latency histograms for every module's
routes, labelled by module and route template rather than raw path, plus
module gauges read from the same code that feeds `/health/summary` (Hare's
client, monitor and subscription counts to start). The overlay's Prometheus
scrapes it and Grafana gets a provisioned datasource. Naming, label and
collection rules for adding module metrics are on the [metrics page](metrics.md).

## Dashboards

Grafana is auto-provisioned with Loki and Prometheus datasources and two
Loki-backed dashboards (*Canopy Frontend Analytics*, *Canopy Error Reporting*) from
`docker/observability/grafana/`. Edit the JSON there (or export from the
Grafana UI and copy back) to evolve them under review.
