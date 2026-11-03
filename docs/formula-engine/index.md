# Formula Engine

The Formula Engine evaluates mathematical formulas over live and historical PV (Process Variable) data. It supports one-shot REST evaluation, real-time WebSocket subscriptions that re-evaluate as data changes, and a validation endpoint for UI-driven formula editing.

## Expression syntax

Formulas reference PV data through **tokens** — variable names wrapped in curly braces:

```
{temperature} * 1.8 + 32
```

Each token must be bound to a PV name in the request's `bindings` field:

```json
{
  "expression": "{temperature} * 1.8 + 32",
  "bindings": {
    "temperature": "ROOM:TEMP:READING"
  }
}
```

Token names (e.g. `temperature`) can contain letters, digits, and underscores. Every token in the expression must have a binding, and every binding must appear in the expression — mismatches produce a parse error.

PV names in bindings may optionally include `ca://` or `pva://` protocol prefixes — these are stripped internally when resolving data, so `ca://ROOM:TEMP:READING` and `ROOM:TEMP:READING` are equivalent. You do not need to include the prefix, but it won't cause an error if present.

## Operators

| Category | Operators |
|----------|-----------|
| Arithmetic | `+`, `-`, `*`, `/`, `//` (floor div), `%` (modulo), `**` (power) |
| Comparison | `==`, `!=`, `<`, `<=`, `>`, `>=` |
| Boolean | `and`, `or`, `not` (also C-style: `&&`, `||`, `!`) |
| Unary | `+`, `-`, `not` / `!` |
| Ternary | `x if condition else y` |

C-style boolean operators (`&&`, `||`, `!`) are normalized to Python equivalents before evaluation.

## Available functions

All functions operate element-wise on arrays (timeseries data) and accept scalars.

| Category | Functions |
|----------|-----------|
| Trigonometric | `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `sinh`, `cosh`, `tanh` |
| Exponential / Logarithmic | `exp`, `log`, `log10`, `ln`, `sqrt`, `pow`, `abs` |
| Rounding | `floor`, `ceil` |
| Angle conversion | `radians`, `degrees` |
| Reductions | `mean`, `sum`, `min`, `max`, `std`, `var`, `ptp`, `count_nonzero` |
| Element-wise | `where(condition, x, y)`, `clip(value, low, high)` |

Functions use NumPy implementations internally. You can access `np.*` and `math.*` constants directly (e.g., `np.pi`), but only the allowlisted functions above are callable — other attributes like `math.factorial` are **not** exposed.

## Constants

| Constant | Value |
|----------|-------|
| `pi` | 3.14159... |
| `e` | 2.71828... |
| `inf` | Positive infinity |
| `nan` | Not a number |
| `True` | Boolean true |
| `False` | Boolean false |
| `None` | Null / missing |

## Formula kinds

The `kind` field in a formula spec determines how inputs are treated and what result shape is returned:

| Kind | Input treatment | Result |
|------|----------------|--------|
| `scalar` | Uses the latest sample from each PV | Single value + timestamp |
| `timeseries` | Uses all samples in the time window | Array of timestamps + values |
| `waveform` | Uses the latest waveform array from each PV | Single array of values |

## Alignment and index modes

When a formula references multiple PVs, their time series may not share the same timestamps. Two parameters control how they are combined:

### Alignment mode (`align`)

Determines how to produce a value at a timestamp where a PV has no exact sample.

| Mode | Behavior |
|------|----------|
| `last` (default) | Forward-fill: use the most recent sample at or before the target time |
| `linear` | Linear interpolation between the two bracketing samples |
| `nearest` | Use the closest sample by absolute time difference |
| `none` | Only exact timestamp matches produce values; gaps are `None` |

### Index mode (`index`)

Determines which set of timestamps to evaluate the formula at.

| Mode | Behavior |
|------|----------|
| `union` (default) | All unique timestamps from all input PVs |
| `intersection` | Only timestamps present in every input PV |
| `grid` | Regular cadence at `gridDtNs` intervals (requires `gridDtNs` > 0) |
| `first_input` | Use the first PV's timestamps as the output index |

## Error codes

| Error | HTTP | Meaning |
|-------|------|---------|
| `formula_parse_error` | 400 | Syntax error, missing/unused bindings, nested braces |
| `formula_unsafe` | 400 | Disallowed operations (imports, lambdas, unsafe attributes) |
| `formula_input_missing` | 422 | Referenced PV not found or has no data |
| `formula_type_error` | 422 | Type mismatch during evaluation |
| `formula_dimension_mismatch` | 422 | Array shape incompatibility between inputs |
| `formula_eval_timeout` | 504 | Computation exceeded time limit |
| `formula_circular_reference` | 400 | Cyclic dependency detected |

All errors return JSON with `code` and `detail` fields:

```json
{
  "code": "formula_parse_error",
  "detail": "Unbound variable: {temp}"
}
```
