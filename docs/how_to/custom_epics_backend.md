# Custom EPICS backend

Hare reads and writes live PVs through a connector backend. Canopy ships two, `simulation` and `epics`; labs (or test environments) that reach their control system another way implement the `PVConnector` Protocol and load it via `CANOPY_EPICS__BACKEND`; no fork of Canopy required.

## 1. Implement the Protocol

The contract lives in `canopy.epics.base.PVConnector`. Implement these seven methods:

- `from_config(cfg) -> "YourConnector"`: classmethod that builds an instance from the `EpicsConfig`.
- `start()`: acquire resources and capture the running event loop. Called once at app startup.
- `close()`: drain clients and cancel monitors. Called at app shutdown.
- `get(pv, *, timeout=None) -> PVValue`: one-shot read of the current value.
- `put(pv, value, *, timeout=None, wait=True)`: write `value`; with `wait=True`, return only once the write has completed.
- `subscribe(pv, on_event) -> Subscription`: start a monitor and return a handle whose async `close()` stops it.
- `describe(pv) -> PVMeta`: static per-channel metadata (value kind, units, precision, enum choices, limits).

Plus one attribute:

- `backend_name: str`: class attribute identifying the backend. The WebSocket `heartbeat` frame reports it as `backend`.

The value types come from `canopy.contracts.pv` (`PVValue`, `PVMeta`, `PVWriteValue`, `ValueKind`) and `canopy.contracts.sample` (`AlarmSeverity`).

### Behaviour Hare relies on

- `on_event` must not block, and runs on the event loop thread. A client library that delivers updates on its own threads marshals them onto the loop first (`loop.call_soon_threadsafe`).
- A monitor fires `on_event` when its channel connects and on every update, and with a `PVValue(connected=False, value=None)` when an established channel drops.
- A monitor on a channel that never connects may stay silent. Hare reports such a PV `connected: false` itself after `CANOPY_HARE__SEED_TIMEOUT_S`.
- `get` on an unreachable channel returns a `PVValue` with `connected=False` rather than raising, and `describe` returns a bare `PVMeta(pv=pv)`, as the `epics` backend does.

### PV names

Names may carry a `ca://` or `pva://` prefix, matched case-insensitively. Use `canopy.epics.pv_protocol` (`validate_pv_name`, `parse_pv_name`, `strip_protocol_prefix`) rather than parsing them yourself, so your backend rejects and routes names the same way the built-in ones do.

### Severity normalization

If your client library returns severity as an EPICS integer, use `canopy.epics.base.normalize_severity`. If it returns strings, map directly to `canopy.contracts.sample.AlarmSeverity`.

### Errors

Raise the typed domain exceptions from `canopy.epics.base`:

| Exception                 | HTTP status | When                                                                     |
| ------------------------- | ----------- | ------------------------------------------------------------------------ |
| `PVWriteFailed`           | 502         | A write was rejected or did not complete within its timeout.              |
| `EpicsBackendUnavailable` | 503         | `from_config` cannot build the backend (e.g. a missing client library).   |

Canopy's error handler maps each to its HTTP status with the right slug in the `{"code", "detail"}` envelope.

### Configuration

`from_config` receives the whole `EpicsConfig`. It carries only the fields it declares; other `CANOPY_EPICS__*` variables are ignored. Read backend-specific settings from a settings class of your own, under its own environment prefix.

## 2. Load via dotted path

Install your package alongside Canopy and name the class in the `module:Class` form:

```bash
CANOPY_EPICS__BACKEND=labxyz.canopy_plugin:LabXYZConnector
```

Hare imports the module and builds the connector via `from_config(cfg)` at startup. A module that does not import, a missing class, or a class without a `from_config` classmethod stops startup with an `EpicsBackendUnavailable` that names the path.

## 3. Working example

`canopy.epics.backends.simulation.SimulationConnector` implements the full Protocol with no external dependencies. Loading it by path, `CANOPY_EPICS__BACKEND=canopy.epics.backends.simulation:SimulationConnector`, is a quick check that the dotted-path form works in a deployment.
