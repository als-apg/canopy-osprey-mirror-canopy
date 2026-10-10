# Modules

Canopy is a modular monolith: one FastAPI application mounts independent
sub-applications, each owning a single concern. Modules talk to each other
through in-process gateways, so the whole platform runs as one API process
(plus a background worker).

## Mounted modules

| Mount | Module | What it does |
|-------|--------|--------------|
| `/hare` | [Hare](../hare/index.md) | Live-PV broker: WebSocket streaming, REST reads and writes |
| `/mole` | Mole | Archiver Appliance proxy for historical PV data |
| `/raccoon` | Raccoon | PV name-lookup proxy over the lab directory service |
| `/vole` | [Vole](../vole/index.md) | In-memory per-PV buffers merging Hare live data with Mole history |
| `/fox` | [Fox](../fox/index.md) | Formula engine — expressions over live and historical PV data |
| `/squirrel` | Squirrel | Machine-state snapshots: save, restore, and compare (Postgres-backed) |
| `/jobs` | Jobs | Background-job status over the Arq/Redis queue |
| `/converter` | Converter | Legacy screen converter — EDM `.edl` / Qt `.ui` to Canopy Screen IR |

Modules without a guide here yet are documented by their interactive API docs
(below) and their design pages on the
[GitHub wiki](https://github.com/slaclab/Canopy/wiki).

The top-level Swagger UI at `/docs` provides a dropdown to switch between
Canopy's core API and each mounted module. Each module's standalone Swagger UI
and OpenAPI schema remain available at `/<module>/docs` and
`/<module>/openapi.json`.

## Conventions every module follows

**URLs.** APIs are versioned per module: `/<module>/v1/...`. Each module also
serves its own Swagger UI at `/<module>/docs` — with the API running, that is
the authoritative, always-current endpoint reference.

**Authentication.** Send an API key in the `X-API-Key` header. Read endpoints
need a key with the `read` scope, mutating endpoints need `write`. WebSocket
endpoints also accept `?api_key=<token>` in the URL for clients that cannot
set handshake headers. See [Getting started](../getting_started.md#api-keys)
for key registration.

**Errors.** Every error response uses one envelope:

```json
{
  "code": "pv_read_timeout",
  "detail": "TEST:SINE: no response within 5.0 s"
}
```

`code` is a stable machine-readable slug; match on it rather than on `detail`
text or HTTP status alone.

**JSON casing.** Wire payloads use `camelCase` field names (`tsNs`,
`valueKind`, `pvNames`).

**Health.** Each module exposes two endpoints under `/<module>/v1/health`:

- `GET /<module>/v1/health/watchdog` — liveness heartbeat (module, version,
  uptime). No auth, touches nothing external.
- `GET /<module>/v1/health/summary` — module status rollup (backend state,
  counters). Requires read access.

The root `GET /health` is a plain process-liveness check used by container
health probes.

## Internal packages (not mounted)

These live in the same codebase but expose no HTTP API of their own:

- **core** — configuration, API-key auth, database sessions, the error
  envelope, time parsing.
- **epics** — the CA/PVA connector Hare speaks through. Ships two backends:
  `simulation` (default; synthetic data, no network) and `epics` (real
  Channel Access / PV Access). Selected with `CANOPY_EPICS__BACKEND`.
- **contracts** — shared DTOs used across modules (PV values, samples).
- **worker** — the Arq entrypoint (`canopy.worker.WorkerSettings`) that runs
  background jobs for Squirrel and Converter.

## Planned modules

- **Owl** — plot-configuration persistence + legacy importer.
- **Wren** — rules engine, hosted inside Hare.

Design discussions for these live on the
[GitHub wiki](https://github.com/slaclab/Canopy/wiki).
