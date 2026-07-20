# Observability

Canopy ships a lightweight, fully open-source observability pipeline
(mirroring the approach proven in react-squirrel-backend PR #82):

```
canopy-api / canopy-worker write JSON log lines
    -> Promtail tails the log files
    -> Loki stores + indexes them
    -> Grafana dashboards query Loki
```

Everything runs from one compose overlay — see the [setup guide](setup_guide.md).

## Structured logging

Both process types call `canopy.core.logging.configure_logging()` at startup.
Configuration comes from core settings:

| Env var | Default | Meaning |
|---|---|---|
| `CANOPY_LOG_FORMAT` | `text` | `text` (human-readable) or `json` (one JSON object per line) |
| `CANOPY_LOG_PATH` | unset | When set, also log to this file (directory auto-created); Promtail tails it |
| `CANOPY_LOG_LEVEL` | `INFO` | Root log level |

In `json` mode each line carries `ts`, `level`, `logger`, `message`, plus any
`extra={...}` attributes from the call site as first-class fields. Exceptions
are rendered into an `error` field. The docker compose stack enables JSON
logging to `/tmp/canopy-logs/` by default.

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
- Returns `204 No Content`. Malformed bodies get a standard `422`.
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

## Dashboards

Grafana is auto-provisioned with a Loki datasource and two dashboards
(*Canopy Frontend Analytics*, *Canopy Error Reporting*) from
`docker/observability/grafana/`. Edit the JSON there (or export from the
Grafana UI and copy back) to evolve them under review.
