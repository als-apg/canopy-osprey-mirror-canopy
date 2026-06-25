# Vole architecture

This page documents Vole's internal design decisions and component interactions for developers working on or extending the module.

## Why Vole exists

Vole decouples Fox from direct dependencies on Hare (live broker) and Mole (archiver proxy). Without Vole, Fox would need to:

- Query Mole on every formula evaluation (network round-trip, latency)
- Maintain its own subscription management for live data
- Handle the complexity of merging live and historical data inline

Vole provides a single in-memory layer that:

- Absorbs live data from Hare continuously
- Seeds from Mole on first access (auto-provisioning)
- Serves Fox with sub-millisecond reads from memory
- Manages memory bounds and eviction independently

This separation means Fox only knows about the `DataSource` protocol — it doesn't know or care whether data came from a live broker, an archiver, or both.

## Ring buffer model

### PerPVRing

Each PV gets a dedicated `PerPVRing` instance — a bounded FIFO buffer backed by a sorted list of `StoredSample` namedtuples:

```python
StoredSample(ts_ns: int, value: Any, severity: AlarmSeverity, source: "live" | "archive")
```

Key behaviors:

- **Capacity**: Hard ceiling at `max_points`; oldest samples dropped on overflow
- **Ordering**: Strict monotonic timestamps; `append()` rejects out-of-order samples (returns `False`)
- **Seeding**: `seed()` bulk-inserts archive samples, deduplicating against existing live data (live wins at same timestamp, archive samples at or after earliest live sample are skipped)
- **Slicing**: `slice(t0_ns, t1_ns)` returns samples in the `[t0, t1]` inclusive range using binary search
- **Pre-window lookup**: `sample_before(ts_ns)` enables forward-fill alignment by finding the last sample before a given time

### PVStore

`PVStore` manages the collection of rings with lifecycle and concurrency control:

- **Lazy creation**: `get_or_create()` instantiates rings on demand with optional per-PV capacity overrides
- **Per-PV locks**: Each PV has its own `asyncio.Lock` for concurrent access safety
- **LRU tracking**: `lru_candidates()` returns rings sorted by last access time for eviction decisions
- **Stats aggregation**: `total_bytes`, `total_points`, `ring_count` for introspection

## Eviction policy

Vole uses a two-level capacity model:

1. **Per-ring cap** (`max_points`): FIFO within the ring — oldest samples silently drop
2. **Global byte cap** (`global_bytes_max`): LRU across all rings — entire rings are removed

### LRU eviction algorithm

```
When total_bytes > global_bytes_max:
    1. Sort all rings by last_access_ns ascending (oldest access first)
    2. Drop the least-recently-accessed ring entirely
    3. Call on_evict callback (unsubscribes from Hare)
    4. Repeat until total_bytes <= global_bytes_max
```

Eviction removes whole rings, not individual samples. This is intentional — partial eviction would leave fragmented data that produces misleading formula results. A fully evicted PV can be cleanly re-provisioned later.

### Access time tracking

Both reads and writes update `last_access_ns`:

- `append()` and `seed()` touch the ring (write path)
- `read_window()` and `read_aligned()` touch the ring (read path)

This means actively-queried PVs stay warm even if they receive data infrequently.

## ChangeNotifier pattern

`ChangeNotifier` implements push-based pub/sub for PV data changes:

```
BufferService.append(pv, sample)
    │
    ├── Appends to ring
    └── notifier.notify(pv)
            │
            ├── asyncio.create_task(callback_1(pv))
            ├── asyncio.create_task(callback_2(pv))
            └── ...  (fire-and-forget)
```

Design decisions:

- **Fire-and-forget**: Callbacks are launched as independent asyncio tasks. Failures are logged but don't block the data path or affect other callbacks.
- **Unsubscribe function**: `subscribe()` returns a callable that removes the callback. This avoids the need for callback identity tracking by callers.
- **Single PV granularity**: Subscriptions are per-PV, not per-ring or global. Fox registers one callback per PV per shared eval loop.

## DataSource protocol boundary

Fox defines a `DataSource` protocol (port) that abstracts data access. Vole implements it via `VoleDataSource`:

```python
class DataSource(Protocol):
    async def read_aligned(pvs, t0_ns, t1_ns, *, align, index, grid_dt_ns) -> AlignedWindow
    async def read_latest(pv) -> Sample | None
    async def read_binned(pvs, t0_ns, t1_ns, *, target_buckets) -> AlignedWindow
```

### Auto-provisioning logic

`VoleDataSource` intercepts `PVNotBuffered` errors and transparently provisions:

```
read_aligned(pvs=[...])
    │
    ├── Try BufferService.read_aligned()
    │       └── Raises PVNotBuffered for unknown PV
    │
    ├── For each missing PV:
    │   ├── Acquire per-PV lock (prevent race on concurrent provisions)
    │   ├── fetch_window(pv, t0_ns, t1_ns) --> list[Sample] from Mole
    │   ├── buffer_service.seed(pv, samples)
    │   └── subscribe_live([pv]) --> register with Hare
    │
    └── Retry BufferService.read_aligned()
```

Dependencies are injected at construction:

- `fetch_window` — callback to Mole gateway for historical data
- `fetch_binned` — callback for pre-aggregated binned data
- `subscribe_live` — callback to Hare for live subscription
- `clock` — injectable for testing (defaults to `time.time_ns`)

## Concurrency model

### Per-PV async locks

Every PV in `PVStore` has a dedicated `asyncio.Lock`. Operations that mutate a ring (`append`, `seed`, `drop`) acquire the lock for that PV. This allows concurrent operations on different PVs without contention.

### Alphabetical lock acquisition

Multi-PV reads (`read_aligned`) must acquire locks for all requested PVs. To prevent deadlocks, locks are always acquired in **alphabetical order** by PV name:

```python
sorted_pvs = sorted(pvs)
for pv in sorted_pvs:
    await store.lock_for(pv).acquire()
```

This guarantees a consistent lock ordering regardless of the order PVs appear in the request.

## HareIngestService

`HareIngestService` bridges Hare's `ClientChannel` protocol to Vole's buffer:

```
Hare PVBroker
    │
    └── send_json(frame) --> VoleChannel
            │
            ├── "initial" frame: bulk seed of current values
            └── "diff" frame: incremental updates
                    │
                    └── For each PV in frame:
                        ├── Strip ca:// / pva:// prefix
                        ├── Convert wire dict to Sample
                        └── buffer_service.append(pv, sample)
```

Key behaviors:

- Implements Hare's `ClientChannel` protocol via the inner `VoleChannel` class
- Normalizes PV names by stripping protocol prefixes (`ca://`, `pva://`)
- Converts Hare's `PVValue` wire format (dict with `value`, `timestamp`, `severity` fields) to Canopy's `Sample` domain object
- Gracefully degrades if Hare is not available (import wrapped in try/except)
