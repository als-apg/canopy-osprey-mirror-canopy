# Hare REST API

Three endpoints under `/hare/v1/pvs`. Reads require a key with the `read`
scope, writes require `write` (sent in the `X-API-Key` header). PV names may
carry a `ca://` or `pva://` prefix — see [PV names](index.md#pv-names).

## GET /hare/v1/pvs/{pv}

Current value of a PV. Returns the cached value if the PV is actively
monitored (some client is subscribed to it), otherwise issues a one-shot read
through the connector.

```bash
curl -H "X-API-Key: dev" http://localhost:8000/hare/v1/pvs/TEST:SINE
```

```json
{
  "pv": "TEST:SINE",
  "value": 3.27,
  "valueKind": "scalar",
  "severity": "NO_ALARM",
  "tsNs": 1700000000123456789,
  "connected": true,
  "error": null
}
```

| Field | Type | Description |
|-------|------|-------------|
| `pv` | string | The PV name as requested |
| `value` | varies | Payload per `valueKind` — number, string, enum index, array, or `{labels, columns}` table. `null` when disconnected |
| `valueKind` | string | How to interpret `value` — see [values and metadata](index.md#values-and-metadata) |
| `severity` | string | Alarm severity: `NO_ALARM`, `MINOR`, `MAJOR`, `INVALID` |
| `tsNs` | int | IOC timestamp in epoch nanoseconds |
| `connected` | bool | `false` when the channel is disconnected |
| `error` | string \| null | Connector error detail, when there is one |

## GET /hare/v1/pvs/{pv}/describe

Static metadata for a PV — the fields a client reads once to configure a
display. Served from the broker's cache when the PV is actively monitored,
otherwise fetched on demand.

```bash
curl -H "X-API-Key: dev" http://localhost:8000/hare/v1/pvs/TEST:SINE/describe
```

```json
{
  "pv": "TEST:SINE",
  "valueKind": "scalar",
  "units": "degC",
  "precision": 2,
  "description": null,
  "enumChoices": null,
  "displayLow": -10.0,
  "displayHigh": 10.0,
  "controlLow": -10.0,
  "controlHigh": 10.0,
  "warningLow": -5.0,
  "warningHigh": 5.0,
  "alarmLow": -8.0,
  "alarmHigh": 8.0,
  "writable": true,
  "form": null
}
```

All fields except `pv` and `valueKind` are optional — whatever the backend
cannot supply stays `null`.

| Field | Description |
|-------|-------------|
| `units` / `precision` | Display units and decimal places |
| `description` | Channel description, when the backend provides one |
| `enumChoices` | Labels for enum PVs, indexed by the integer `value` |
| `displayLow` / `displayHigh` | Suggested display (view) range |
| `controlLow` / `controlHigh` | Writable (control) range |
| `warningLow` / `warningHigh`, `alarmLow` / `alarmHigh` | Alarm thresholds |
| `writable` | Whether the channel accepts writes |
| `form` | Display format hint (e.g. `"Decimal"`, `"Hex"`), from PVA |

## PUT /hare/v1/pvs/{pv}

Write a value to a PV. Requires the `write` scope.

```bash
curl -X PUT -H "X-API-Key: dev" -H "Content-Type: application/json" \
  -d '{"value": 42.0}' \
  http://localhost:8000/hare/v1/pvs/TEST:SETPOINT
```

### Request body

```json
{
  "value": 42.0,
  "wait": true,
  "timeout": 5.0
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `value` | number \| bool \| string \| array of scalars | required | The value to write |
| `wait` | bool | `true` | Block until the IOC processes the write (put-with-callback). With `wait: true`, a `200` means the value landed. `false` submits without waiting for record processing |
| `timeout` | number \| null | `null` | Per-write timeout in seconds (must be positive); `null` uses the connector default |

### Response

```json
{
  "pv": "TEST:SETPOINT"
}
```

A non-2xx status signals failure — a rejected or timed-out write returns
`502 pv_write_failed`.

## Error codes

| Code | HTTP | Meaning |
|------|------|---------|
| `invalid_api_key` | 401 | Missing or unknown API key |
| `insufficient_read_access` / `insufficient_write_access` | 403 | Key lacks the required scope |
| `invalid_pv_name` | 400 | Empty, over-long, or malformed PV name |
| `pv_connection_failed` | 503 | Channel could not be connected |
| `pv_read_timeout` | 504 | No response from the IOC within the timeout |
| `pv_write_failed` | 502 | Write rejected or not confirmed by the IOC |
| `epics_backend_unavailable` | 503 | The connector backend is not running |

All errors use the standard envelope: `{"code": "...", "detail": "..."}`.
