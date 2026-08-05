# Vole REST API

Vole exposes buffered PV data via HTTP for client backfill on subscribe/reconnect.

!!! note
    Only PVs that are currently being buffered (i.e., subscribed via Hare) will
    return data. A never-before-seen PV will return 404.

All endpoints require a valid API key with `read` scope via the `X-API-Key` header.

## Single-PV Read

```
GET /vole/v1/pvs/{pv}
```

Returns buffered samples for a single PV within a time window.

### Parameters

| Parameter   | In    | Type   | Required | Description |
|-------------|-------|--------|----------|-------------|
| `pv`        | path  | string | yes      | PV name (URL-encode special characters) |
| `from`      | query | string | yes      | Start of window — ISO-8601 or epoch nanoseconds |
| `to`        | query | string | no       | End of window — ISO-8601 or epoch nanoseconds; omit for "now" |
| `align`     | query | string | no       | `"last"` (default) pins a pre-window sample at t0; `"none"` returns raw slice |
| `maxPoints` | query | int    | no       | Decimation cap — if the result exceeds this, it is uniformly downsampled |

### Response

```json
{
  "pv": "QUAD:LI21:301:BDES",
  "count": 3,
  "samples": [
    {
      "tsNs": 1719388800000000000,
      "value": 3.14,
      "severity": "NO_ALARM",
      "source": "live"
    }
  ]
}
```

### Errors

| Status | Code | Condition |
|--------|------|-----------|
| 400 | `bad_request` | Missing `from`, or `from` > `to` |
| 404 | `pv_not_buffered` | PV is not currently buffered |

---

## Multi-PV Aligned Read

```
GET /vole/v1/pvs/
```

Returns multiple PVs aligned to a shared time index using Vole's alignment service.

### Parameters

| Parameter  | In    | Type   | Required | Description |
|------------|-------|--------|----------|-------------|
| `pvs`      | query | string | yes      | Comma-separated PV names |
| `from`     | query | string | yes      | Start of window — ISO-8601 or epoch nanoseconds |
| `to`       | query | string | no       | End of window — ISO-8601 or epoch nanoseconds; omit for "now" |
| `align`    | query | string | no       | Alignment mode: `"last"` (default), `"nearest"`, `"linear"`, `"none"` |
| `index`    | query | string | no       | Index mode: `"union"` (default), `"intersection"`, `"grid"`, `"first_input"` |
| `gridDtNs` | query | int    | conditional | Grid spacing in nanoseconds — required when `index="grid"` |

### Alignment modes

| Mode | Behavior |
|------|----------|
| `last` | Forward-fill: most recent sample at or before each index point |
| `nearest` | Closest sample by absolute time difference |
| `linear` | Linear interpolation between bracketing samples |
| `none` | Only exact timestamp matches produce values |

### Index modes

| Mode | Behavior |
|------|----------|
| `union` | All unique timestamps across all PVs |
| `intersection` | Only timestamps present in all PVs |
| `grid` | Regular time grid with `gridDtNs` spacing |
| `first_input` | Use timestamps from the first PV in the list |

### Response

```json
{
  "tsNs": [1719388800000000000, 1719388801000000000],
  "pvs": [
    {
      "pv": "QUAD:LI21:301:BDES",
      "values": [3.14, 3.15],
      "severities": [0, 0]
    },
    {
      "pv": "QUAD:LI21:301:BACT",
      "values": [3.13, null],
      "severities": [0, 1]
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `tsNs` | Shared time index (epoch nanoseconds) |
| `values` | Per-PV values aligned to the index; `null` where no value is available |
| `severities` | Alarm severity ordinals: 0 = NO_ALARM, 1 = MINOR, 2 = MAJOR, 3 = INVALID |

### Errors

| Status | Code | Condition |
|--------|------|-----------|
| 400 | `bad_request` | Empty `pvs`, missing `from`, `from` > `to`, or `index="grid"` without `gridDtNs` |
| 404 | `pv_not_buffered` | Any requested PV is not currently buffered |

---

## Subscribe (Multi-PV)

```
POST /vole/v1/pvs/subscribe
```

Tells Vole to start buffering the given PVs. This is the REST alternative to opening a
Hare WebSocket — useful for dashboards or batch readers that want warm buffers without
maintaining a persistent connection.

### Request body

```json
{
  "pvs": ["QUAD:LI21:301:BDES", "QUAD:LI21:301:BACT"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `pvs` | array of strings | yes | PV names to subscribe (at least one) |

### Response

```json
{
  "subscribed": ["QUAD:LI21:301:BDES", "QUAD:LI21:301:BACT"]
}
```

`subscribed` lists what Vole is actually buffering. A name dropped by Hare's
subscription caps is absent from it, so compare against the request rather than
assuming every PV was accepted.

### Errors

| Status | Code | Condition |
|--------|------|-----------|
| 400 | `bad_request` | Empty `pvs` list |
| 400 | `invalid_pv_name` | Any PV name is empty, over-long, or contains whitespace / control characters |
| 401 | `unauthorized` | Missing or invalid API key |
| 503 | `ingest_unavailable` | Live ingestion is not wired up (Hare not connected) |

---

## Subscribe (Single-PV)

```
POST /vole/v1/pvs/subscribe/{pv}
```

Convenience endpoint that subscribes a single PV by path parameter.

### Parameters

| Parameter | In   | Type   | Required | Description |
|-----------|------|--------|----------|-------------|
| `pv`      | path | string | yes      | PV name (URL-encode special characters) |

### Response

```json
{
  "subscribed": ["QUAD:LI21:301:BDES"]
}
```

`subscribed` is empty if Hare declined the PV (e.g. its subscription cap is reached).

### Errors

| Status | Code | Condition |
|--------|------|-----------|
| 400 | `invalid_pv_name` | The PV name is empty, over-long, or contains whitespace / control characters |
| 401 | `unauthorized` | Missing or invalid API key |
| 503 | `ingest_unavailable` | Live ingestion is not wired up (Hare not connected) |

---

## Subscription lifecycle

When a PV is subscribed via the endpoints above, Vole registers with Hare for live
updates and begins buffering data. The buffer remains active as long as at least one
external Hare client is subscribed to the PV.

When all external subscribers disconnect, a **grace period** begins (default: 60 seconds,
configurable via `CANOPY_VOLE__BUFFER_GRACE_SECONDS`). If a new subscriber arrives before
the timer expires, the timer is cancelled and the buffer stays warm. If the grace period
elapses with no reconnection, Vole unsubscribes from Hare and drops the ring buffer.

This provides tolerance for page refreshes and brief disconnections without leaking
stale buffers indefinitely.
