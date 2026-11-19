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

C-style boolean operators (`&&`, `||`, `!`) are normalized to Python equivalents before evaluation. The text of a string literal is never changed: `{state} == 'Fault!'` and `'A&&B'` compare as written, and a `{name}` inside quotes is text, not a token.

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
| Enum string | `label({x})` (see [`label()`](#label)) |
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

### `label()`

The value of a discrete channel is an index. `label({x})` gives the enum string
the control system holds for that index, so a formula compares on the meaning
and does not restate the index of every state:

```
label({valve}) == 'Open'
0 if label({valve}) == 'Open' else (1 if label({valve}) == 'Moving' else -1)
```

| Channel | `label({x})` |
|---|---|
| Value is a whole number that indexes the channel's enum strings | That string |
| Value is text | The text itself |
| No enum strings, or the index is outside them, or the value is not a whole number | `''` |
| Connection lost, or no value yet | The formula fails with `formula_input_missing`, as for `{x}` |

`label()` takes exactly one token, not a vector token and not an expression, and
is for `scalar` formulas. It reads the channel's value, so it follows the rule
of a plain token and not the rule of `severity()` and `connected()`. The result
of a formula can be the string itself (`label({valve})`).

The enum strings are read from the control system when the channel connects.
They are not re-read while it stays connected.

### Channels without a value

A plain token still fails the whole formula when its channel is disconnected or
has no data (`formula_input_missing`). A member of a vector token, and a token
that the expression reads only through `severity()` or `connected()`, is read
as follows:

| Channel | Value | `severity()` | `connected()` |
|---------|-------|--------------|---------------|
| Has a number | its number | 0 to 3 | 1 |
| Disconnected | `nan` | 3 | 0 |
| No sample yet | `nan` | 3 | 0 |
| Sample that is no number (text, also `"5"`; an array) | `nan` | as sampled | 1 |
| Empty sample (the channel answers, but with no value) | `nan` | 3 | 1 |

A member without a number never passes for a number:

- `severity()` and `connected()` do not read the value. `max(severity(...))`
  gives 3 as soon as one member is disconnected.
- `nanmax`, `nanmin`, `nansum` and `nanmean` leave such members out, and
  `count` does not look at the values.
- Every other use of a vector token's values fails with
  `formula_input_missing` while a member of that token has no number, and the
  message names those members. This holds for the plain reductions
  (`max({*})`), for comparisons (`count_nonzero({*} == 2)`, `max({*}) > 5`),
  for `count_nonzero({*})`, `where`, `clip` and `**`. The reason: a comparison
  with `nan` is false and `count_nonzero` counts `nan`, so the member would be
  taken for a value. A live subscription retries the error.
- A use is guarded by a `nan*` reduction only when nothing but `+ - * /` and
  the element-wise functions (`abs`, `sqrt`, `exp`, `log`, `log10`, `ln`, the
  trigonometric and hyperbolic functions, `floor`, `ceil`, `radians`,
  `degrees`) stands between the token and the reduction, and the reduction
  has that one argument. `nansum(abs({*}))` and `nanmax(({*} - 1) * 2)` are
  guarded; `nansum({*} > 1)` is not. The check is made on the text of the
  formula and is strict: a form it does not know is unguarded, also one that
  would be sound, such as `nanmax({*} ** 2)` or `nanmax(clip({*}, 0, 9))`.
  Write those with the accepted forms (`nanmax({*} * {*})`).
- When no member of a vector token has a number, a `nan*` reduction over it has
  nothing to reduce and does not answer: the formula fails with
  `formula_input_missing` naming the members, also when the reduction sits
  inside a comparison or a conditional (`nanmax({*}) > 5` is not false while
  nothing is known). With at least one member that has a number, the others
  are skipped. `count({*})` always answers.
- A channel whose own value is infinite is a number. A formula whose result is
  not finite because of it fails with `formula_input_missing` naming it.
- A result that is `nan` or infinite although every input value is finite
  (`log(min({*}))` with a 0) fails with `formula_type_error`. The result of a
  formula with a vector token or a status function is always a finite number,
  a boolean or text.
- The result's own `severity` is the worst over all inputs, and an input
  without a sample counts as `INVALID`, whatever the expression computes.
- When no input has a sample, the result's `tsNs` is the time of evaluation.

The message of the error tells the two causes apart. `PVs not available:
['A']` names channels that are disconnected or have no sample yet. `PVs without
a number: ['A']` names channels that are connected, but whose sample is no
number the formula can use (`nan`, infinite, text, an array, empty). When both
occur, the channels that are not available are named. The code is
`formula_input_missing` for both.

A roll-up that must always give a state uses `severity()`, `connected()` or the
`nan*` reductions. Any other expression over values has no meaning while a
member is missing, and says so.

A plain token whose sample is there but empty fails a formula with a vector
token or a status function in the same way; it is not read as 0.

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
| `missing` | Channels that are disconnected, have no sample, or have an empty sample, in member order. At most `VECTOR_MISSING_LISTED` names. |
| `more` | Number of further such channels that are not named |

The `formula_input_missing` message of a value reduction names the channels in
the same way: `PVs not available: ['A', 'B'] and 28 more`.

A token that is also read as a plain value (`severity({x}) + {x}`) follows the
rule of plain tokens.

### Limits and update rate

| Setting | Default | Meaning |
|---------|---------|---------|
| `CANOPY_FORMULA_ENGINE__MAX_VECTOR_INPUTS` | 1000 | Most channels the vector tokens of one formula may name. A formula above it is refused with `invalid_spec`, and the message names this limit. A live subscription is refused before any channel is subscribed. |
| `CANOPY_FORMULA_ENGINE__VECTOR_INTERVAL_MS` | 500 | Shortest time between two evaluations of a live formula with a vector token. Inside the start window the shortest time is 50 ms. |
| `CANOPY_FORMULA_ENGINE__VECTOR_START_WINDOW_MS` | 1000 | Longest time a live formula with a vector token or a status function holds its first result while inputs are pending (see below). 0 sends the first result as it is. |
| `CANOPY_FORMULA_ENGINE__VECTOR_READ_TIMEOUT_MS` | 2000 | One evaluation of such a formula reads its inputs together and waits this long for all of them. An input not read by then is without a value. |
| `CANOPY_FORMULA_ENGINE__VECTOR_MISSING_LISTED` | 20 | Most channels without a value that a result or an error message names. The others are counted. |

The limit on the expression length (`CANOPY_FORMULA_ENGINE__MAX_EXPRESSION_LEN`)
is not affected by the number of bindings.

A live formula with a vector token is evaluated when one of its members
changes, connects or disconnects, but at most once per
`VECTOR_INTERVAL_MS`; changes inside the interval are taken together. A
subscription that asks for a poll interval gets at least this interval.
Subscriptions with the same spec share one evaluation. The order in which the
bindings are written does not matter.

After a subscribe, the first values of the members arrive one after the other.
A member is pending while it has neither a first sample nor a known connection
failure. A live formula with a vector token, or with `severity()` or
`connected()` of a plain token, sends no result while such an input is
pending, so a display does not show the disconnected state for the moment it
takes to connect. The first result goes out when no member is pending, or when
the start window has passed. A member that is still pending then is read as
disconnected and named in `vectorInputs.missing`. After the first result
nothing is held.

Every member is one live channel on the server. A vector token removes the
limit on the expression, not the cost of the connections.

The members of a vector token, and a token read only through `severity()` or
`connected()`, are read from the live side only. A one-time evaluation
(`POST /evaluate`, or a subscription with a fixed window) does not connect
them and does not ask the archive for them. What the caller sees for a channel
the server holds no live value for (no subscription of any client has it
connected):

- under `severity()`: 3; under `connected()`: 0;
- the result's `severity` is `INVALID`, and `vectorInputs.missing` names it;
- an expression over its values fails with `formula_input_missing`,
  `PVs not available: [...]`.

So a one-time evaluation of a roll-up tells the state of the channels that are
live on the server at that moment, and names the others; it is no test that a
channel exists. A plain token (`{x}`, also through `label()`) is still read as
in a plain formula, archive included. Evaluate a roll-up through a live
subscription, which connects its members and holds its first result until
they have answered.

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
