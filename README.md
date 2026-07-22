# Canopy

Canopy is a planned platform for control system displays, tools and applications.

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
CANOPY_API_KEYS="dev:read+write" uv run uvicorn canopy.main:app
```

Then exercise it:

- **REST (one-shot read).** Any PV name works in simulation (each is a synthetic
  sine wave):

  ```bash
  curl -H "X-API-Key: dev" http://localhost:8000/hare/v1/pvs/SIM:TEMP
  ```

- **Live stream.** Open `frontend-demos/hare-example.html` in a browser, click **Connect**,
  then **Subscribe**. The page defaults to `ws://localhost:8000/hare/v1/ws/pvs?api_key=dev`.

- **Interactive docs.** The mounted Hare app serves Swagger UI at
  <http://localhost:8000/hare/docs>.

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
CANOPY_API_KEYS="dev:read+write" CANOPY_EPICS__BACKEND=epics \
  EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
  uv run uvicorn canopy.main:app
```

Examples are provided under the `frontend-demos` directory for connecting to PVs.

## Building Docs

```bash
uv run zensical serve
```
