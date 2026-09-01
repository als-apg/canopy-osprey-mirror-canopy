# Vole configuration

Vole is configured through environment variables with the prefix `CANOPY_VOLE__` (double underscore as the nested delimiter). All settings have sensible defaults and can be overridden in your `.env` file or deployment environment.

## Environment variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `CANOPY_VOLE__MAX_POINTS` | int | `500000` | Maximum samples per PV ring buffer. Older samples are dropped (FIFO) when this limit is reached. |
| `CANOPY_VOLE__GLOBAL_BYTES_MAX` | int | `4294967296` (4 GB) | Global memory ceiling in bytes. When exceeded, LRU eviction removes entire rings until under budget. |
| `CANOPY_VOLE__DEFAULT_ALIGN` | string | `"last"` | Default alignment mode for time-window reads. Options: `last`, `linear`, `nearest`, `none`. |
| `CANOPY_VOLE__DEFAULT_INDEX` | string | `"union"` | Default index mode for multi-PV reads. Options: `union`, `intersection`, `grid`, `first_input`. |
| `CANOPY_VOLE__BUFFER_GRACE_SECONDS` | int | `60` | Seconds to wait after the last external subscriber disconnects before dropping a PV's buffer. |

## Example .env

```bash
CANOPY_VOLE__MAX_POINTS=1000000
CANOPY_VOLE__GLOBAL_BYTES_MAX=8589934592
CANOPY_VOLE__DEFAULT_ALIGN=linear
CANOPY_VOLE__DEFAULT_INDEX=union
CANOPY_VOLE__BUFFER_GRACE_SECONDS=120
```

## Tuning guidance

### Per-PV ring size (`MAX_POINTS`)

The ring size determines how much history is available for formula evaluation without fetching from Mole:

- **500,000 (default)** — ~11.5 hours at 12 Hz sample rate, good for most display use cases
- **1,000,000** — ~23 hours at 12 Hz, suitable for longer trend views
- **10,000** — minimal footprint for PVs only used in scalar (latest-value) formulas

Individual PVs can be seeded with a custom `max_points` override, but the default applies to auto-provisioned rings.

### Global memory ceiling (`GLOBAL_BYTES_MAX`)

The ceiling bounds total memory usage across all buffered PVs. Each sample uses approximately 80 bytes.

| Ceiling | Approx. capacity at default ring size |
|---------|---------------------------------------|
| 4 GB (default) | ~100 PVs at 500k samples each |
| 8 GB | ~200 PVs at 500k samples each |
| 16 GB | ~400 PVs at 500k samples each |

If your deployment monitors many PVs simultaneously, increase this ceiling or reduce `MAX_POINTS` per ring.

## Memory pressure behavior

When total buffer memory exceeds `GLOBAL_BYTES_MAX`:

1. **Detection** — Checked after every `append` and `seed` operation
2. **Eviction** — Rings are sorted by last access time (read or write); the least-recently-accessed ring is evicted first
3. **Cleanup** — Evicted PVs are unsubscribed from Hare live updates
4. **Recovery** — If a formula later references an evicted PV, auto-provisioning re-creates the ring (seeds from Mole, re-subscribes to Hare)

Eviction removes entire rings, not individual samples. A single eviction pass continues until total memory drops below the ceiling.

If memory pressure persists after eviction (e.g., a single ring exceeds the ceiling), the `append` operation raises `BufferAtCapacity`. In practice this only occurs if `GLOBAL_BYTES_MAX` is set lower than one full ring's memory usage.
