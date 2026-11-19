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
| Reductions that skip `nan` | `nanmean`, `nansum`, `nanmin`, `nanmax` |
| Size | `count` (number of elements) |
| Channel status | `severity({x})`, `connected({x})` (see [Formulas over many channels](#formulas-over-many-channels)) |
| Element-wise | `where(condition, x, y)`, `clip(value, low, high)` |

Functions use NumPy implementations internally. You can access `np.*` and `math.*` constants directly (e.g., `np.pi`), but only the allowlisted functions above are callable — other attributes like `math.factorial` are **not** exposed.

## Formulas over many channels

A `scalar` formula can read many channels through one short expression. Two
additions to the expression language make this possible: vector tokens, and the
status functions `severity()` and `connected()`. Both are for `scalar` formulas
only; a `timeseries`, `waveform` or `table` formula that uses them is a parse
error.

### Vector tokens

A vector token names several bindings at once:

| Token | Members |
|-------|---------|
| `{*}` | every binding of the formula |
| `{g.*}` | the bindings whose key starts with `g.` |

The members are ordered by binding key (string order), whatever the order the
bindings were written in. A vector token must be reduced to one value with a
reduction function:

```json
{
  "kind": "scalar",
  "expression": "max({*})",
  "bindings": {"0": "PUMP:1:CURRENT", "1": "PUMP:2:CURRENT", "2": "PUMP:3:CURRENT"}
}
```

Named groups keep sets of channels apart in one formula:

```json
{
  "kind": "scalar",
  "expression": "max(abs({set.*} - {read.*}))",
  "bindings": {
    "set.1": "PUMP:1:SETPOINT", "read.1": "PUMP:1:CURRENT",
    "set.2": "PUMP:2:SETPOINT", "read.2": "PUMP:2:CURRENT"
  }
}
```

Two groups with the same key suffixes pair up member by member, because both are
ordered by key.

Rules:

- A vector token counts as a use of each of its members. Every binding must
  still be used by some token, and a vector token that matches no binding is a
  parse error.
- A token that is itself a binding key is an ordinary token, not a vector token.
- The result must be one value. An expression that leaves a vector unreduced
  (`{*} + 1`) fails with `formula_type_error`.
- `and`, `or`, `not` and the ternary do not work element by element. Combine
  element-wise conditions with `*` (and) and `+` (or), or with `where(...)`:
  `count_nonzero(({*} > 5) * (severity({*}) < 2))`.

### `severity()` and `connected()`

| Function | Result |
|----------|--------|
| `severity({x})` | The channel's alarm severity as a number: 0 `NO_ALARM`, 1 `MINOR`, 2 `MAJOR`, 3 `INVALID` |
| `connected({x})` | 1 while the channel is connected and has a value, else 0 |

The argument is exactly one token, plain or vector. On a vector token the
function gives one number per member:

| Aggregate | Expression |
|-----------|------------|
| Worst state of all channels | `max(severity({*}))` |
| Number of channels in alarm | `count_nonzero(severity({*}) >= 1)` |
| Number of channels in a state | `count_nonzero({*} == 2)` |
| Number of connected channels | `sum(connected({*}))` |
| All connected (1 or 0) | `min(connected({*}))` |
| Any connected (1 or 0) | `max(connected({*}))` |
| Number of channels | `count({*})` |
| Largest, smallest, total | `max({*})`, `min({*})`, `sum({*})` |

### Channels without a value

A plain token still fails the whole formula when its channel is disconnected or
has no data (`formula_input_missing`). A member of a vector token, and a token
that the expression reads only through `severity()` or `connected()`, never
fails the formula. It is read as follows:

| Channel | Value | `severity()` | `connected()` |
|---------|-------|--------------|---------------|
| Has a sample | its number | 0 to 3 | 1 |
| Disconnected | `nan` | 3 | 0 |
| No sample yet | `nan` | 3 | 0 |
| Sample that is not a number (text, array) | `nan` | as sampled | 1 |

So that a channel without a value is always visible:

- `max(severity(...))` gives 3 as soon as one member is disconnected.
- `max`, `min`, `sum` and `mean` over values are `nan` while a member has no
  value. A formula whose result is `nan` for that reason fails with
  `formula_input_missing`, and the message names the members without a value.
  This is the error of a plain formula. A live subscription retries it.
- Use `nanmax`, `nanmin`, `nansum` or `nanmean` to leave such members out.
- Comparisons with `nan` are false. `count_nonzero({*} == 2)` does not count a
  member without a value, and `max({*}) > 5` is false, not an error.
- A result that is `nan` or infinite for another reason (a channel whose own
  value is `nan`) fails with `formula_type_error`. The result of a formula with
  a vector token or a status function is always a finite number, a boolean or
  text.
- The result's own `severity` is the worst over all inputs, and an input
  without a sample counts as `INVALID`, whatever the expression computes.
- When no input has a sample, the result's `tsNs` is the time of evaluation.

A roll-up that must always give a state uses `severity()`, `connected()` or the
`nan*` reductions. A plain reduction over values (`max({*})`) is for a number
that has no meaning while a member is missing.

### The result of a formula with a vector token

The members of a vector token are not listed in `inputsUsed`. That map holds
only the plain tokens of the formula. The members are summed up in
`vectorInputs`, which only a formula with a vector token has:

```json
{
  "tsNs": 1700000000000000000,
  "value": 3,
  "severity": "INVALID",
  "inputsUsed": {},
  "vectorInputs": {"count": 240, "missing": ["PUMP:17:CURRENT"], "more": 0}
}
```

| Field | Meaning |
|-------|---------|
| `count` | Number of channels the vector tokens name |
| `missing` | Channels that are disconnected or have no sample, in member order. At most `VECTOR_MISSING_LISTED` names. |
| `more` | Number of further such channels that are not named |

The `formula_input_missing` message of a value reduction names the channels in
the same way: `PVs not available: ['A', 'B'] and 28 more`.

A token that is also read as a plain value (`severity({x}) + {x}`) follows the
rule of plain tokens.

### Limits and update rate

| Setting | Default | Meaning |
|---------|---------|---------|
| `CANOPY_FORMULA_ENGINE__MAX_VECTOR_INPUTS` | 1000 | Most channels the vector tokens of one formula may name. A formula above it is refused with `invalid_spec`, and the message names this limit. A live subscription is refused before any channel is subscribed. |
| `CANOPY_FORMULA_ENGINE__VECTOR_INTERVAL_MS` | 500 | Shortest time between two evaluations of a live formula with a vector token. In the first second after the subscribe the shortest time is 50 ms. |
| `CANOPY_FORMULA_ENGINE__VECTOR_MISSING_LISTED` | 20 | Most channels without a value that a result or an error message names. The others are counted. |

The limit on the expression length (`CANOPY_FORMULA_ENGINE__MAX_EXPRESSION_LEN`)
is not affected by the number of bindings.

A live formula with a vector token is evaluated when one of its members
changes, connects or disconnects, but at most once per
`VECTOR_INTERVAL_MS`; changes inside the interval are taken together. A
subscription that asks for a poll interval gets at least this interval.
Subscriptions with the same spec share one evaluation. The order in which the
bindings are written does not matter.

Every member is one live channel on the server. A vector token removes the
limit on the expression, not the cost of the connections.

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
| `invalid_spec` | 400 | A server-side limit is exceeded, e.g. more vector inputs than `MAX_VECTOR_INPUTS` |

All errors return JSON with `code` and `detail` fields:

```json
{
  "code": "formula_parse_error",
  "detail": "Unbound variable: {temp}"
}
```
