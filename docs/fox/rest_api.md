# Fox REST API

Fox provides two REST endpoints for formula evaluation and validation. Both require read access (`X-API-Key` header with a key that has the `read` scope).

## POST /fox/v1/evaluate

Evaluates a formula over a time window and returns the computed result.

### Request

```json
{
  "spec": {
    "kind": "timeseries",
    "expression": "{a} + {b}",
    "bindings": {
      "a": "PV:SENSOR:A",
      "b": "PV:SENSOR:B"
    },
    "mode": "raw",
    "align": "last",
    "index": "union",
    "gridDtNs": null,
    "maxPoints": null
  },
  "t0Ns": 1700000000000000000,
  "t1Ns": 1700003600000000000
}
```

### FormulaSpec fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | `"scalar"` \| `"timeseries"` \| `"waveform"` | required | Determines evaluation strategy and result shape |
| `expression` | string | required | Formula with `{variable}` tokens |
| `bindings` | object | required | Map of variable name --> PV name |
| `mode` | `"raw"` \| `"binned"` | `"raw"` | Whether to use raw samples or binned averages |
| `align` | `"last"` \| `"linear"` \| `"nearest"` \| `"none"` | `"last"` | How to handle time misalignment between inputs |
| `index` | `"union"` \| `"intersection"` \| `"grid"` \| `"first_input"` | `"union"` | Strategy for combining multiple time series |
| `gridDtNs` | int \| null | null | Grid spacing in nanoseconds (required when `index` is `"grid"`) |
| `maxPoints` | int \| null | null | Maximum number of output points (triggers decimation) |

### Time parameters

| Field | Type | Description |
|-------|------|-------------|
| `t0Ns` | int | Window start in epoch nanoseconds |
| `t1Ns` | int | Window end in epoch nanoseconds. Use `0` for "now" |

### Response — scalar result

Returned when `kind` is `"scalar"`:

```json
{
  "kind": "scalar",
  "tsNs": 1700003600000000000,
  "value": 42.5,
  "severity": "NO_ALARM",
  "inputsUsed": {
    "a": 1,
    "b": 1
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"scalar"` | Discriminator |
| `tsNs` | int | Timestamp of the result |
| `value` | float \| int \| bool \| string | Computed value |
| `severity` | string | Alarm severity (`NO_ALARM`, `MINOR`, `MAJOR`, `INVALID`) |
| `inputsUsed` | object | Map of binding name --> sample count used |

### Response — timeseries result

Returned when `kind` is `"timeseries"`:

```json
{
  "kind": "timeseries",
  "tsNs": [
    1700000000000000000,
    1700000001000000000,
    1700000002000000000
  ],
  "values": [
    1.0,
    2.5,
    null
  ],
  "severity": [
    "NO_ALARM",
    "NO_ALARM",
    "INVALID"
  ],
  "inputs": {
    "a": "PV:SENSOR:A",
    "b": "PV:SENSOR:B"
  },
  "decimated": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"timeseries"` | Discriminator |
| `tsNs` | list[int] | Timestamps in nanoseconds |
| `values` | list[float \| null] | Computed values (`null` for gaps) |
| `severity` | list[string] | Per-point alarm severity |
| `inputs` | object | Binding name --> PV name map |
| `decimated` | bool | `true` if output was downsampled to fit `maxPoints` |

### Response — waveform result

Returned when `kind` is `"waveform"`:

```json
{
  "kind": "waveform",
  "tsNs": 1700003600000000000,
  "values": [
    0.1,
    0.2,
    0.3,
    0.4,
    0.5
  ],
  "severity": "NO_ALARM"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"waveform"` | Discriminator |
| `tsNs` | int | Timestamp of the waveform |
| `values` | list[float] | Dense array of values |
| `severity` | string | Overall alarm severity |

## POST /fox/v1/validate

Parses and typechecks a formula without evaluating it. Use this for real-time UI validation as users type formulas — it performs no data retrieval or computation.

### Request

The request body is a `FormulaSpec` (same shape as the `spec` field in the evaluate request):

```json
{
  "kind": "timeseries",
  "expression": "{temp} * 1.8 + 32",
  "bindings": {
    "temp": "ROOM:TEMP:READING"
  }
}
```

### Response

```json
{
  "valid": true,
  "tokens": [
    "temp"
  ],
  "errors": []
}
```

| Field | Type | Description |
|-------|------|-------------|
| `valid` | bool | Whether the formula parsed and typechecked successfully |
| `tokens` | list[string] | Extracted `{variable}` token names |
| `errors` | list[string] | Parse or validation error messages (empty if valid) |

### Example — invalid formula

```json
// Request
{
  "kind": "scalar",
  "expression": "{x} + import os",
  "bindings": {
    "x": "PV:A"
  }
}

// Response
{
  "valid": false,
  "tokens": [
    "x"
  ],
  "errors": [
    "Disallowed AST node: Import"
  ]
}
```
