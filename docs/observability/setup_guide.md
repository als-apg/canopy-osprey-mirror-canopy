# Observability setup guide

Step-by-step bring-up of the logging → Loki → Grafana pipeline, plus the
`/metrics` → Prometheus leg, on a development machine. Requires Docker and this repository.

## 1. Start the stack with the observability overlay

From the repo root, with `docker/.env` in place (copy `docker/.env.example`;
it sets the Redis password the base stack requires):

```bash
docker compose -f docker/docker-compose.yml \
               -f docker/observability/docker-compose.observability.yml up -d --build
```

`--build` rebuilds the api/worker images when `pyproject.toml` or `uv.lock`
changed since your last build — the dev image installs dependencies at build
time, so a stale image fails at import.

This starts the usual stack (`canopy-api`, `canopy-worker`, `canopy-postgres`,
`canopy-redis`) plus `canopy-loki`, `canopy-promtail`, `canopy-prometheus`,
and `canopy-grafana`.

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

The examples use the compose default of port 8000. If you started the stack
with `API_HOST_PORT` set, use that port instead.

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

**Metrics** — the parent app's exposition should already count the event you
posted, under the analytics module's route template:

```bash
curl -s http://localhost:8000/metrics | grep 'canopy_http_requests_total{.*module="analytics"'
```

**Prometheus** — the `canopy-api` target should be `up` at
<http://localhost:9090/targets>. The [metrics page](metrics.md) lists what is
exported.

**Grafana** — open <http://localhost:3000> (admin/admin unless overridden via
`GRAFANA_ADMIN_USER`/`GRAFANA_ADMIN_PASSWORD`). The Loki and Prometheus datasources and the
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

- **`failed to bind host port 0.0.0.0:8000`** when `canopy-api` starts: another
  process owns port 8000 — typically `uv run zensical serve` previewing these
  docs, which answers with a redirect if you curl it by mistake. Stop it, or
  start the stack with `API_HOST_PORT=8080` and use that port in every
  command below. Then re-run the `up -d` command so compose *recreates*
  `canopy-api`: the failed attempt leaves a half-created container with no
  network, and `docker start` on it comes up without DNS or a published port.
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
docker volume rm canopy_canopy-loki-data canopy_canopy-prometheus-data canopy_canopy-grafana-data
rm -rf /tmp/canopy-logs
```
