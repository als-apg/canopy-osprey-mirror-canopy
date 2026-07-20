# Observability setup guide

Step-by-step bring-up of the logging → Loki → Grafana pipeline on a
development machine. Requires Docker and this repository.

## 1. Start the stack with the observability overlay

From the repo root:

```bash
docker compose -f docker/docker-compose.yml \
               -f docker/observability/docker-compose.observability.yml up -d
```

This starts the usual stack (`canopy-api`, `canopy-worker`, `canopy-postgres`,
`canopy-redis`) plus `canopy-loki`, `canopy-promtail`, and `canopy-grafana`.

The base compose already sets `CANOPY_LOG_FORMAT=json` and
`CANOPY_LOG_PATH=/tmp/canopy-logs/backend.log` (worker: `worker.log`), and
bind-mounts `/tmp/canopy-logs` so Promtail can tail the files from the host.

!!! note
    Database migrations still run from the host, not in-container:
    `CANOPY_DATABASE_URL=postgresql+asyncpg://canopy:canopy@localhost:5432/canopy uv run alembic upgrade head`

## 2. Generate an event

```bash
curl -s -X POST http://localhost:8000/analytics/v1/events \
  -H 'Content-Type: application/json' \
  -d '{"event":"screen_opened","sessionId":"s1","route":"/demo","properties":{"screen":"demo"}}' \
  -w '%{http_code}\n'
```

Expect `204`. Add `-H 'X-API-Key: dev'` to see the keyed variant.

## 3. Verify each pipeline stage

**Log file** — a JSON line should appear:

```bash
tail -n1 /tmp/canopy-logs/backend.log
```

**Loki** — query it directly:

```bash
curl -sG http://localhost:3100/loki/api/v1/query_range \
  --data-urlencode 'query={app="canopy", type="analytics"}' | head -c 500
```

**Grafana** — open <http://localhost:3000> (admin/admin unless overridden via
`GRAFANA_ADMIN_USER`/`GRAFANA_ADMIN_PASSWORD`). The Loki datasource and the
*Canopy Frontend Analytics* / *Canopy Error Reporting* dashboards are
provisioned automatically under the **Canopy** folder.

## 4. Simulate a frontend error

```bash
curl -s -X POST http://localhost:8000/analytics/v1/events \
  -H 'Content-Type: application/json' \
  -d '{"event":"frontend_error","route":"/demo","properties":{"error":"TypeError: x is undefined","stack":"..."}}' \
  -o /dev/null -w '%{http_code}\n'
```

It appears on the *Canopy Error Reporting* dashboard (panels select
`event=~"frontend_error|error"`).

## Common issues

- **No data in Grafana**: check each stage in order — file, Loki, dashboard
  time range. `docker logs canopy-promtail` shows scrape/parse problems.
- **Old plain-text lines in the log file**: lines written before JSON mode was
  enabled don't parse; either delete `/tmp/canopy-logs/*` or ignore them.
- **Permission errors on `/tmp/canopy-logs`**: the directory is created by the
  api container; if it exists with root-only ownership from an earlier run,
  `sudo rm -rf /tmp/canopy-logs` and restart the stack.

## Reset

```bash
docker compose -f docker/docker-compose.yml \
               -f docker/observability/docker-compose.observability.yml down
docker volume rm canopy_canopy-loki-data canopy_canopy-grafana-data
rm -rf /tmp/canopy-logs
```
