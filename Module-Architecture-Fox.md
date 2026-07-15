Fox is the formula engine. It evaluates user-defined expressions over PV samples — three flavors: scalar formulas (one number out per evaluation), trace/curve formulas (a derived time-series, the workhorse for plots), and waveform formulas (array math on a single timestamped vector). Fox holds no state of its own; every call is a pure function of `(expression, inputs, time-window)`. The state that *does* exist for live formula curves — the derived ring of values that grows as inputs grow — lives in Vole, alongside the input PV rings. Fox writes into those derived rings; Vole owns their lifecycle.

If `canopy-api` restarts, Fox is empty until a caller asks it to evaluate something. It needs no warm-up.

> Status: this page is a *design proposal*. Issue #3 is the tracking ticket. Open questions are concentrated in §13. Where this page contradicts the older `canopy-handoff.md` or the Component Inventory, this page is the newer source.

## 1. Position in the call graph

```
                         Owl  ─── (formula text, bindings) ─────────┐
                         Hare ─── (one-shot eval from Wren?) ──┐    │
                         Notebooks / scripts ─── HTTP ─────────┤    │
                                                               ▼    ▼
                            Vole  ◄────── reads aligned ──────  Fox
                              ▲             windows               │
                              │                                   │
                              └────── writes derived samples ─────┘
                                       (live formula curve tailing)
```

- **Callers** (in-process via `fox.gateway`): Owl (renders saved plots), Hare (Wren may delegate scalar formulas), Squirrel (delta-vs-snapshot uses formulas).
- **Callers** (HTTP, `/fox/v1/...`): notebooks, ad-hoc scripts, the frontend Owl tool for one-shot previews.
- **Callees**: Vole (the only data source), Mole indirectly via Vole's `PVNotBuffered` retry path.
- **Not callers**: Fox does not call Hare, Mole, or the EPICS Connector directly. All input data comes from `vole.gateway.read_aligned`.

Fox has no event-bus subscriptions of its own. The "live formula tailing" path is driven by Vole: when an input ring gets a new sample, Vole's writer hooks call Fox to extend the derived ring.

## 2. Boundaries — what Fox owns vs. delegates

| Concern | Fox owns | Fox delegates to |
|---|---|---|
| Expression parsing + AST safety check | yes | — |
| Expression evaluation (scalar / array) | yes | numexpr, asteval (TBD §5) |
| Resolving variable tokens to PV names | yes | — |
| Choosing alignment + index strategy per call | yes (defaults), exposed to caller | Vole applies it |
| Output dtype inference + error envelope shaping | yes | — |
| Input PV time-aligned buffering | — | Vole |
| Live PV connection / monitor | — | Hare |
| Historical fetch | — | Mole (via Vole's seed retry path) |
| Persistence of formula definitions | — | Owl (formula belongs to a saved plot config) |
| Stateful storage of derived live curves | — | Vole (derived ring) |
| Long-running CSV / MATLAB export of formula curves | — | Jobs (Arq), eventually |

The discipline: **Fox is compute, Vole is state, Owl is persistence.** Anything that smells like "remember this formula across calls" goes to Owl. Anything that smells like "remember the values you just computed" goes to Vole.

## 3. Internal structure

```
src/canopy/fox/
  app.py                       # sub-FastAPI: v1 router + canopy error handler
  runtime.py                   # uptime_s() for /health/watchdog
  api/v1/
    router.py                  # APIRouter(prefix="/v1")
    evaluate.py                # POST /evaluate           (one-shot scalar | curve | waveform)
    validate.py                # POST /validate          (parse+typecheck without evaluating)
    health.py                  # GET  /health/{summary,watchdog}
  schemas/
    formula.py                 # FormulaSpecDTO, NewFormulaEvalDTO, FormulaResultDTO,
                               # ScalarResultDTO, CurveResultDTO, WaveformResultDTO,
                               # FormulaValidationDTO
  services/
    formula_service.py         # FormulaService — orchestration, the gateway calls into this
    parser.py                  # expression -> AST + variable tokens; validation
    evaluator.py               # AST -> numexpr/asteval dispatch + namespace
    curve_evaluator.py         # curve-formula path: pulls aligned windows from Vole
    waveform_evaluator.py      # waveform-formula path: numpy ops on a single Sample.value
    scalar_evaluator.py        # scalar path: latest-value lookup per input + eval
    # uniform-time output decimation lives in canopy.core.decimation (shared with Mole)
  language/
    grammar.py                 # registry of allowed AST node types + functions
    namespace.py               # the function/constants namespace exposed to expressions
  config.py                    # FoxSettings — eval timeout, max output points, allowed funcs
  events.py                    # subscribes nothing; publishes nothing in v1
  gateway.py                   # public surface (in-process)
  dependencies.py              # FormulaServiceDep
```

The same shape as Mole and Vole. There is no `data/` or `adapters/` tier because Fox's only "external" call is to `vole.gateway`, which is an in-process import.

## 4. Data model

```python
# canopy.contracts.formula  (shared so Owl/Squirrel/notebooks all speak the same shape)

FormulaKind = Literal["scalar", "curve", "waveform"]

class FormulaSpec(CanopyModel):
    """A complete formula request. Owl stores these in its plot configs; Fox
    accepts them as input. The same shape is used at the wire and at the
    gateway."""
    kind: FormulaKind
    expression: str                       # e.g. "{a} + sqrt({b})"
    bindings: dict[str, str]              # {"a": "PV:NAME:ONE", "b": "PV:NAME:TWO"}
    align: AlignmentMode = "last"         # passed through to vole.read_aligned
    index: IndexMode = "union"            # ditto
    grid_dt_ns: int | None = None         # required when index="grid"
    max_points: int | None = None         # output decimation hint, applied after eval

class FormulaResult(CanopyModel):
    """Tagged union by `kind`. Frontend never has to guess which shape it got."""
    kind: FormulaKind
    # Exactly one of:
    scalar: ScalarResult | None = None
    curve:  CurveResult  | None = None
    waveform: WaveformResult | None = None

class ScalarResult(CanopyModel):
    ts_ns: int
    value: float | int | bool
    severity: AlarmSeverity                # worst severity across input snapshots
    inputs_used: dict[str, int]            # {pv: ts_ns of the sample read} — for traceability

class CurveResult(CanopyModel):
    ts_ns: list[int]                       # common time index, length N
    values: list[float | None]             # length N; None where any input was missing
    severity: list[AlarmSeverity]          # length N; worst across inputs at that ts
    inputs: dict[str, str]                 # {var_name: pv_name} echoed back
    decimated: bool                        # true if max_points triggered

class WaveformResult(CanopyModel):
    ts_ns: int                             # the timestamp the inputs were aligned to
    values: list[float]                    # the result vector
    severity: AlarmSeverity
```

Fox-specific errors all inherit `CanopyError`. Slugs go in `ErrorCode`:

```python
class FoxError(CanopyError): ...

class FormulaParseError(FoxError):
    status_code = 400; error_code = ErrorCode.FORMULA_PARSE_ERROR
class FormulaUnsafeError(FoxError):
    status_code = 400; error_code = ErrorCode.FORMULA_UNSAFE
class FormulaInputMissing(FoxError):
    status_code = 422; error_code = ErrorCode.FORMULA_INPUT_MISSING
class FormulaTypeError(FoxError):
    status_code = 422; error_code = ErrorCode.FORMULA_TYPE_ERROR
class FormulaDimensionMismatch(FoxError):
    status_code = 422; error_code = ErrorCode.FORMULA_DIMENSION_MISMATCH
class FormulaEvalTimeout(FoxError):
    status_code = 504; error_code = ErrorCode.FORMULA_EVAL_TIMEOUT
class FormulaCircularReference(FoxError):
    status_code = 400; error_code = ErrorCode.FORMULA_CIRCULAR_REFERENCE
```

## 5. Expression language and evaluator

### 5.1 Token syntax

Adopts trace's **`{name}`** convention exclusively. Bare PV names in expressions are not accepted in v1. Reasons: trace's syntax is what users already write; it survives URL-encoding and JSON cleanly; it makes the bindings dict an explicit second field rather than smuggled in the URL; and refactor-rename of a PV becomes a one-line bindings edit instead of a re-parse of the expression body.

```
"{ibeam} * {energy}"                          # curve formula, two PV inputs
"mean({wf})"                                  # waveform-to-scalar reduction
"sqrt({a}**2 + {b}**2)"                       # works on either curve or waveform inputs
```

### 5.2 Validation pipeline

Every expression goes through `parser.py` before evaluation:

1. Regex-lift `{...}` tokens into a list and substitute placeholder identifiers (`__v0`, `__v1`, ...). Trace uses regex too; we keep it but reject nested braces explicitly so `{x*{y}}` errors at parse time rather than evaluating something surprising.
2. `ast.parse` the placeholder-substituted expression in `eval` mode.
3. Walk the AST and reject any node not in `language.grammar.ALLOWED_NODES` (BinOp, UnaryOp, Compare, BoolOp, Call, Name, Constant, Attribute on `np`/`math`). Reject `Lambda`, `Subscript` (for now), `comprehensions`, `ImportFrom`, dunder attribute access. This is the same posture as trace's `formula_validation.py`, lifted into a Canopy-shared module so other modules (Wren, eventually) can share it.
4. For every `Call`, the function must resolve to a name in `language.namespace.SAFE_FUNCS`.
5. Return a compiled artifact + the list of bound `{name}` tokens.

### 5.3 The evaluator

**Recommendation: numexpr for the array/curve/waveform paths, asteval for the scalar path. Final choice deferred to whoever implements §3.** The two reports point in this direction:

- pydm calc uses bare `eval()` with `{math, np}` injected. Fast, unsafe, no AST sandbox. We are not shipping that.
- trace uses an `ast`-based whitelist plus `eval()`. Safer, but `eval()` on numpy expressions is slow when expressions are evaluated millions of times across a curve.
- numexpr compiles `a + sqrt(b**2)` once and runs it on aligned arrays at C speed; it is the right tool for curve and waveform paths.
- numexpr's function set is narrow (no `mean`, no conditionals). For scalar formulas with branching, comparisons, or string handling, asteval is a better fit and stays sandboxed.
- Fallback / unknown-function handling: if an expression contains a function numexpr does not support, the curve evaluator falls back to a vectorized asteval pass over the aligned arrays. Caller sees the same `CurveResult`.

The implementer should benchmark both on representative inputs before locking this in, and document the choice in `services/evaluator.py`.

### 5.4 Namespace exposed to expressions

`language.namespace.SAFE_FUNCS` — a single, central allowlist:

```
arithmetic:   + - * / ** %  (operators, not functions)
math:         sin, cos, tan, asin, acos, atan, atan2, sinh, cosh, tanh,
              exp, log, log10, ln (= log), sqrt, pow, abs, floor, ceil,
              radians, degrees, pi, e, inf, nan
reductions:   mean, sum, min, max, std, var, ptp, count_nonzero
elementwise:  np.where (only via the `where` shorthand), np.clip
constants:    True, False, None
```

No `__import__`, no `open`, no attribute access except on `np`/`math`. No magic `prev_res` (pydm calc has this; it has caused confusion and we are dropping it — formulas are pure).

## 6. Three formula kinds — what differs per path

| Concern | scalar | curve | waveform |
|---|---|---|---|
| Vole call | `read_window(t1=0, max_points=1)` per input | `read_aligned(pvs=…, t0,t1, align, index)` | `read_window(t1=0, max_points=1)` (latest waveform sample) |
| Eval over | one number per input | aligned numpy arrays (length N) | one numpy array (length M) |
| Output | `ScalarResult` (length 1) | `CurveResult` (length N) | `WaveformResult` (length M) |
| Live tailing | yes — see §7 | yes — see §7 | yes |
| Safety | asteval | numexpr w/ asteval fallback | numexpr |
| Decimation | n/a | yes (uniform-time, after eval) | n/a |

A single `FormulaSpec` carries `kind` explicitly. The frontend / Owl chooses the kind when the formula is created; Fox does not infer it.

## 7. Live formula curves — Vole as the stateful helper

**Fox stays stateless; Vole holds the derived ring.** Formula curves bind to PV names only — a binding cannot resolve to another `fox:<id>`. Users compose by editing the parent expression. This kills the circular-reference problem trace has at its source rather than detecting it after the fact.

### 7.1 The lifecycle

When a caller (Owl, typically) registers a *live* curve formula, it calls:

```python
fox.gateway.register_curve(spec: FormulaSpec) -> FormulaCurveId
```

`register_curve` does not run on Fox state. It:
1. Validates the expression.
2. Computes a stable `formula_curve_id` from `(expression, sorted(bindings))`.
3. Asks Vole to **create a derived ring** under the synthetic name `fox:<formula_curve_id>` and registers Fox as its writer.
4. Tells Vole: "every time any of `{bindings.values()}` gets a new sample, call back into Fox with the new aligned step; Fox will compute and append to the derived ring."

From that point on, the derived ring behaves like any other PV ring in Vole — readable via `vole.gateway.read_window`, subscribable via `vole.gateway.subscribe_window`, evicted under memory pressure, dropped when no subscriber remains. Hare can serve it out over WebSocket the same way it serves a real PV. The frontend cannot tell the difference.

### 7.2 What Vole gains

```python
# canopy.vole.gateway   (additions to the existing surface)

async def register_derived_ring(
    name: str,                            # "fox:<id>" — namespaced so they cannot collide with real PVs
    inputs: list[str],                    # the PVs this derived ring depends on
    on_input_step: Callable[[dict[str, Sample]], Awaitable[Sample | None]],
    *,
    max_age_ns: int | None = None,
    max_points: int | None = None,
) -> None:
    """Create a derived ring and wire `on_input_step` as its writer.

    Vole calls `on_input_step` with the aligned latest-sample dict whenever
    any input gets a new sample. The callback returns one `Sample` to append,
    or `None` to skip this step (e.g. inputs not all reporting yet)."""

async def unregister_derived_ring(name: str) -> None: ...
```

This is the only API surface Vole grows for Fox. The eviction service treats derived rings the same as PV rings; LRU just works.

### 7.3 What stays in Owl

The *definition* of a live curve formula (its expression, its plot color, its axis assignment) is plot config. That belongs to Owl, which already persists plot configs. When `canopy-api` restarts, Owl re-registers each live formula with Fox on startup; Fox re-creates the Vole derived ring; the ring repopulates as inputs come in. There is no formula-state recovery on the Fox or Vole side — it is all reconstruction from Owl's persisted truth.

### 7.4 Memory sub-ceiling for `fox:*` rings

Vole's eviction service tracks a global byte ceiling (default 4 GB per replica, see Vole §6). Derived rings count against the same ceiling but also against a **dedicated `fox:*` sub-ceiling** — default 25% of the global, configurable via `VoleSettings.derived_ring_bytes_max`. Eviction prefers `fox:*` rings over input PV rings when the sub-ceiling is hit, even if the global ceiling is fine. This keeps a runaway formula registry (e.g. an Owl plot config that accidentally registers thousands of curves) from evicting the input PVs that other plots depend on.

### 7.5 Why not put curve state in Fox

Two reasons:
- A stateful Fox would couple compute to memory ceilings. Vole already owns memory ceilings; adding a second one is duplication.
- Eviction logic for "we are out of RAM, drop this curve" is identical for input rings and derived rings. Co-locating them means one LRU policy, not two.

## 8. Multi-replica behavior

Same answer as Vole: each `canopy-api` replica computes its own formulas against its own Vole. Sticky sessions (Hare's WS) keep one client's subscriptions on one replica, so its formulas are evaluated and tailed locally. Two replicas may evaluate the same formula independently; that is acceptable — formulas are pure.

If we ever extract Vole into a shared TSDB-backed service (see §7 of the Vole page), Fox does not need to change because it is already stateless.

## 9. Public API — `canopy.fox.gateway`

```python
"""
canopy.fox.gateway
==================

Public API for the Fox formula engine. Other Canopy modules import only
from this file; everything else under `canopy.fox.*` is private.

All evaluation is functional: each call computes a result from
(expression, bindings, time-window) and returns. State for live formula
curves lives in Vole, written through `register_curve` below.
"""

from canopy.contracts.formula import (
    FormulaSpec, FormulaResult, ScalarResult, CurveResult, WaveformResult,
)


# --- one-shot evaluation ------------------------------------------------ #

async def evaluate(
    spec: FormulaSpec,
    *,
    t0_ns: int,
    t1_ns: int,
) -> FormulaResult:
    """Pure function: evaluate `spec` over the given window using current
    Vole contents. `t1_ns=0` means "now".

    Pulls aligned inputs from `vole.gateway.read_aligned` (or `read_window`
    for scalar / waveform kinds). If any binding's PV is not yet buffered,
    raises `FormulaInputMissing` listing the missing PVs so the caller can
    seed Mole and retry.

    Raises:
        FormulaParseError, FormulaUnsafeError, FormulaTypeError,
        FormulaDimensionMismatch, FormulaInputMissing, FormulaEvalTimeout.
    """


async def validate(spec: FormulaSpec) -> FormulaValidationDTO:
    """Parse + AST-check + bindings-resolve without touching Vole.
    Useful for the formula maker UI to red-line a bad expression as the
    user types. Cheap; no I/O."""


# --- live formula curves ------------------------------------------------ #

async def register_curve(spec: FormulaSpec) -> str:
    """Register a live curve formula with Vole.

    Returns a stable `formula_curve_id` derived from the spec. Subsequent
    calls with an identical spec are idempotent (returns the same id, does
    not create a second ring). The ring is reachable as `fox:<id>` via
    `vole.gateway.read_window` / `subscribe_window`.

    Caller (Owl) is responsible for unregistering when the plot config
    is deleted."""


async def unregister_curve(formula_curve_id: str) -> None:
    """Stop tailing the formula and ask Vole to drop the derived ring.
    Idempotent."""
```

## 10. The service behind the gateway

Same pattern as Mole and Vole — the gateway is one-line wrappers around `FormulaService` methods. `FormulaService` holds:
- a reference to `vole.gateway` (injected, swappable in tests),
- a `Parser` (cached `ast.parse` + AST-walk results),
- a per-`(numexpr|asteval)` evaluator pool,
- a registry of currently-live `formula_curve_id` → input-PV set, for `register_curve` idempotency.

Concurrency: fully async, no locks. `register_curve` takes a brief lock around the registry to keep idempotency atomic; everything else is lock-free.

## 11. Wire surface — `/fox/v1/...`

```
POST /fox/v1/evaluate                     Security(require_read_access)
POST /fox/v1/validate                     Security(require_read_access)
GET  /fox/v1/health/{summary,watchdog}    Security(require_read_access)
```

`/evaluate` body is `NewFormulaEvalDTO = FormulaSpec + {t0_ns, t1_ns}`. Response is `FormulaResultDTO`, the discriminated union shown in §4. Same camelCase wire convention as everywhere else.

There is **no live-subscription endpoint on Fox.** Live formula tailing is reached by subscribing to the derived ring `fox:<id>` through Hare's existing WebSocket — same path the frontend already uses for real PVs, no second protocol to learn.

`register_curve` / `unregister_curve` are deliberately **not** on the wire. They are gateway-only because the only expected callers are Owl (plot config save/delete) and Hare (Wren reuse). Notebooks that want live formula data can `POST /evaluate` in a loop or subscribe to a real PV that Owl has already registered.

## 12. Testing strategy

- **Parser/grammar unit tests**: golden table of expressions → expected (kind-allowed | parse-error | unsafe-error). Property tests with Hypothesis on round-trip `{name}`-substitution.
- **Evaluator unit tests, per kind**: scalar (numpy values, edge cases for `nan`/`inf`/`None`), curve (golden time-series in, golden out), waveform (vector ops on numpy arrays).
- **Service integration tests**: `FormulaService` against a fake `vole.gateway` with scripted aligned-window outputs. Exercises `FormulaInputMissing` retry, the alignment plumbing, decimation.
- **End-to-end**: real Vole + real Hare-fed samples + real Mole-seeded windows (uses Mole's existing test fixture archiver) → assert curve formula yields expected aligned values across the live/archive seam.
- **Sandbox tests**: every dangerous Python construct (`__import__`, `open`, `lambda`, `Subscript`, dunders, comprehensions) tested to assert it raises `FormulaUnsafeError`. New entries added when bugs find one we missed.

No fakes for numexpr / asteval — they are deterministic libraries.

## 13. Decisions and open questions

### Decided (locked for v1)

- **`{name}`-only token syntax** — bare PV names are not accepted (§5.1).
- **Live scalar formulas are 1-point curves** — realized via `register_curve` with `index="grid"`, `grid_dt_ns` set to the desired cadence (default 1 s). No separate scalar-tail path in v1.
- **No formula-of-formula composition** — bindings resolve to PV names only; `{fox:<id>}` is rejected at `register_curve` and at `evaluate`. Users compose by editing the parent expression. Removes the circular-reference class of bug entirely.
- **Dedicated `fox:*` sub-ceiling in Vole** — default 25% of Vole's global byte ceiling, configurable. Eviction prefers `fox:*` rings over input PV rings when the sub-ceiling is hit (§7.4).
- **Decimation lives in `canopy.core.decimation`** — lifted out of Mole; both Mole and Fox import from it. Mole's existing implementation is the seed; Fox does not duplicate.
- **Severity propagation for curves** — per-output-step "worst severity across inputs at this step." pydm calc's drop-severity behavior is not carried forward.

### Still open

1. **numexpr vs asteval split** (§5.3). Recommended: numexpr for array paths, asteval for scalar branching. Implementer to benchmark on representative inputs and confirm or override.
2. **Eval timeout defaults**. 250 ms feels right for a single `/evaluate` curve call at N=10k; waveform at M=100k probably wants more headroom. Measure on a representative formula set before defaulting.
3. **Output of long-running export jobs**. Fox emits `CurveResult` in memory. CSV / MATLAB export of a formula curve is a Jobs concern; v1 returns the in-memory result and lets the existing Mole export path handle the file. Revisit once a shared `/exports/v1/...` surface exists.
