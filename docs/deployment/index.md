# Deployment overview

A Canopy deployment is one container image running as two process types,
backed by Postgres and Redis:

| Process | Command | Role |
|---------|---------|------|
| `api` | `uvicorn canopy.main:app` | The HTTP/WebSocket API |
| `worker` | `arq canopy.worker.WorkerSettings` | Background jobs (snapshot create/restore, batch conversion) |

Both run from the same image (`ghcr.io/slaclab/canopy`) and share the same
configuration. Postgres holds the persistent modules' data (one schema per
module); Redis carries the job queue.

Two deployment paths are documented here:

- [Docker Compose](docker.md) — the development stack, and the app-only
  production compose file.
- [Facility deployment](facilities.md) — the GitHub Actions pipeline that
  builds images and deploys the SLAC facility instances.

## Configuration

Canopy is configured entirely through environment variables (optionally via a
`.env` file in development). There are no config files to mount. The
essentials:

| Variable | Purpose |
|----------|---------|
| `CANOPY_API_KEYS` | API-key registry — see below. Unset means every gated endpoint returns 401 |
| `CANOPY_EPICS__BACKEND` | `simulation` (default) or `epics`. **Production must set `epics`** |
| `EPICS_CA_ADDR_LIST`, `EPICS_PVA_ADDR_LIST`, ... | Standard EPICS network discovery variables (only with the `epics` backend) |
| `CANOPY_DATABASE_URL` | Postgres, asyncpg driver: `postgresql+asyncpg://user:pass@host:port/db` |
| `CANOPY_REDIS_URL` | Redis: `redis://[:password@]host:port/0` |
| `CANOPY_DEBUG` | Verbose errors in responses; keep `false` in production |
| `CANOPY_LOG_LEVEL` | Root log level for the API and worker (`DEBUG`, `INFO`, `WARNING`, ...). Default `INFO`; an unknown name falls back to `INFO` |
| `CANOPY_SQUIRREL__RESTORE_HOOKS` | Comma-separated restore hooks, applied in order; empty means none run. Once a hook's write fails for a PV, later hooks skip it, so list `write_to_con` before `magnet_trim` to trim a magnet only after its BCON is updated. Must reach the **worker**, which performs restores. Verify with `GET /squirrel/v1/health/restore-hooks`: `worker` is what the worker reported loading at startup, `api` is the API's view, and `matchesApi: false` flags a mismatch |
| `CANOPY_<MODULE>__<FIELD>` | Per-module tuning (e.g. `CANOPY_VOLE__GLOBAL_BYTES_MAX`) |

The annotated template
[`.env.example`](https://github.com/slaclab/Canopy/blob/main/.env.example)
at the repo root documents every setting with its default.

!!! warning "Simulation is the default backend"
    A deployment that forgets `CANOPY_EPICS__BACKEND=epics` starts cleanly,
    passes health checks, and serves synthetic sine waves for every PV. The
    WebSocket `heartbeat` frame reports the active backend — check it when
    values look too smooth to be real.

### Provisioning API keys

```
CANOPY_API_KEYS="<token>:<scope>[+<scope>][,<token>:<scope>...]"
```

- Issue **separate tokens per client class**, with the minimum scope:
  read-only displays get a `read` token; anything that writes PVs or saves
  snapshots gets `read+write`.
- Tokens are opaque strings — generate long random values for production.
- Rotation: add the new token alongside the old (comma-separated), roll
  clients over, remove the old token, restart. Keys are read at process
  startup.
- In the facility pipeline, keys live in the per-instance `APP_SECRETS`
  GitHub secret — see [facility deployment](facilities.md#prerequisite-app_secrets_instance-github-secrets).

## Database migrations

The schema is managed exclusively by Alembic — the app never creates tables
at runtime:

```bash
CANOPY_DATABASE_URL=postgresql+asyncpg://... uv run alembic upgrade head
```

The production image carries the migration tree, and the facility deploy
workflow runs `alembic upgrade head` (configurable) before restarting
services. A fresh database serves nothing until its first migration.

## Health checks

| Endpoint | Use |
|----------|-----|
| `GET /health` | Process liveness — used by the container healthchecks and the deploy pipeline |
| `GET /<module>/v1/health/watchdog` | Per-module liveness heartbeat (version, uptime) |
| `GET /<module>/v1/health/summary` | Per-module status rollup (requires read access) — e.g. Hare's reports backend, client and monitor counts |
