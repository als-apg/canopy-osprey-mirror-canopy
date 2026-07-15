Mole is the read-only proxy in front of one or more EPICS Archiver Appliance instances. It is the only place in the backend that speaks the archiver's HTTP API, and the only place that knows which archiver instances are configured at this site. Everything upstream of Mole sees a single, normalized read surface keyed by an opaque archiver name.

If `canopy-api` restarts, Mole rebuilds its archiver registry from config. There is no per-process state to recover.

## 1. Position in the call graph

```
                 Workbench (Owl, Squirrel) ──┐
                                              ▼
   Archiver Appliance(s) ──────►   Mole   ───┬──►  Vole  (seeds historical windows)
                                              │
                                              └──►  Fox   (rare; Fox usually reads via Vole)
```

- **Writers**: none — Mole is read-only with respect to its upstream archivers.
- **Readers (in-process)**: Vole, when Fox asks for a window the buffer doesn't have. Occasionally Fox directly for one-shot evaluations.
- **Readers (HTTP)**: the frontend Owl tool (live + historical overlays), Squirrel (delta against historical), one-off scripts and notebooks.
- **External callees**: N Archiver Appliance instances, each over HTTP.

Mole has no WebSocket surface, no background pipeline, no event-bus subscriptions. It is a stateless request/response proxy with a typed adapter boundary.

## 2. Boundaries — what Mole owns vs. delegates

| Concern | Mole owns | Mole delegates to |
|---|---|---|
| Archiver instance registry (name → adapter) | ✅ | — |
| Adapter dispatch (default vs. per-call override) | ✅ | — |
| Normalization to Canopy's `Sample` / `BinnedPoint` / `PVMetadata` types | ✅ | — |
| `maxPoints` decimation fallback when adapter ignores hint | ✅ | — |
| Error normalization (`ArchiverUnavailable`, `PVNotFoundInArchiver`, …) | ✅ | — |
| Long-running CSV / MATLAB exports *(planned, not built in v1)* | ✅ (enqueue) | Jobs (Arq) |
| Archiver protocol details (URLs, query params, response shape) | — | adapter classes |
| HTTP transport, retries, timeouts | — | `httpx.AsyncClient` per adapter |
| Time-aligned multi-PV reads | — | Vole |
| Live PV metadata (description, alarm/display limits, enum strings) | — | Hare / EPICS Connector (via Channel Access) |
| Caching of archiver responses | — | (deferred; will live in Redis when added) |

Mole's job is dispatch + translation. The intelligence is on either side: real protocol logic in adapters, real consumption logic in callers.

**Note on metadata scope.** The EPICS Archiver Appliance only carries `EGU` and `PREC` in its responses. Full PV metadata (description, `LOPR`/`HOPR`, `HIHI`/`LOLO`/`HIGH`/`LOW`, enum strings) lives in IOC records and is fetched via Channel Access — that is Hare / EPICS Connector's responsibility, not Mole's. Mole's `PVMetadata` type defines the full schema for forward-compatibility with adapters that do carry richer metadata, but the Appliance adapter populates only the archiver-known fields and leaves the rest `null`.

## 3. Internal structure

Standard three-tier shape; the data tier is replaced by an adapter tier because Mole has no Canopy-owned persistence:

```
src/canopy/mole/
  app.py                       # sub-FastAPI — registers the v1 router and the canopy error handler
  runtime.py                   # process-start monotonic time; uptime_s() for /health/watchdog
  api/v1/
    router.py                  # APIRouter(prefix="/v1") that includes the per-resource routers
    archivers.py               # GET  /archivers
    data.py                    # GET  /data/{pv}
    metadata.py                # GET  /metadata/{pv}
    search.py                  # GET  /search
    health.py                  # GET  /health/{summary,watchdog}
    _time_parse.py             # parse_time / parse_bin_size_to_ns helpers
  schemas/
    series.py                  # ArchiverSeriesDTO, ArchiverBinnedSeriesDTO, SampleDTO, BinnedPointDTO
    pv.py                      # PVMetadata + PVMetadataDTO alias
    archiver.py                # ArchiverInfoDTO, ArchiverListDTO, ArchiverHealthDTO, HealthSummaryDTO
    health.py                  # HeartbeatDTO
    search.py                  # SearchResultsDTO
  services/
    archiver_service.py        # ArchiverService — selects adapter, normalizes, decimates
    decimation.py              # uniform-time fallback decimator
  adapters/
    base.py                    # ArchiverAdapter protocol + AdapterHealth + error types + severity normalizer + type registry
    registry.py                # AdapterRegistry; build_registry(MoleConfig); module:Class dotted-path resolution
    appliance.py               # EpicsArchiverApplianceAdapter — the only built-in type in v1
  config.py                    # MoleConfig + ArchiverInstanceConfig (pydantic-settings)
  gateway.py                   # public surface for other modules; bind() / get_service() singleton
  dependencies.py              # ArchiverServiceDep

# Planned / not yet built:
#   api/v1/exports.py          # POST /exports (enqueues a Jobs task)
#   schemas/export.py          # NewExportDTO, ExportResultDTO
#   tasks/export_csv.py        # Arq task body for CSV exports
#   tasks/export_matlab.py     # Arq task body for MATLAB exports
#   events.py                  # publishes ExportCompleted; no subscribers in v1
```

`adapters/` plays the role `data/` plays in a DB-backed module. It is the adapter tier; nothing above `services/` imports from it.

## 4. Data model

Mole defines no ORM models in v1. The types it operates on:

```python
# canopy.contracts.sample (shared with Vole)
class Sample(BaseModel):
    ts_ns: int
    value: float | int | bool | str | bytes | None
    severity: AlarmSeverity                        # "NO_ALARM" | "MINOR" | "MAJOR" | "INVALID"
    source: Literal["live", "archive"]             # always "archive" from Mole

# canopy.contracts.sample
class BinnedPoint(BaseModel):
    """A statistical bucket from a server-side binning operator (e.g. Appliance `optimized_<N>`).

    Distinct from `Sample` — there is no single "value", instead the bucket
    summarizes the underlying samples in `[ts_ns, ts_ns + bin_ns)`.
    """
    ts_ns: int                                     # bucket start, UTC ns
    bin_ns: int                                    # bucket width, ns
    mean: float
    std_dev: float
    min: float
    max: float
    count: int                                     # number of underlying samples
    severity: AlarmSeverity                        # worst severity in the bucket

# canopy.mole.schemas.pv
class PVMetadata(BaseModel):
    """Per-PV metadata as known to one archiver.

    Only `pv`, `archiver`, `units`, and `precision` are populated by the
    Appliance adapter — the rest come from Channel Access (Hare / EPICS
    Connector) and are left `null` here. Defined as a superset so other
    adapter types (or future Appliance versions) can fill them in.
    """
    pv: str
    archiver: str
    description: str | None
    units: str | None                              # EGU
    precision: int | None                          # PREC
    display_low: float | None                      # LOPR
    display_high: float | None                     # HOPR
    alarm_low: float | None                        # LOLO
    alarm_high: float | None                       # HIHI
    warning_low: float | None                      # LOW
    warning_high: float | None                     # HIGH

# canopy.mole.adapters.base
class AdapterHealth(CanopyModel):                  # camelCase on the wire via CanopyModel
    healthy: bool
    latency_ms: float | None = None
    last_error: str | None = None
    last_check_ts_ns: int

# canopy.mole.config
class ArchiverInstanceConfig(BaseModel):
    type: str                                      # registered short name OR "module:Class"
    url: str
    api_key_env: str | None = None
    timeout_s: float = 30.0                        # per-archiver HTTP timeout; see §8
    extras: dict = {}                              # adapter-specific knobs

class MoleConfig(BaseSettings):
    """Sourced from env vars with prefix `CANOPY_MOLE__` and nested delimiter `__`.
    Defaults are empty so the process boots even with no archivers configured;
    requests hit `ArchiverNotConfigured` until config is supplied.
    """
    model_config = SettingsConfigDict(
        env_prefix="CANOPY_MOLE__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )
    default_archiver: str = ""
    archivers: dict[str, ArchiverInstanceConfig] = {}   # key = instance name
```

Errors are typed domain exceptions inheriting from `CanopyError` (shared kernel). Each carries a status code and a stable slug for the error envelope:

```python
# canopy.core.errors  (shared kernel — every module's domain exceptions inherit this)
class CanopyError(Exception):
    """Base for all Canopy domain exceptions.

    Services raise these; the per-module exception handler in `app.py` maps
    them to a uniform JSON response: {"code": "<slug>", "detail": "<msg>"}.
    Services never import FastAPI / never raise HTTPException directly —
    that is what makes them extractable later without rewriting error sites.
    """
    status_code: int = 500
    error_code: "ErrorCode" = ErrorCode.UNHANDLED_EXCEPTION

    def __init__(self, detail: str = "") -> None:
        self.detail = detail or self.__class__.__name__
        super().__init__(self.detail)


# canopy.mole.adapters.base
class ArchiverError(CanopyError): ...
class ArchiverNotConfigured(ArchiverError):
    status_code = 404
    error_code = ErrorCode.ARCHIVER_NOT_CONFIGURED
class ArchiverUnavailable(ArchiverError):
    status_code = 503
    error_code = ErrorCode.ARCHIVER_UNAVAILABLE
class ArchiverTimeout(ArchiverUnavailable):
    status_code = 504
    error_code = ErrorCode.ARCHIVER_TIMEOUT
class ArchiverBadResponse(ArchiverError):
    status_code = 502
    error_code = ErrorCode.ARCHIVER_BAD_RESPONSE
class PVNotFoundInArchiver(ArchiverError):
    status_code = 404
    error_code = ErrorCode.PV_NOT_FOUND_IN_ARCHIVER
class BinningNotSupported(ArchiverError):
    status_code = 501
    error_code = ErrorCode.BINNING_NOT_SUPPORTED
```

The `ErrorCode` enum lives in `canopy.core.errors` and is appended to per module. Slugs are auto-derived as lowercase snake_case from the member name via `auto()`:

```python
# canopy.core.errors
class ErrorCode(StrEnum):
    UNHANDLED_EXCEPTION = auto()
    NO_CODE_GIVEN = auto()
    BAD_REQUEST = auto()
    NOT_FOUND = auto()

    INVALID_API_KEY = auto()
    INSUFFICIENT_READ_ACCESS = auto()
    INSUFFICIENT_WRITE_ACCESS = auto()

    # Mole
    ARCHIVER_NOT_CONFIGURED = auto()
    ARCHIVER_UNAVAILABLE = auto()
    ARCHIVER_TIMEOUT = auto()
    ARCHIVER_BAD_RESPONSE = auto()
    PV_NOT_FOUND_IN_ARCHIVER = auto()
    BINNING_NOT_SUPPORTED = auto()
    # …other modules add their own
```

Cross-module utility errors live in `canopy.core.errors`: `BadRequest` (400 / `bad_request`) and `NotFound` (404 / `not_found`). Mole routes raise these directly when input parsing fails (e.g. malformed `from`/`to`).

Per-module exception handlers are registered via the shared helper:

```python
# canopy.core.errors
def register_canopy_error_handler(app: FastAPI) -> None:
    """Each module's `app.py` calls this on its own sub-app — FastAPI exception
    handlers do not propagate across `app.mount()` boundaries."""

    @app.exception_handler(CanopyError)
    async def canopy_error_handler(request, exc: CanopyError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.error_code.value, "detail": exc.detail},
        )

    @app.exception_handler(Exception)
    async def fallback_exception_handler(request, exc: Exception) -> JSONResponse:
        # 500 with `unhandled_exception` slug; detail is suppressed unless debug=True.
        ...
```

Routes never raise `HTTPException` directly. Services raise the typed domain exception; the handler shapes the response. The fallback handler ensures unexpected exceptions still produce a `{code, detail}` envelope rather than FastAPI's default shape.

### 4.1 DTO base class

All DTOs in Mole (and Canopy generally) inherit from a shared `CanopyModel` base configured to serialize snake_case Python attributes as camelCase on the wire and accept either form on input — the Pydantic v2 canonical pattern:

```python
# canopy.contracts.base
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

class CanopyModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
    )
```

Mole DTOs (`SampleDTO`, `BinnedPointDTO`, `PVMetadataDTO`, `ArchiverInfoDTO`, etc.) inherit from `CanopyModel`. No per-field `Field(alias=...)` annotations except for fields that collide with Python keywords — `ArchiverSeriesDTO` and `ArchiverBinnedSeriesDTO` use `from_ns: int = Field(alias="from")` / `to_ns: int = Field(alias="to")` because `from` cannot be a Python attribute name. The wire still sees `from` / `to`.

## 5. The dispatch problem

The thing that makes Mole more than a thin httpx wrapper. A request enters with an optional archiver name. The service must:

1. Resolve `archiver_name or config.default_archiver` to a configured instance, or raise `ArchiverNotConfigured`.
2. Look up the adapter instance for that name in the registry.
3. Pick the adapter method matching the requested mode (raw `fetch_window` vs. binned `fetch_binned`).
4. If the adapter raises a transport-layer error, normalize to `ArchiverUnavailable` / `ArchiverTimeout`.
5. For raw reads: if the caller asked for `maxPoints` and the adapter returned more samples, apply the fallback decimator. (Binned reads are already at the requested bucket count.)
6. Return the result tagged with the archiver name (so the caller can display *which* archiver answered).

```python
samples = await archiver_service.fetch_window(
    pv="LCLS:BPM:1:X",
    t0_ns=...,
    t1_ns=...,
    archiver=None,           # → uses default
    max_points=2000,
)
```

No failover, no fan-out, no per-PV routing in v1. If the named archiver is down, the caller sees a 503 — they know exactly which archiver failed and can retry against another by name. Routing rules (`PV:LCLS:* → "slac"`) are deferred to v2; the dispatcher has the seam to add them without changing the gateway signature.

## 6. Two read modes — raw vs. binned

The Archiver Appliance distinguishes two fundamentally different responses:

- **Raw samples.** The archiver returns its actual stored points: `[{secs, nanos, val, severity, status}, …]`. One `Sample` per row. Used for high-resolution views and any caller that needs exact stored values.
- **Binned (statistical) buckets.** When the URL wraps the PV in a binning operator (e.g. `optimized_2000(PV)`), the archiver computes per-bucket statistics on the server and returns one bucket per row, with `val` carrying `[mean, std, min, max, count]`. Used by every plot that's zoomed out far enough to span more samples than pixels — the bucket count is chosen to match the plot width.

These produce different data, and clients need to know which they got. Mole exposes them as **two response shapes**, not a polymorphic one:

| Mode | DTO returned | Per-point type | When |
|---|---|---|---|
| Raw | `ArchiverSeriesDTO` | `SampleDTO` | `binning` query param absent. |
| Binned | `ArchiverBinnedSeriesDTO` | `BinnedPointDTO` | `binning` query param set. |

Binning is **intent-based**, not operator-based. Two modes that any TSDB-shaped adapter can implement:

- **`binning="auto"` + `maxPoints=N`** — "give me ≤ N representative buckets covering the window." Bucket widths may vary across the window. The Appliance adapter maps this to `optimized_<N>(PV)`; an InfluxDB adapter could map to a `SELECT mean(),min(),max() GROUP BY time(auto)`.
- **`binning="time"` + `binSize=<duration>`** — "give me one bucket per `binSize`." Bucket widths are uniform. The Appliance adapter maps this to `mean_<dt>(PV)`; other archivers map to their native group-by-time operator.

The user expresses intent; the adapter picks the native mechanism. No archiver-specific operator names appear in the public contract — that's how the cross-adapter promise stays honest.

`maxPoints` semantics across modes:
- Raw mode (no `binning`): cap on returned-sample count; service decimates if the adapter returned more. Optional — omitting it returns the archiver's full response.
- `binning="auto"`: target bucket count. Required.
- `binning="time"`: ignored. `binSize` determines the count given the window.

Adapter-specific operators (e.g. an Appliance-only `firstSample_<dt>(PV)`) are not exposed on the wire in v1. If a caller needs one, the path forward is either (a) lift it to a new intent verb if it generalizes, or (b) add an explicit `extras` passthrough documented as adapter-specific and non-portable. Deferred until a caller asks.

## 7. The fallback decimator

`maxPoints` is a **hint** to the adapter in raw mode. The Archiver Appliance respects it via the binning operators (which we surface as binned mode); in raw mode the adapter returns whatever the archiver gave. To give callers a stable contract in raw mode, the service post-processes:

```python
if max_points is not None and len(samples) > max_points:
    samples = uniform_time_decimate(samples, max_points)
```

`uniform_time_decimate` in `services/decimation.py` is **uniform-time downsampling** — it picks the sample nearest each of `max_points` evenly-spaced timestamps in `[t0, t1]`. This is for plot density control, *not* statistical aggregation. Callers who want statistical aggregation should request binned mode instead; the service does not synthesize statistics on the client side.

## 8. Adapter lifecycle

**Startup.** Mole exposes `mole_lifespan()` (an `@asynccontextmanager` function in `canopy.mole.app`) which the parent application's lifespan composes — mounted sub-apps do not run their own lifespan automatically behind `app.mount()`, so `canopy.main.lifespan` enters Mole's context manager explicitly. `mole_lifespan` builds the registry once: for each entry in `MoleConfig.archivers`, `build_registry` resolves the `type` (short name → `@register_archiver_type`-decorated class, or `module:Class` → `importlib.import_module`), instantiates the adapter via `AdapterClass.from_config(name, cfg)`, stores it, then constructs the `ArchiverService` and binds it on the gateway via `gateway.bind(service)`. Each adapter owns its own `httpx.AsyncClient` configured with the per-archiver `timeout_s`. Startup fails loud if `default_archiver` is set but not in `archivers`, or any adapter type cannot be resolved.

**Per-request.** Adapters are reused across requests; `httpx.AsyncClient` is concurrency-safe and pools connections. No per-request adapter construction.

**Timeouts.** Each adapter's HTTP client uses the per-archiver `timeout_s` (default 30s). PyDM 1.0 used 7.5s and that was usually fine for live plotting; Mole defaults higher because long export windows are a first-class case here. Operators tune per-archiver as needed.

**Health checks.** Each adapter exposes `health() -> AdapterHealth`. The `/health/summary` endpoint fans out to all configured adapters concurrently with a short timeout (2s, independent of the per-archiver `timeout_s`) and returns per-archiver status. A single archiver being down does not make Mole unhealthy — the response carries the per-archiver state and the overall status is `degraded` rather than `down`. The Appliance adapter probes its instance via `GET /mgmt/bpl/getApplianceInfo`.

**Shutdown.** `mole_lifespan` calls `registry.close_all()`, which calls `adapter.close()` on each instance (logging but not propagating per-adapter errors), then unbinds the gateway via `gateway.bind(None)`.

**Reconfiguration.** Not supported in v1. Changing config requires a process restart.

## 9. Multi-replica behavior

Each `canopy-api` replica builds its own registry from the same config. Adapters are stateless (modulo their httpx connection pool), so two replicas calling the same archiver is fine — the archiver sees independent HTTP clients. No coordination, no sticky routing.

When a Redis-backed response cache is added later, the cache key includes the archiver name; cross-replica hits become possible without any change to the dispatch path.

## 10. Public API — `canopy.mole.gateway`

The gateway is the only symbol other modules import from Mole. Other modules pass archiver names as opaque strings; they never see adapter instances or config. Internally it is a thin facade: a module-level `_service: ArchiverService | None`, wired at startup via `bind(service)` and unwired at shutdown via `bind(None)`. `get_service()` is what `dependencies.py` uses to inject the service into routes; tests substitute by re-binding.

```python
"""
canopy.mole.gateway
====================

Public API for the Mole archiver-proxy module.

Other Canopy modules import only from this file; the rest of `canopy.mole.*`
is private implementation. The signatures here form the contract that survives
adapter churn and the eventual extraction of Mole into its own service.

All timestamps are UTC nanoseconds (`int`). Pass `t1_ns=0` to mean "now"; the
gateway resolves it to a single frozen value at request entry.

`archiver=None` selects the configured default. Passing an unknown archiver
name raises `ArchiverNotConfigured`.
"""

from typing import Literal

from canopy.contracts.sample import Sample, BinnedPoint
from canopy.mole.schemas.pv import PVMetadata
from canopy.mole.schemas.archiver import ArchiverListDTO
from canopy.mole.services.archiver_service import ArchiverService

BinningMode = Literal["auto", "time"]


def bind(service: ArchiverService | None) -> None:
    """Wire (or unwire) the ArchiverService singleton. Called from app lifespan."""

def get_service() -> ArchiverService:
    """Used by `dependencies.py` to inject the service into routes; raises if unbound."""


# --------------------------------------------------------------------------- #
# Reads — raw samples
# --------------------------------------------------------------------------- #

async def fetch_window(
    pv: str,
    t0_ns: int,
    t1_ns: int,
    *,
    archiver: str | None = None,
    max_points: int | None = None,
) -> list[Sample]:
    """Fetch one PV's raw samples from one archiver in [t0_ns, t1_ns].

    Returned samples have `source="archive"`. Time-ordered, ascending `ts_ns`.
    The gateway returns just the sample list; callers that also need the
    resolved archiver name or the `decimated` flag should call the HTTP route
    or `ArchiverService.fetch_window` directly (the service returns a
    `(archiver_name, samples, decimated)` tuple).

    Args:
        pv: Canonical PV name. Case-sensitive.
        t0_ns, t1_ns: Inclusive window bounds in UTC nanoseconds.
            `t1_ns=0` means "now" (resolved at call entry).
        archiver: Instance name from configuration. None → default.
        max_points: Density hint. If set and the adapter returns more samples,
            `services.decimation.uniform_time_decimate` is applied as a
            fallback. None → no decimation, full archiver response.

    Returns:
        Time-ordered list of `Sample` objects. Empty list if the archiver
        has no data for the window.

    Raises:
        ArchiverNotConfigured: `archiver` is not in the registry.
        ArchiverUnavailable: archiver is reachable but errored, or unreachable.
        ArchiverTimeout: subclass of ArchiverUnavailable.
        PVNotFoundInArchiver: PV is not indexed by this archiver.
    """


# --------------------------------------------------------------------------- #
# Reads — binned (statistical aggregation)
# --------------------------------------------------------------------------- #

async def fetch_binned(
    pv: str,
    t0_ns: int,
    t1_ns: int,
    *,
    binning: BinningMode,
    archiver: str | None = None,
    target_buckets: int | None = None,
    bin_size_ns: int | None = None,
) -> list[BinnedPoint]:
    """Fetch one PV's pre-binned statistics from one archiver.

    Used by plots zoomed out far enough that per-bucket mean/std/min/max is
    more useful than raw samples. Bucket boundaries and statistics are
    computed by the archiver; Mole does not synthesize statistics.

    Args:
        pv, t0_ns, t1_ns, archiver: as in fetch_window.
        binning: Binning intent.
            - `"auto"`: adapter chooses bucket widths to land on roughly
              `target_buckets` buckets covering the window.
            - `"time"`: uniform-width buckets of `bin_size_ns` each.
        target_buckets: Required when `binning="auto"`. Approximate output
            bucket count; the adapter may return slightly more or fewer.
        bin_size_ns: Required when `binning="time"`. Ignored otherwise.

    Returns:
        Time-ordered list of `BinnedPoint` objects. Each point summarizes
        the underlying samples in `[ts_ns, ts_ns + bin_ns)`.

    Raises:
        ArchiverNotConfigured, ArchiverUnavailable, ArchiverTimeout,
        PVNotFoundInArchiver: as in fetch_window.
        BinningNotSupported: adapter cannot implement the requested mode
            (501) — rare, since both modes are part of the v1 contract.
        ValueError: missing required argument for the chosen mode.
    """


# --------------------------------------------------------------------------- #
# Reads — metadata + search
# --------------------------------------------------------------------------- #

async def metadata(
    pv: str,
    *,
    archiver: str | None = None,
) -> PVMetadata:
    """Return per-PV metadata as known to the archiver.

    For the Appliance adapter, only `units` and `precision` are populated;
    callers needing alarm/display limits or descriptions should query
    Hare / EPICS Connector instead. See §2 note.

    Raises:
        ArchiverNotConfigured, ArchiverUnavailable, PVNotFoundInArchiver.
    """


async def search(
    pattern: str,
    *,
    archiver: str | None = None,
    limit: int = 100,
) -> list[str]:
    """Search a single archiver's PV index by name pattern.

    Pattern syntax is the archiver's native syntax (Archiver Appliance uses
    regex; Mole does not normalize across adapter types). Returns just PV
    names — callers wanting metadata fetch it per-PV via `metadata()`.

    Args:
        pattern: Archiver-native pattern.
        archiver: Instance name; None → default. Search is one-archiver-at-a-time
            in v1; cross-archiver search is deferred until there is a use case.
        limit: Max names to return.

    Returns:
        List of PV names in archiver-native order.
    """


# --------------------------------------------------------------------------- #
# Introspection
# --------------------------------------------------------------------------- #

async def list_archivers() -> ArchiverListDTO:
    """Return all configured archivers with current health.

    Used by the `/archivers` endpoint and by the frontend to populate the
    archiver-selection dropdown. The DTO carries both the configured
    `default` and an `archivers` list of `ArchiverInfoDTO` entries; a
    concurrent health probe (2s per adapter) populates each entry.
    """
```

## 11. `ArchiverService` — the implementation behind the gateway

The gateway functions are one-line wrappers around `ArchiverService` methods. The service is constructed once per process by `app.lifespan` and held in a module-level slot in `gateway`.

The service exposes the archiver name (and the `decimated` flag for raw reads) as part of its return values, so HTTP routes can echo "which archiver answered" in responses; the gateway facades unpack and drop those for in-process callers that only need the data.

```python
class ArchiverService:
    """Selects adapter, dispatches, normalizes errors, applies decimation fallback.

    Lifecycle: one instance per `canopy-api` process. Constructed in
    `canopy.mole.app.mole_lifespan`; injected into `canopy.mole.gateway` via
    its module-level singleton. Tests substitute via `gateway.bind(service)`.
    """

    def __init__(self, registry: AdapterRegistry, config: MoleConfig) -> None: ...

    @property
    def registry(self) -> AdapterRegistry: ...

    async def fetch_window(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        archiver: str | None = None,
        max_points: int | None = None,
    ) -> tuple[str, list[Sample], bool]:
        """Resolve archiver → call adapter.fetch_window → normalize errors → decimate if needed.

        Returns `(archiver_name, samples, decimated)` where `decimated` is True
        when the service-side fallback engaged.
        """

    async def fetch_binned(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        binning: Literal["auto", "time"],
        archiver: str | None = None,
        target_buckets: int | None = None,
        bin_size_ns: int | None = None,
    ) -> tuple[str, list[BinnedPoint]]:
        """Returns `(archiver_name, points)`."""

    async def metadata(self, pv: str, *, archiver: str | None = None) -> PVMetadata: ...

    async def search(
        self,
        pattern: str,
        *,
        archiver: str | None = None,
        limit: int = 100,
    ) -> tuple[str, list[str]]:
        """Returns `(archiver_name, pv_names)`."""

    async def list_archivers(self) -> ArchiverListDTO:
        """Concurrent fan-out via the registry; URLs are credential-redacted."""

    async def health_summary(self) -> HealthSummaryDTO:
        """Per-archiver rollup with `status` ∈ {ok, degraded, down}.

        `ok` when all archivers are healthy (or none configured), `down` only
        when all are unhealthy, `degraded` otherwise.
        """

    # --- internal -------------------------------------------------------- #

    def _resolve(self, name: str | None) -> ArchiverAdapter:
        """Map name (or default) to an adapter; raise ArchiverNotConfigured if absent."""
```

## 12. Adapter contract — `canopy.mole.adapters.base`

The protocol every archiver type implements. v1 ships exactly one type — `EpicsArchiverApplianceAdapter` — which is configured N times for N appliance instances. Other types come from other labs' packages via the dotted-path config escape hatch.

```python
class ArchiverAdapter(Protocol):
    name: str               # injected by registry from config key
    type_name: str          # for /archivers introspection ("epics_archiver_appliance")

    @classmethod
    def from_config(cls, name: str, cfg: ArchiverInstanceConfig) -> "ArchiverAdapter":
        """Build an instance from validated config. Owns its own httpx client."""

    async def fetch_window(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        max_points: int | None = None,
    ) -> list[Sample]:
        """Return raw samples in [t0_ns, t1_ns]. May ignore max_points; service decimates."""

    async def fetch_binned(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        binning: Literal["auto", "time"],
        target_buckets: int | None,
        bin_size_ns: int | None,
    ) -> list[BinnedPoint]:
        """Return statistical buckets in the requested mode.

        - `"auto"`: pick bucket widths to land on ~target_buckets. The
          Appliance adapter maps this to `optimized_<N>(PV)`. Other adapters
          map to their native adaptive-bucket operator.
        - `"time"`: uniform `bin_size_ns` buckets. The Appliance adapter
          maps this to `mean_<dt>(PV)`. Other adapters map to their native
          GROUP BY time().

        Adapters that legitimately cannot implement a mode raise
        `BinningNotSupported` (the registry surfaces this as 501); both
        modes are part of the v1 contract, so this should be rare."""

    async def metadata(self, pv: str) -> PVMetadata: ...
    async def search(self, pattern: str, *, limit: int) -> list[str]: ...

    async def health(self) -> AdapterHealth:
        """Cheap reachability check — typically a single HEAD or status endpoint hit."""

    async def close(self) -> None:
        """Drain the httpx client. Called from app.lifespan shutdown."""
```

### 12.1 Severity normalization

Adapters MUST map archiver-native severity codes to Canopy's `AlarmSeverity` enum. The Appliance returns integer severity codes; the mapping the Appliance adapter applies (and that any other Appliance-compatible adapter should match) is:

| Wire value | `AlarmSeverity` |
|---|---|
| `0` | `"NO_ALARM"` |
| `1` | `"MINOR"` |
| `2` | `"MAJOR"` |
| `3` | `"INVALID"` |
| anything else | `"INVALID"` |

For binned points where multiple severities appear in a bucket, return the **worst** severity in that bucket (`INVALID > MAJOR > MINOR > NO_ALARM`). The Appliance does not return per-bucket severity in `optimized_<N>` responses; the adapter sets `severity="NO_ALARM"` for binned points unless the operator carries severity info.

### 12.2 Adapter type registration

Adapters register their short name with the registry:

```python
# canopy/mole/adapters/appliance.py
@register_archiver_type("epics_archiver_appliance")
class EpicsArchiverApplianceAdapter:
    @classmethod
    def from_config(cls, name: str, cfg: ArchiverInstanceConfig) -> "EpicsArchiverApplianceAdapter": ...
    # …
```

For out-of-tree adapters, config uses the `module:Class` form and the registry imports it dynamically:

```toml
[mole.archivers.lab_xyz]
type = "labxyz.canopy_plugin:XYZArchiverAdapter"
url  = "https://archiver.lab-xyz.example/"
```

This keeps Canopy's repo unaware of any non-EPICS-Appliance archiver. The lab ships their adapter in their own deploy package; deployment imports it via config alone.

## 13. Wire surface — `/mole/v1/...`

All routes mounted at `/mole/v1/...` from `canopy/mole/app.py`. Field casing on the wire is camelCase; Python types are snake_case (`CanopyModel`'s `alias_generator=to_camel` handles the conversion — see §4.1). Wire timestamps are accepted as ISO-8601 (millisecond precision is sufficient — adapters truncate as needed) or epoch nanoseconds.

**Error envelope.** Every error response — handled or unhandled — has the shape:

```json
{ "code": "<slug>", "detail": "<human-readable message>" }
```

The `code` slug is a stable machine-readable identifier from `canopy.core.errors.ErrorCode` (e.g. `archiver_not_configured`, `pv_not_found_in_archiver`). Clients should branch on `code`, not on parsing `detail`. The status-code tables below name both the HTTP status and the slug.

### 13.1 Endpoint summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET`  | `/mole/v1/archivers`            | read   | List configured archivers + per-instance health. |
| `GET`  | `/mole/v1/data/{pv}`            | read   | Time-window read for one PV. Raw samples by default; binned buckets when `binning` is set. |
| `GET`  | `/mole/v1/metadata/{pv}`        | read   | Per-PV metadata as the archiver knows it (mostly `EGU` and `PREC`). |
| `GET`  | `/mole/v1/search`               | read   | Pattern-search a single archiver's PV index; returns names. |
| `GET`  | `/mole/v1/health/summary`       | read   | Per-archiver health roll-up. |
| `GET`  | `/mole/v1/health/watchdog`      | none   | Liveness heartbeat. Unauthenticated (used by k8s probes). |
| `POST` | `/mole/v1/exports` *(planned)*  | write  | Enqueue a CSV/MATLAB export job; returns a job id. Not built in v1. |

### 13.2 `GET /mole/v1/archivers`

List all configured archiver instances with current health. Used to populate the frontend's archiver dropdown and by ops dashboards.

**Query parameters:** *(none)*

**Response 200** — `ArchiverListDTO`:

| Field | Type | Description |
|---|---|---|
| `default`     | `string`             | Name of the configured default archiver. |
| `archivers`   | `ArchiverInfoDTO[]`  | One entry per configured instance. |

`ArchiverInfoDTO`:

| Field | Type | Description |
|---|---|---|
| `name`        | `string`        | Instance name (config key). |
| `typeName`    | `string`        | Adapter type, e.g. `"epics_archiver_appliance"`. |
| `url`         | `string`        | Base URL (any embedded credentials redacted). |
| `healthy`     | `boolean`       | Result of the most recent `health()` probe. |
| `latencyMs`   | `number \| null`| Probe latency, ms. |
| `lastError`   | `string \| null`| Most recent error message, if any. |

**Errors:** none beyond standard auth (401/403).

### 13.3 `GET /mole/v1/data/{pv}`

Fetch one PV's data over a time window. Returns raw samples by default; returns binned statistical buckets when `binning` is set. The response DTO differs between the two modes — clients should branch on the `mode` discriminator.

**Path parameters:**

| Name | Type | Description |
|---|---|---|
| `pv` | `string` | URL-encoded PV name. Case-sensitive. |

**Query parameters:**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `from`        | `string` (ISO-8601) or `int` (epoch-ns) | yes | —          | Inclusive window start. |
| `to`          | `string` (ISO-8601) or `int` (epoch-ns) | no  | `"now"`    | Inclusive window end. `now` resolved at request entry. |
| `archiver`    | `string`                                | no  | configured default | Archiver instance name. |
| `binning`     | `"auto" \| "time"`                      | no  | —     | If set, switches to binned mode. `"auto"` lets the adapter pick bucket widths to hit `maxPoints`; `"time"` uses fixed-width buckets of `binSize`. |
| `maxPoints`   | `integer ≥ 1`                           | conditional | `null` | Raw mode: cap on returned samples; service decimates if adapter returned more. `binning="auto"`: target bucket count (required). Ignored when `binning="time"`. |
| `binSize`     | `int` (ns) as string                    | conditional | — | Required when `binning="time"`; ignored otherwise. v1 accepts integer nanoseconds only — ISO-8601 duration support is deferred (frontends compute ns themselves). |

**Response 200 (raw mode — `binning` absent)** — `ArchiverSeriesDTO`:

| Field | Type | Description |
|---|---|---|
| `mode`      | `"raw"`           | Discriminator. |
| `pv`        | `string`          | Echo of the requested PV. |
| `archiver`  | `string`          | Which instance answered. |
| `from`      | `int` (epoch-ns)  | Window start as resolved. |
| `to`        | `int` (epoch-ns)  | Window end as resolved. |
| `decimated` | `boolean`         | Whether the fallback decimator engaged. |
| `samples`   | `SampleDTO[]`     | Time-ordered samples. |

`SampleDTO`:

| Field | Type | Description |
|---|---|---|
| `tsNs`     | `int`                                    | UTC nanoseconds. |
| `value`    | `number \| string \| boolean \| null`    | `null` only at synthetic boundary samples. |
| `severity` | `"NO_ALARM" \| "MINOR" \| "MAJOR" \| "INVALID"` | Mapped from archiver-native severity (see §12.1). |
| `source`   | `"archive"`                              | Always `"archive"` from Mole. |

**Response 200 (binned mode — `binning` set)** — `ArchiverBinnedSeriesDTO`:

| Field | Type | Description |
|---|---|---|
| `mode`         | `"binned"`           | Discriminator. |
| `pv`           | `string`             | Echo of the requested PV. |
| `archiver`     | `string`             | Which instance answered. |
| `from`         | `int` (epoch-ns)     | Window start as resolved. |
| `to`           | `int` (epoch-ns)     | Window end as resolved. |
| `binning`      | `"auto" \| "time"`   | Echo of the requested mode. |
| `binSizeNs`    | `int \| null`        | Set when `binning="time"`; `null` when `"auto"` (bucket widths may vary; see per-point `binNs`). |
| `targetBuckets`| `int \| null`        | Set when `binning="auto"`; `null` otherwise. |
| `points`       | `BinnedPointDTO[]`   | Time-ordered buckets. |

`BinnedPointDTO`:

| Field | Type | Description |
|---|---|---|
| `tsNs`    | `int`             | Bucket start, UTC ns. |
| `binNs`   | `int`             | Bucket width, ns. |
| `mean`    | `number`          | Bucket mean. |
| `stdDev`  | `number`          | Bucket standard deviation. |
| `min`     | `number`          | Bucket min. |
| `max`     | `number`          | Bucket max. |
| `count`   | `int`             | Underlying-sample count. |
| `severity`| same enum as `SampleDTO.severity` | Worst severity in the bucket; `"NO_ALARM"` when archiver does not report per-bucket severity. |

**Errors:**

| Status | `code` slug | Condition |
|---|---|---|
| 400 | `bad_request` | Malformed `from`/`to`, `from > to`, `binning="auto"` without `maxPoints`, or `binning="time"` without `binSize`. |
| 404 | `archiver_not_configured` | `archiver` is not in the registry. |
| 404 | `pv_not_found_in_archiver` | PV is not indexed by this archiver. |
| 501 | `binning_not_supported` | Adapter does not implement the requested binning mode. |
| 502 | `archiver_bad_response` | Adapter returned a malformed response. |
| 503 | `archiver_unavailable` | Archiver reachable but errored, or unreachable. |
| 504 | `archiver_timeout` | Adapter timed out. |

### 13.4 `GET /mole/v1/metadata/{pv}`

Return per-PV metadata as the archiver knows it. For the Appliance adapter only `units` and `precision` are populated; alarm limits, display limits, and descriptions live in EPICS records and are fetched via Channel Access (Hare / EPICS Connector), not this endpoint.

**Path parameters:** `pv: string`.

**Query parameters:**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `archiver` | `string` | no | configured default | Archiver instance name. |

**Response 200** — `PVMetadataDTO`:

| Field | Type | Appliance-populated? | Description |
|---|---|---|---|
| `pv`            | `string`           | yes | Canonical PV name. |
| `archiver`      | `string`           | yes | Reporting instance. |
| `description`   | `string \| null`   | no  | Free-text description (CA `.DESC`). |
| `units`         | `string \| null`   | yes | EGU. |
| `precision`     | `int \| null`      | yes | PREC. |
| `displayLow`    | `number \| null`   | no  | LOPR. |
| `displayHigh`   | `number \| null`   | no  | HOPR. |
| `alarmLow`      | `number \| null`   | no  | LOLO. |
| `alarmHigh`     | `number \| null`   | no  | HIHI. |
| `warningLow`    | `number \| null`   | no  | LOW. |
| `warningHigh`   | `number \| null`   | no  | HIGH. |

**Errors:** 404 `archiver_not_configured` / `pv_not_found_in_archiver`; 502/503/504 as in §13.3.

### 13.5 `GET /mole/v1/search`

Pattern-search a single archiver's PV index; returns names only. For full metadata, call §13.4 per-PV.

**Query parameters:**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `pattern`   | `string`        | yes | —     | Archiver-native pattern. Appliance: regex via `searchForPVsRegex`. |
| `archiver`  | `string`        | no  | default | Instance to query. |
| `limit`     | `int` (1..1000) | no  | `100` | Max names to return. |

**Response 200** — `SearchResultsDTO`:

| Field | Type | Description |
|---|---|---|
| `archiver`  | `string`     | Which instance answered. |
| `pattern`   | `string`     | Echo of the input pattern. |
| `pvs`       | `string[]`   | PV names in archiver-native order. |

**Errors:** 400 `bad_request`; 404 `archiver_not_configured`; 502/503/504 as in §13.3.

### 13.6 `POST /mole/v1/exports` *(planned, not built in v1)*

Enqueue a multi-PV export. Long-running by definition (typical exports span hours of data and many PVs); request will return immediately with a job id, client polls `/jobs/v1/{id}`. The Jobs module, the Arq worker pool, and the export tasks (`tasks/export_csv.py`, `tasks/export_matlab.py`) are not yet built; this section is preserved as the v1.x design target.

**Request body** — `NewExportDTO`:

| Field | Type | Required | Description |
|---|---|---|---|
| `pvs`       | `string[]`                  | yes | PV names. |
| `from`      | `string \| int`             | yes | ISO-8601 or epoch-ns. |
| `to`        | `string \| int`             | no  | Default `"now"`. |
| `format`    | `"csv" \| "matlab"`         | yes | Output format. |
| `archiver`  | `string`                    | no  | Default: configured default. |
| `maxPointsPerPv` | `int \| null`          | no  | Optional decimation cap, applied per PV. |

**Response 202** — `JobCreatedDTO` (defined in `canopy.contracts.jobs`):

| Field | Type | Description |
|---|---|---|
| `jobId`     | `string` (UUID) | Poll via `/jobs/v1/{jobId}`. |
| `statusUrl` | `string`        | Convenience absolute URL. |

**Errors:** 400 `bad_request` (empty `pvs`, unknown `format`, malformed times); 404 `archiver_not_configured`.

### 13.7 `GET /mole/v1/health/summary`

Per-archiver roll-up. The endpoint fans out `health()` to every configured adapter concurrently with a 2 s timeout (independent of per-archiver `timeout_s`); the response is the per-archiver result table plus an overall status.

**Response 200** — `HealthSummaryDTO`:

| Field | Type | Description |
|---|---|---|
| `status`     | `"ok" \| "degraded" \| "down"` | `degraded` when any archiver is unhealthy; `down` only when *all* are. |
| `archivers`  | `ArchiverHealthDTO[]`           | One entry per configured instance. |

`ArchiverHealthDTO`:

| Field | Type | Description |
|---|---|---|
| `name`            | `string`         | Instance name. |
| `healthy`         | `boolean`        | Result of the probe. |
| `latencyMs`       | `number \| null` | Probe latency. |
| `lastError`       | `string \| null` | Most recent error, if any. |
| `lastCheckTsNs`   | `int`            | Timestamp of the probe in this response. |

### 13.8 `GET /mole/v1/health/watchdog`

Standard heartbeat surface used by liveness/readiness probes; does not call any adapter.

**Response 200** — `HeartbeatDTO`:

| Field | Type | Description |
|---|---|---|
| `module`      | `string` | `"mole"`. |
| `version`     | `string` | Build version. |
| `uptimeS`     | `number` | Seconds since process start. |
| `nowTsNs`     | `int`    | Server clock at response time. |

## 14. Testing strategy

- **Unit tests for `EpicsArchiverApplianceAdapter`** with `pytest-httpx` (mock transport): per-method known-input → known-output. Includes error mapping (4xx → `PVNotFoundInArchiver`, 5xx → `ArchiverUnavailable`, timeout → `ArchiverTimeout`, malformed JSON → `ArchiverBadResponse`). Includes both raw and `optimized_<N>` response shapes — they have different parsing paths.
- **Contract tests for `ArchiverAdapter`**: a parameterized test module that any adapter can be plugged into. Asserts response-normalization invariants (sample ordering, `source="archive"`, severity enum mapping, binned-point field completeness). The reference adapter passes; out-of-tree adapters can pull it in to verify their own implementations.
- **Unit tests for `decimation.uniform_time_decimate`**: property-based (Hypothesis), monotonic timestamps, target `max_points` honored, edge cases (fewer samples than max → identity; equally-spaced input).
- **Integration tests for `ArchiverService`** with a `FakeArchiverAdapter` registered in the registry: verifies dispatch (default vs. named), error normalization, decimation fallback engaging when adapter ignores the hint, registry rejecting unknown names, raw-vs-binned routing.
- **App-level tests** using FastAPI's TestClient and a fake registry built in a test fixture: verifies route → service wiring, status code mapping, query-param parsing (including the binning-mode discriminator).
- **No live archiver in CI.** A nightly job hitting a SLAC dev archiver lives outside the unit suite; it gates promotion, not commits.

## 15. Open architectural questions (deferred, listed for visibility)

1. **Per-PV routing.** Config table mapping pattern → archiver name, for sites with many archivers and PVs that obviously belong to one. Add when the manual-override pattern stops scaling.
2. **Failover.** Per-archiver `fallback = "<name>"` knob, attempted only on `ArchiverUnavailable`. Add when a caller has a use case that can tolerate non-deterministic source.
3. **Response cache.** Redis, keyed by `(archiver, pv, t0, t1, mode, binning, maxPoints, binSize)`. Add when profiling shows the appliance round-trip is the bottleneck for plot loads.
4. **Cross-archiver search.** Fan out, merge, dedupe by PV name. Currently search is one-archiver-at-a-time; add fan-out when there is a UI surface that needs it.
5. **Adapter-specific operator passthrough.** A caller might need an Appliance-only operator like `firstSample_<dt>(PV)` or a custom-lab aggregation. If common enough, lift to a new intent verb; otherwise expose via an `extras` passthrough on `/data/{pv}` that's documented as adapter-specific and explicitly non-portable. Deferred until a caller asks.
6. **Streaming for large windows.** `GET /data/{pv}` switches to chunked transfer (NDJSON or CBOR frames) above a size threshold. Add when typical export/plot sizes outgrow a single response buffer.
