# Canopy

Canopy is the backend for SLAC's control-system display platform. It is a single
FastAPI application made of independent modules that serve live and historical
process-variable (PV) data, formula results, machine-state snapshots and
background jobs over REST and WebSocket APIs. It talks to EPICS Channel Access
and pvAccess, and includes a simulation backend, so you can run it without an
IOC.

Canopy is pre-1.0 and under active development; APIs may change between
releases.

- [Documentation](https://slaclab.github.io/canopy/), including the
  [getting started guide](https://slaclab.github.io/canopy/getting_started/) and
  the [modules overview](https://slaclab.github.io/canopy/modules/)
- [Deploy at your facility](#deployment)
- [Contributing](CONTRIBUTING.md) and [security policy](SECURITY.md)
- [License](LICENSE.md)

## Setup

Canopy uses [`uv`](https://docs.astral.sh/uv/) for dependency management.

## Running Tests

```bash
uv run pytest tests/ -v
```

## Running Hare locally

**Hare** is the live-PV WebSocket broker.

### Simulation backend (no IOC)

Start the API with an API key registered:

```bash
CANOPY_API_KEYS="dev:read+write@*" uv run uvicorn canopy.main:app
```

Then exercise it:

- **REST (one-shot read).** Any PV name works in simulation (each is a synthetic
  sine wave):

  ```bash
  curl -H "X-API-Key: dev" http://localhost:8000/hare/v1/pvs/SIM:TEMP
  ```

- **Live stream.** Open `frontend-demos/hare-example.html` in a browser, click **Connect**,
  then **Subscribe**. The page defaults to `ws://localhost:8000/hare/v1/ws/pvs?api_key=dev`.

- **Interactive docs.** <http://localhost:8000/docs> provides one Swagger UI
  with a service dropdown for Canopy and each mounted module.

### EPICS Interaction

To interact with EPICS PVs, either supply you own IOC(s) and set the location
in the EPICS_CA_ADDR_LIST and EPICS_PVA_ADDR_LIST variables as needed, or use the bundled
caproto softioc:

```bash
# Terminal 1 — soft IOC, PVs under TEST:
EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
  uv run --with caproto python scripts/sim_ioc.py
```

Replace the value of EPICS_CA_ADDR_LIST in this command as needed:

```bash
CANOPY_API_KEYS="dev:read+write@*" CANOPY_EPICS__BACKEND=epics \
  EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
  uv run uvicorn canopy.main:app
```

Examples are provided under the `frontend-demos` directory for connecting to PVs.

## Deployment

Canopy ships as the `ghcr.io/slaclab/canopy` image. To run it at your
facility, see [Deploy at your facility](https://slaclab.github.io/canopy/deployment/production/)
([source](docs/deployment/production.md)) and the reference
[`docker/docker-compose.prod.yml`](docker/docker-compose.prod.yml).

## Building Docs

```bash
uv run zensical serve
```
