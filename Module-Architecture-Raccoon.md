Raccoon is the PV name-lookup module. It wraps the lab's directory service so other modules can ask three kinds of questions without each carrying their own client: *what PVs match this pattern*, *does this PV name exist*, and *does this batch of PV names exist*. At SLAC the upstream is the EPICS v4 **Directory Service**, accessed via the [`meme`](https://github.com/slaclab/meme) Python wrapper. Other labs swap in their own directory backend by implementing a `DirectoryAdapter`.

If `canopy-api` restarts, Raccoon starts with an empty in-memory cache. The adapter is reachable immediately; the cache warms itself on the first call for each PV.

> Status: this page is a *design proposal*. Issue #10 is the tracking ticket. Open questions are concentrated in §12. Where this page contradicts the older `canopy-handoff.md`, this page wins.

## 1. Position in the call graph

```
              ┌── validate on subscribe ──┐
              │                            ▼
            Hare ──────────────────────► Raccoon ───► DirectoryAdapter ───► Directory Service
            Mole ─── (optional) ──────►   │              (meme.names)
            Squirrel ── validate-on-restore ─►│
            Owl ── validate legacy import ──►│
            Frontend ── HTTP autocomplete ──►│  /raccoon/v1/...
```

- **Callers** (in-process, via `raccoon.gateway`): Hare (PV-name validation on subscribe), Squirrel (which PVs in this snapshot still exist?), Owl (legacy `.trc`/`.xml`/`.stp` import validates names against the directory).
- **Callers** (HTTP, `/raccoon/v1/...`): the frontend (PV-name autocomplete, browse-by-tag, region filters), notebooks, ad-hoc scripts.
- **Callees**: a single configured `DirectoryAdapter` instance. The SLAC adapter wraps `meme.names`; other labs supply their own.
- **Not callers** (deliberately): Raccoon does *not* call Mole, Vole, Hare, or the EPICS Connector. "Is this PV archived?" is Mole's question (`/mole/v1/metadata/{pv}` and `/mole/v1/search`); "is this PV live-connectable?" is the EPICS Connector's question. Raccoon answers only "is this PV known to the directory."

Raccoon publishes nothing on the event bus and subscribes to nothing. It is a synchronous request/response module.

## 2. Boundaries — what Raccoon owns vs. delegates

| Concern | Raccoon owns | Raccoon delegates to |
|---|---|---|
| Pattern → list of PV names | ✅ (via adapter) | — |
| Pattern → list of device names | ✅ | — |
| Pattern → list of element (MAD) names | ✅ | — |
| Single-PV existence check | ✅ (built on `list_pvs`) | — |
| Batch existence check | ✅ | — |
| Result caching (per-PV existence, per-pattern queries) | ✅ | — |
| Single-flight de-duplication of concurrent identical lookups | ✅ | — |
| Translating upstream errors → `CanopyError` slugs | ✅ | — |
| Wildcard / regex pattern syntax | — | adapter (passthrough; semantics belong to the upstream) |
| "Is this PV in the archive?" | — | Mole (`mole.gateway.metadata` / `search`) |
| "Is this PV live-connectable right now?" | — | EPICS Connector (via Hare) |
| Tag taxonomy (L1/L2/BSY/...) | — | adapter (the upstream defines them; Raccoon is opaque) |
| PV name *canonicalization* | — | callers (Raccoon does not normalize case, fields, or aliases) |
| Persistence | — | (none — cache is volatile per replica) |

The discipline: **Raccoon answers directory questions, nothing else.** A unified "validate + is-archived" composite is the caller's job (or a future cross-cutting endpoint), not Raccoon's.

## 3. Internal structure

The standard module shape, but the data tier is an in-process cache rather than SQLAlchemy or a ring buffer:

```
src/canopy/raccoon/
  app.py                       # sub-FastAPI: v1 router + canopy error handler + lifespan
  runtime.py                   # uptime_s() for /health/watchdog
  api/v1/
    router.py                  # APIRouter(prefix="/v1")
    pvs.py                     # GET  /pvs            (list)
                               # GET  /pvs/{pv}/exists
                               # POST /pvs/validate   (batch)
    devices.py                 # GET  /devices
    elements.py                # GET  /elements
    health.py                  # GET  /health/{summary,watchdog}
  schemas/
    directory.py               # ListQueryDTO, NameListResultDTO, ExistsResultDTO,
                               # ValidateRequestDTO, ValidateResultDTO
  services/
    directory_service.py       # DirectoryService — orchestration + caching + single-flight
  cache/
    name_cache.py              # PVNameCache — bounded LRU with per-entry TTL
    pattern_cache.py           # PatternCache — keyed on (kind, pattern, tag, sort_by, element_type)
    flight.py                  # SingleFlight — coalesce concurrent identical calls
  adapters/
    base.py                    # DirectoryAdapter protocol + AdapterHealth + error types + type registry
    registry.py                # AdapterRegistry; build_registry(RaccoonConfig); module:Class dotted-path resolution
    meme.py                    # MemeDirectoryAdapter — the only built-in adapter in v1
  config.py                    # RaccoonConfig + DirectoryInstanceConfig
  gateway.py                   # public surface for other modules
  dependencies.py              # typed Depends aliases
```

The `cache/` directory plays the role that `data/` plays in a DB-backed module. It is the data tier; nothing in `services/` or `api/` reaches past it to the adapter without going through `DirectoryService`.

## 4. Data model

Raccoon has no domain entities of its own; it returns lists of strings (and a few wrapper DTOs for batch responses). The upstream `meme.names` API also returns plain `list[str]` for all three kinds — we do not pretend a richer record exists.

```python
class NameKind(StrEnum):
    PV = "pv"
    DEVICE = "device"
    ELEMENT = "element"


@dataclass(frozen=True)
class CacheKey:                        # internal to cache/ — not a DTO
    kind: NameKind
    pattern: str
    tag: str | None
    sort_by: str | None
    element_type: str | None
```

The cache stores two distinct populations:

- **Pattern cache**: `CacheKey → list[str]`, bounded LRU. Used for `list_pvs` / `list_devices` / `list_elements`. TTL is configurable; default 5 minutes (the directory service changes rarely, but new PVs do appear after IOC restarts).
- **Existence cache**: `pv → ExistsEntry(known: bool, expires_at: float)`, bounded LRU. Hit on `exists()` and on every entry of a batch `validate()`. TTL longer than the pattern cache (default 30 minutes) because individual PVs that *exist* keep existing for a long time. Negative results have a shorter TTL (default 60 seconds) to avoid pinning a transient miss.

Both caches are per-replica. Each `canopy-api` process has its own; there is no cross-replica synchronization.

## 5. Caching strategy — the only real design lever

Raccoon's upstream surface (three list calls, all returning `list[str]`) is too thin to justify a whole module unless caching earns its keep. It does, on three hot paths:

1. **Hare subscribe-time validation.** Every WebSocket subscribe runs `raccoon.exists(pv)`. Without a cache, every reconnect storm hammers the directory service. With a 30-minute existence cache, after the first connection a subscribe-storm of 10k PVs hits the directory zero times.
2. **Squirrel snapshot restore.** A snapshot can name 50k PVs. Without batching + caching, that's 50k individual lookups. With batch `validate()` + cache + single-flight, it's at worst one batch upstream call (and zero on a warm cache).
3. **Owl legacy import.** Importing a PyDM `.trc` file with hundreds of references runs the same batch path.

Three primitives carry this:

- **Bounded LRU** (per cache). When the cache hits its size ceiling, evict by least-recently-accessed. Defaults: 100k entries for the existence cache, 10k entries for the pattern cache (patterns are heavier).
- **Per-entry TTL.** Above. Negative existence results have their own (shorter) TTL.
- **Single-flight.** If 100 concurrent requests arrive for the same uncached `(kind, pattern, tag, sort_by, element_type)`, only one upstream call goes out; the rest await the same future. Implemented as a `dict[CacheKey, asyncio.Future]` guarded by a short asyncio lock. Once the call resolves, the result populates the cache and every awaiter receives it.

Cold-start strategy is **none**. Raccoon does not pre-warm. The first request after a restart pays full upstream latency; subsequent requests are local.

## 6. Multi-replica behavior

Each `canopy-api` replica builds its own `DirectoryAdapter` and its own caches. **No shared distributed state.** Justification mirrors Vole's:

- Cross-replica synchronization would require a shared store (Redis with stringly-typed keys, a TTL'd hash, etc.). The win is small: directory results are stable on the order of hours, so a per-replica cache is already well-behaved on a hit-rate basis.
- A request that lands on a cold replica pays one upstream lookup, populates that replica's cache, and from then on is fast. The cost of the cold miss is acceptable.
- If every replica caches independently, the directory service sees `N_replicas × (unique queries / TTL)` traffic — bounded and small.

If at some future scale the directory service can't take that load (it can; it's not the hot path), the answer is to put a Redis-backed read-through layer *between* `DirectoryService` and the adapter, not to change the gateway contract.

## 7. Single configured adapter, but the type is swappable

Mole has multiple archiver instances (one registry entry per appliance). Raccoon is different: in practice a deployment talks to **one** directory service. The shape of the registry is therefore degenerate — one entry — but Raccoon still goes through the same `AdapterRegistry` mechanism so that:

- The adapter *type* remains pluggable. The SLAC build registers `MemeDirectoryAdapter` as `meme`. Another lab points `RaccoonConfig.directory.type = "ourlab.directory:OurAdapter"` and the registry resolves the dotted path.
- Tests substitute a `FakeDirectoryAdapter` via the same `gateway.bind(service)` pattern Mole and Vole use.

Configuration is a single `DirectoryInstanceConfig` block (not a dict of them):

```yaml
raccoon:
  directory:
    type: meme            # or "module:Class" dotted path
    timeout_s: 5
    cache:
      pattern_ttl_s: 300
      exists_ttl_s: 1800
      negative_ttl_s: 60
      max_pattern_entries: 10000
      max_exists_entries: 100000
```

## 8. Adapter lifecycle

**Startup.** Raccoon exposes `raccoon_lifespan()` (an `@asynccontextmanager` in `canopy.raccoon.app`); `canopy.main.lifespan` enters it explicitly because mounted sub-apps do not run their own lifespan automatically. `raccoon_lifespan` resolves the configured adapter type (short-name → `@register_directory_type` decorated class, or `module:Class` → `importlib.import_module`), instantiates it via `AdapterClass.from_config(cfg)`, constructs the caches and `DirectoryService`, and binds the service on the gateway via `gateway.bind(service)`. The `MemeDirectoryAdapter` instantiates a `meme.names` client (or whatever wrapper the upstream provides) at this point; it does not lazily connect on first use. Startup fails loud if the adapter type cannot be resolved or if `from_config` raises.

**Per-request.** The adapter is reused across requests. `meme.names` is synchronous; `MemeDirectoryAdapter` runs each upstream call in a thread executor (`asyncio.to_thread`) so the FastAPI event loop is never blocked. Per-call timeout is enforced via `asyncio.wait_for` with the configured `timeout_s`.

**Health checks.** `DirectoryAdapter.health()` performs a cheap probe — for `MemeDirectoryAdapter`, a `list_pvs("__healthcheck__:nonexistent")` whose only goal is to exercise the round-trip. A successful empty result means the directory service is reachable. `/health/summary` returns the adapter status plus current cache sizes / hit rates.

**Shutdown.** `raccoon_lifespan` calls `adapter.close()` (no-op for `MemeDirectoryAdapter` if `meme.names` keeps no persistent state) and unbinds the gateway via `gateway.bind(None)`.

## 9. Public API — `canopy.raccoon.gateway`

The gateway is the only symbol other modules import from Raccoon. It is a thin facade over `DirectoryService`; its purpose is to keep the import surface stable as the implementation evolves.

```python
"""
canopy.raccoon.gateway
=======================

Public API for the Raccoon directory-lookup module.

Other Canopy modules import only from this file; the rest of `canopy.raccoon.*`
is private implementation. The signatures here form the contract that survives
internal refactoring.

Pattern semantics (Oracle-style `%` wildcards or regex) are defined by the
upstream directory service. Raccoon passes patterns through verbatim; callers
are responsible for understanding what their pattern means.

`tag`, `sort_by`, and `element_type` are opaque strings; Raccoon does not
validate them against any taxonomy. The upstream directory service rejects
unknown values, and Raccoon surfaces the rejection as `InvalidQuery`.
"""

from canopy.raccoon.schemas.directory import (
    NameListResultDTO,
    ExistsResultDTO,
    ValidateResultDTO,
)


# --------------------------------------------------------------------------- #
# Listing — pattern queries
# --------------------------------------------------------------------------- #

async def list_pvs(
    pattern: str,
    *,
    tag: str | None = None,
    sort_by: str | None = None,
    element_type: str | None = None,
) -> list[str]:
    """Resolve a pattern to a list of PV names.

    Cached on `(pattern, tag, sort_by, element_type)` for `pattern_ttl_s`.
    Concurrent identical calls are coalesced via single-flight; only one
    upstream request is in flight at a time per cache key.

    Args:
        pattern: Oracle-style wildcard (`BPMS:%:%:X`) or regex (`(XCOR|BPMS):.*`).
        tag, sort_by, element_type: Passed verbatim to the directory adapter.

    Returns:
        List of PV names. Empty list is a valid result, not an error.

    Raises:
        DirectoryUnavailable: adapter could not reach the directory service.
        DirectoryTimeout: upstream call exceeded `timeout_s`.
        InvalidQuery: upstream rejected the pattern / tag / sort_by / element_type.
    """


async def list_devices(
    pattern: str,
    *,
    tag: str | None = None,
    sort_by: str | None = None,
    element_type: str | None = None,
) -> list[str]: ...


async def list_elements(
    pattern: str,
    *,
    tag: str | None = None,
    sort_by: str | None = None,
    element_type: str | None = None,
) -> list[str]: ...


# --------------------------------------------------------------------------- #
# Existence — single and batch
# --------------------------------------------------------------------------- #

async def exists(pv: str) -> bool:
    """Return True if `pv` is known to the directory service.

    Implemented as `pv in (await list_pvs(pv))`. Result is cached in the
    existence cache (positive: `exists_ttl_s`; negative: `negative_ttl_s`).

    `pv` MUST be a literal PV name, not a pattern. If it contains `%` or
    regex metacharacters, `InvalidQuery` is raised — use `list_pvs` for
    pattern queries.

    Raises:
        DirectoryUnavailable, DirectoryTimeout: as `list_pvs`.
        InvalidQuery: `pv` contains pattern metacharacters.
    """


async def validate(pvs: list[str]) -> ValidateResultDTO:
    """Batch existence check for a list of literal PV names.

    Splits `pvs` into hits and misses against the existence cache, then
    issues a single batched upstream lookup for the misses (the adapter
    decides whether "batched" means one regex `(A|B|C|...)` or N parallel
    calls — `MemeDirectoryAdapter` builds an alternation regex up to a
    configurable size and chunks above that). Caches every result.

    Returns:
        ValidateResultDTO(known=[...], unknown=[...]) — preserves caller's
        input order in each list.

    Raises:
        DirectoryUnavailable, DirectoryTimeout: as above. Partial failure
            is not represented; either the batch resolves or it raises.
        InvalidQuery: any input string contains pattern metacharacters.
    """


# --------------------------------------------------------------------------- #
# Introspection
# --------------------------------------------------------------------------- #

async def stats() -> "RaccoonStatsDTO":
    """Return cache hit rates, sizes, in-flight counts, and adapter health.

    Used by `/raccoon/v1/health/summary`. Cheap; no upstream call.
    """
```

## 10. `DirectoryService` — the implementation behind the gateway

`DirectoryService` is the orchestrator. The gateway functions are one-line wrappers around its methods.

```python
class DirectoryService:
    """Coordinates adapter, caches, and single-flight; the implementation behind the gateway.

    Lifecycle: one instance per `canopy-api` process, constructed by
    `raccoon_lifespan`. Tests can construct their own instance with a fake
    adapter and substitute it via `canopy.raccoon.gateway.bind(service)`.

    Concurrency: fully async. The adapter call is the only blocking site;
    sync adapters (like meme) are wrapped in `asyncio.to_thread`. Caches
    are guarded by short asyncio locks held only across get/set, never
    across upstream calls.
    """

    def __init__(
        self,
        adapter: DirectoryAdapter,
        pattern_cache: PatternCache,
        name_cache: PVNameCache,
        flight: SingleFlight,
        config: RaccoonConfig,
    ) -> None: ...

    async def list(
        self,
        kind: NameKind,
        pattern: str,
        *,
        tag: str | None = None,
        sort_by: str | None = None,
        element_type: str | None = None,
    ) -> list[str]:
        """Pattern lookup with cache + single-flight.

        Steps: build CacheKey → pattern_cache.get → return on hit →
        flight.start(key, _do_upstream) → adapter call in thread executor →
        store result → wake all awaiters → on PV-kind, opportunistically
        warm the existence cache for every returned name (positive entries).
        """

    async def exists(self, pv: str) -> bool:
        """Single-PV existence; rejects patterns; consults name_cache then list."""

    async def validate(self, pvs: list[str]) -> ValidateResultDTO:
        """Batch existence; partition by cache, fan out misses through `list`,
        merge, return."""

    async def stats(self) -> RaccoonStatsDTO: ...
```

The opportunistic warm in `list` is worth calling out: every successful `list_pvs(pattern)` populates `name_cache` with positive entries for every returned PV. After one good pattern call, individual `exists(pv)` for any of those PVs is free. Patterns are *not* used to populate negative entries — a pattern that returns 5 names tells you nothing about which other names are missing.

## 11. Wire surface — `/raccoon/v1/...`

```
GET  /raccoon/v1/pvs                ?pattern=&tag=&sortBy=&elementType=     -> NameListResultDTO
GET  /raccoon/v1/devices            (same query params)                      -> NameListResultDTO
GET  /raccoon/v1/elements           (same query params)                      -> NameListResultDTO
GET  /raccoon/v1/pvs/{pv}/exists                                              -> ExistsResultDTO
POST /raccoon/v1/pvs/validate       body: ValidateRequestDTO                  -> ValidateResultDTO
GET  /raccoon/v1/health/summary
GET  /raccoon/v1/health/watchdog
```

All query params are camelCase on the wire (`sortBy`, `elementType`); the DTO base's `to_camel` alias generator handles the snake_case ↔ camelCase mapping. All endpoints gated by `Security(require_read_access)`. None of them require write access — Raccoon never mutates upstream state.

`validate` is POST (not GET) because the input list can be large (snapshot restores routinely send 10k+ PVs) and PUTting URL-encoded query strings of that size is fragile.

Errors follow the project envelope `{code, detail}`:

| `CanopyError` subclass | `ErrorCode` slug | HTTP | When |
|---|---|---|---|
| `DirectoryUnavailable` | `raccoon/directory_unavailable` | 503 | adapter raised a connection / DNS / network error |
| `DirectoryTimeout` | `raccoon/directory_timeout` | 504 | per-call timeout exceeded |
| `InvalidQuery` | `raccoon/invalid_query` | 400 | upstream rejected the pattern; or `exists`/`validate` got pattern metacharacters |
| `AdapterMisconfigured` | `raccoon/adapter_misconfigured` | 500 | startup-time config errors that surface late |

## 12. Adapter contract: `canopy.raccoon.adapters.base`

```python
class DirectoryAdapter(Protocol):
    """One configured directory backend. v1 ships exactly one type:
    `MemeDirectoryAdapter`. Other types come from other labs' packages
    via the `module:Class` dotted-path config escape hatch.
    """

    name: str    # injected by registry from config

    @classmethod
    def from_config(cls, cfg: DirectoryInstanceConfig) -> "DirectoryAdapter": ...

    async def list_pvs(
        self,
        pattern: str,
        *,
        tag: str | None,
        sort_by: str | None,
        element_type: str | None,
    ) -> list[str]: ...

    async def list_devices(self, pattern: str, **kw) -> list[str]: ...

    async def list_elements(self, pattern: str, **kw) -> list[str]: ...

    async def health(self) -> AdapterHealth: ...

    async def close(self) -> None: ...
```

Adapter implementations MUST translate upstream-native errors into the four `CanopyError` subclasses above. For `MemeDirectoryAdapter`:

| `meme.names` raises | Adapter raises |
|---|---|
| `TimeoutError` (or `asyncio.TimeoutError` from `wait_for`) | `DirectoryTimeout` |
| connection / network exceptions | `DirectoryUnavailable` |
| upstream-rejected query (bad regex, unknown tag) | `InvalidQuery` |
| anything else unexpected | `DirectoryUnavailable` (logged at `error` with traceback) |

`MemeDirectoryAdapter` does NOT cache. Caching is `DirectoryService`'s job; the adapter is the thinnest possible bridge to `meme.names`.

### 12.1 Adapter type registration

Mirrors Mole exactly:

```python
@register_directory_type("meme")
class MemeDirectoryAdapter:
    ...
```

Out-of-tree adapters can also use the `module:Class` form in config without any decorator registration.

## 13. Testing strategy

- **Unit tests for `PVNameCache` and `PatternCache`**: TTL expiry, LRU eviction, negative entries, cache-size ceilings. Fast, no async.
- **Unit tests for `SingleFlight`**: 100 concurrent calls coalesce to 1 upstream; an upstream error propagates to all awaiters; subsequent calls retry.
- **Adapter contract suite**: parameterized over every adapter type registered. Every adapter must satisfy the same input/output assertions. SLAC's CI loads `MemeDirectoryAdapter` against a recorded fixture; out-of-tree labs run the same suite against theirs.
- **`MemeDirectoryAdapter` tests with `pytest`'s `mocker` over `meme.names.*`**: error-mapping matrix, thread-executor wrapping, timeout propagation.
- **`DirectoryService` integration tests**: cache hit/miss/expire, opportunistic name-cache warm from `list_pvs`, single-flight with a deliberately slow fake adapter.
- **Route tests**: auth gating, error-envelope slugs, camelCase query-param round-trip, batch `validate` with a 5k-PV input.
- **End-to-end**: dotted-path adapter loading from a fixture out-of-tree adapter, asserting Raccoon mounts and serves correctly.

No real `meme` import in the test suite; the SLAC adapter is exercised against a recorded-fixture transport so CI doesn't need network access.

## 14. Open architectural questions

1. **Default cache TTLs.** Starting points: pattern 5 min, exists 30 min, negative 60 s. Tune from observability. Question: should the existence TTL be longer (e.g., 6 hours) given that "PV exists in the directory" really does change rarely?
2. **Pattern metachar detection for `exists`.** Reject any string containing `%`, `*`, `?`, `(`, `|`, `[`? Or a stricter "must match `^[A-Za-z0-9:._-]+$`" rule? The latter is safer but rejects PV names that legitimately contain other characters at some labs.
3. **Batch `validate` strategy in the adapter.** `MemeDirectoryAdapter` can either (a) build one alternation regex `(A|B|C|...)` up to ~1k items per call, or (b) issue N concurrent `list_pvs` calls. (a) is fewer calls; (b) is simpler error handling and works with directories that don't support alternation. Default: (a) with a fallback to (b) on `InvalidQuery`.
4. **Tag enumeration endpoint?** A `GET /raccoon/v1/tags` that lists known tags would help the frontend build a "browse by region" UX — but `meme.names` has no `list_tags()`. We'd need to either (a) hardcode the SLAC tag list, (b) extract it as the union of tags appearing in some bootstrap pattern query, or (c) add a `list_tags` method to the adapter contract and require each lab to implement it. Defer to v2.
5. **Should `list_pvs` warm a per-pattern *negative* answer space?** Probably not — see §10.
6. **Composite "validate + is-archived" endpoint?** Out of scope for Raccoon. If the frontend ends up needing it, add it as a cross-cutting endpoint at the app level (`GET /pvs/{pv}/known` calls both gateways) rather than putting Mole knowledge inside Raccoon.
7. **Naming.** Issue title is "Raccoon", body uses "Racoon". This page commits to **Raccoon** (the correct English spelling). Update issue #10's body when the design lands.
