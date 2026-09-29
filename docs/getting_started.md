# Getting started

Canopy is the backend for SLAC's control-system display platform. It is a single
FastAPI application composed of independent modules — each mounted at
`/<module>` and serving its API under `/<module>/v1/...`. This guide takes you
from a fresh clone to reading live PV data, first against the built-in
simulation backend (no EPICS required), then against real EPICS IOCs.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — installs
  Python and all dependencies; nothing else is required to run the core PV
  modules.
- **Docker** (optional) — for the full stack with Postgres and Redis, which the
  persistent modules (Squirrel snapshots, background jobs, batch conversion)
  need. See [Docker Compose](deployment/docker.md).

## Install

```bash
git clone https://github.com/slaclab/Canopy
cd Canopy
./scripts/setup_dev.sh
```

This syncs the dev dependencies and installs the pre-commit hooks. See
[CONTRIBUTING.md](https://github.com/slaclab/Canopy/blob/main/CONTRIBUTING.md)
for the development workflow.

## Configure

Canopy reads configuration from environment variables, and from a `.env` file
in the working directory (real environment variables override the file). Every
setting ships with a working default except the API keys. To start from the
annotated template:

```bash
cp .env.example .env
```

then uncomment what you need. Settings are namespaced per module:
`CANOPY_<MODULE>__<FIELD>` (double underscore), e.g.
`CANOPY_VOLE__MAX_POINTS=500000`.

### API keys

Every data endpoint requires an API key with the right scope. Keys are
registered through a single variable:

```bash
CANOPY_API_KEYS="<token>:<scope>[+<scope>][,<token>:<scope>...]"
```

- Scopes are `read` and `write` — e.g. `dev:read+write` or
  `viewer:read,operator:read+write`.
- Clients send the token in the `X-API-Key` header. WebSocket endpoints also
  accept `?api_key=<token>` for clients that cannot set headers.
- If `CANOPY_API_KEYS` is unset, no keys exist and every gated endpoint
  returns `401`.

## Run the API

```bash
CANOPY_API_KEYS="dev:read+write" uv run uvicorn canopy.main:app
```

The default EPICS backend is `simulation`: **any** PV name resolves to a
synthetic sine wave, so you can exercise the whole API with no IOC, network
access, or database.

Once running, you have:

| URL | What it is |
|-----|------------|
| <http://localhost:8000/> | Service info (name, version) |
| <http://localhost:8000/health> | Liveness check |
| <http://localhost:8000/docs> | API index listing route patterns, Swagger UI, and OpenAPI schemas |
| <http://localhost:8000/hare/docs> | Interactive Swagger UI for Hare (every module serves its own at `/<module>/docs`) |
| `ws://localhost:8000/hare/v1/ws/pvs` | Live PV stream |

## First requests

Read a PV (any name works in simulation):

```bash
curl -H "X-API-Key: dev" http://localhost:8000/hare/v1/pvs/DEMO:TEMP
```

```json
{
  "pv": "DEMO:TEMP",
  "value": 4.79,
  "valueKind": "scalar",
  "severity": "NO_ALARM",
  "tsNs": 1700000000123456789,
  "connected": true,
  "error": null
}
```

Fetch its display metadata (units, precision, limits — read once to configure
a display):

```bash
curl -H "X-API-Key: dev" http://localhost:8000/hare/v1/pvs/DEMO:TEMP/describe
```

Errors always use the same envelope, with a machine-readable `code` slug:

```json
{
  "code": "invalid_api_key",
  "detail": "Unknown API key"
}
```

A missing or unknown key returns `401 invalid_api_key`; a valid key without
the needed scope returns `403 insufficient_read_access` /
`insufficient_write_access`.

## Live data in the browser

The `frontend-demos/` directory contains self-contained HTML pages that
exercise the WebSocket APIs — open them directly in a browser:

- `hare-example.html` — subscribe to PVs and watch raw update frames.
- `hare-plot-example.html` — live strip chart.
- `fox-ws-example.html` / `fox-rest-example.html` — formula evaluation.

Each page defaults to `ws://localhost:8000/...?api_key=dev`. See the
[Hare WebSocket guide](hare/websocket.md) for the protocol itself.

## Talking to real EPICS

Two things change: start (or point at) an IOC, and switch the backend from
`simulation` to `epics`.

Using the bundled soft IOC (Channel Access, PVs under `TEST:`):

```bash
# Terminal 1 — CA soft IOC
EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
  uv run --with caproto python scripts/sim_ioc.py
```

```bash
# Terminal 2 — Canopy on the real EPICS backend
CANOPY_API_KEYS="dev:read+write" CANOPY_EPICS__BACKEND=epics \
  EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
  uv run uvicorn canopy.main:app
```

```bash
curl -H "X-API-Key: dev" http://localhost:8000/hare/v1/pvs/TEST:SINE
```

For PV Access there is a second soft IOC, `scripts/sim_ioc_pva.py`, hosting
every value kind (`TEST:COUNTER`, `TEST:SINE`, `TEST:WAVEFORM`, `TEST:ENUM`,
`TEST:TABLE`, ...). Address a PV over PVA with the `pva://` prefix:

```bash
curl -H "X-API-Key: dev" "http://localhost:8000/hare/v1/pvs/pva://TEST:COUNTER"
```

Unprefixed names use Channel Access. To use your own IOCs, set
`EPICS_CA_ADDR_LIST` / `EPICS_PVA_ADDR_LIST` to your facility's values.

!!! warning "Simulation is the default"
    If you forget `CANOPY_EPICS__BACKEND=epics`, Canopy starts fine and serves
    plausible-looking synthetic sine waves for every PV. If every value looks
    like a smooth ±5 oscillation, check the backend setting.

## Full stack with Docker

Postgres-backed features (Squirrel snapshots, job status, batch conversion)
need the complete stack — API, worker, Postgres, and Redis:

```bash
docker compose -f docker/docker-compose.yml up -d
```

See [Docker Compose](deployment/docker.md) for configuration, running
database migrations, and the production variant.

## Where next

- [Modules overview](modules/index.md) — what each module does and where its
  docs live.
- [Hare](hare/index.md) — live PV reads, writes, and the WebSocket stream.
- [Fox](fox/index.md) — formulas over live and historical data.
- [Vole](vole/index.md) — the time-aligned buffer behind Fox.
- [Deployment](deployment/index.md) — running Canopy for real users.
- [GitHub wiki](https://github.com/slaclab/Canopy/wiki) — module design
  documents.
