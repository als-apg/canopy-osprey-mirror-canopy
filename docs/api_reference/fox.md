# Fox architecture

This page documents Fox's internal design decisions and evaluation pipeline for developers working on or extending the module.

## Why POST for evaluate

The evaluate endpoint uses POST rather than GET because:

- Formula expressions can be arbitrarily long and contain special characters (`{`, `}`, `**`, `||`)
- The `bindings` map and mode configuration form a structured request body
- GET query strings have practical length limits (~2000 characters) that expressions can exceed
- POST avoids URL-encoding complexity for mathematical expressions

## Why validate is separate

The validate endpoint exists independently from evaluate because:

- **No data retrieval** — validation only parses and typechecks the AST; it never touches Vole, Mole, or Hare
- **No computation** — the expression is never evaluated, so there's no timeout risk
- **UI use case** — formula-maker interfaces call validate on every keystroke for real-time error feedback; this must be cheap and fast

## Evaluation pipeline

```
FormulaSpec
    │
    ▼
Parser (formula_service.py --> parser.py)
    ├── Regex-extract {variable} tokens
    ├── Validate all tokens have bindings (and no extras)
    ├── Replace tokens with placeholders (__v0, __v1, ...)
    ├── Normalize booleans (&& --> and, || --> or, ! --> not)
    ├── ast.parse() in eval mode
    ├── Validate AST against ALLOWED_NODES + SAFE_FUNCS
    └── Detect numexpr compatibility
    │
    ▼
ParsedFormula {expression, tokens, placeholder_expr, is_numexpr_compatible}
    │
    ▼
Evaluator dispatch (by spec.kind)
    ├── ScalarEvaluator  --> read_latest per PV, eval once
    ├── TimeseriesEvaluator --> read_aligned window, eval over arrays
    └── WaveformEvaluator --> read_latest waveform arrays, eval element-wise
    │
    ▼
FormulaResult (tagged union: scalar | timeseries | waveform)
```

### Numexpr fast path

When a formula uses only functions and operators that `numexpr` supports, the timeseries evaluator delegates to `numexpr.evaluate()` instead of Python/NumPy eval. This provides significant speedups for large arrays. The parser detects compatibility by checking the AST against `NUMEXPR_FUNCS` and mapping function names (e.g., `asin` --> `arcsin`).

## WebSocket design

### Subscribe-per-formula model

Each WebSocket client can hold multiple independent subscriptions. A subscription is identified by a server-assigned UUID and tracks one formula spec with its time window and update mode.

### Shared eval loops

Subscriptions with identical parameters (`spec` + `t0Ns` + `intervalMs`) are deduplicated into a single `SharedEval`. The eval loop runs once and fans results out to all listeners. This prevents redundant computation when many dashboard panels watch the same formula.

A SHA-256 hash of the serialized parameters serves as the deduplication key.

### Fan-out

When a shared eval produces a result, it iterates over all listeners and sends the result to each. Dead listeners (send failures) are pruned from the list automatically.

### SubscriptionManager lifecycle

```
add_subscription()
    ├── t1_ns != 0? --> _one_shot(): eval once, send result + completed, done
    ├── intervalMs > 0? --> _poll_loop(): eval on timer
    └── else --> _eval_loop(): eval on PV change (push mode)

update_subscription()
    ├── Detach listener from old SharedEval
    ├── If old SharedEval has no listeners --> teardown (cancel task, unsub from Vole)
    └── Attach listener to new/existing SharedEval with updated params

remove_subscription() / remove_client()
    ├── Detach listener(s)
    └── Teardown orphaned SharedEvals
```

## Evaluator internals

### ScalarEvaluator

- Reads the latest sample from each bound PV via `read_latest()`
- Builds a namespace mapping placeholder names --> scalar values
- Evaluates the expression once with Python `eval()` in a restricted namespace
- Returns a single value with the most recent timestamp and worst-case severity

### TimeseriesEvaluator

- Reads aligned arrays for all bound PVs via `read_aligned()`
- Builds a namespace mapping placeholder names --> NumPy arrays
- Uses numexpr (if compatible) or NumPy eval for array computation
- Optionally decimates the output if it exceeds `max_points`
- Returns parallel arrays of timestamps, values, and severities

### WaveformEvaluator

- Reads the latest waveform (array-valued sample) from each PV
- Builds a namespace mapping placeholder names --> 1-D NumPy arrays
- Evaluates element-wise across the waveform
- Returns a single timestamped array result

## Security model

Fox enforces a strict sandbox on formula expressions:

### AST allowlist

Only these `ast` node types are permitted:

- Literals (numbers, strings, booleans, None)
- Arithmetic, comparison, and boolean operators
- Unary operators (`+`, `-`, `not`)
- Function calls (restricted to `SAFE_FUNCS`)
- Conditional expressions (ternary `if/else`)
- Tuples (for multi-arg functions like `atan2(x, y)`)
- Name references (variables and constants)

### Blocked constructs

- `import` / `from ... import`
- Lambda expressions
- List/set/dict comprehensions and generator expressions
- Subscript access (`x[0]`, `d["key"]`)
- Attribute access on anything other than `np` and `math`
- Any attribute starting with `__` (dunder blocking)

### Function allowlist (`SAFE_FUNCS`)

Only functions explicitly listed in `SAFE_FUNCS` (defined in `namespace.py`) can be called. Attempts to call unlisted functions produce a `FormulaUnsafeError`. The allowlist is intentionally conservative — it includes mathematical functions but excludes anything with side effects or filesystem/network access.

## Module reference

### Contracts

::: canopy.contracts.formula
    options:
      show_root_heading: true
      show_source: false
      members:
        - FormulaSpec
        - FormulaResult
        - FormulaValidationResult

### Gateway

::: canopy.fox.gateway
    options:
      show_root_heading: true
      show_source: false

### Services

#### FormulaService

::: canopy.fox.services.formula_service
    options:
      show_root_heading: true
      show_source: false
      members:
        - FormulaService

#### SubscriptionManager

::: canopy.fox.services.subscription_manager
    options:
      show_root_heading: true
      show_source: false
      members:
        - SubscriptionManager

#### Parser

::: canopy.fox.services.parser
    options:
      show_root_heading: true
      show_source: false
      members:
        - ParsedFormula
        - Parser

### Schemas

::: canopy.fox.schemas.formula
    options:
      show_root_heading: true
      show_source: false
