"""Scalar evaluation of a formula with vector tokens or status functions.

A plain scalar formula fails as a whole when one input has no value. A formula
over many channels must not: one channel that is down would hide the state of
all the others. So this path reads every input into three numbers and never
fails on an input it does not read as a plain value:

==========================  =======  ==========  =============
input                       value    severity()  connected()
==========================  =======  ==========  =============
has a sample                number   0 to 3      1
connection down (outage)    NaN      3           0
no sample yet               NaN      3           0
sample without a number     NaN      as sampled  1
==========================  =======  ==========  =============

That holds for every member of a vector token and for a token the expression
reads only through ``severity()`` or ``connected()``. A token whose value the
expression reads directly (``{x}`` outside a status function) keeps the rule of
plain formulas: in outage or without a sample, the formula fails with
``FormulaInputOutage`` / ``FormulaInputMissing``.

The result's own severity is the worst over all inputs, and an input without a
sample counts as INVALID. A formula with one member down therefore never looks
healthy, whatever its expression computes.

The result is always a finite number, a bool or text. NaN and infinity are not
JSON, and a client drops a frame it cannot parse, so it would keep showing the
last good value. A NaN that comes from inputs without a value (``max({*})``
with a member down) is therefore reported as ``FormulaInputOutage`` /
``FormulaInputMissing`` naming those inputs, the error a plain formula gives.
Any other non-finite result is a ``FormulaTypeError``.
"""

import math
import time

import numpy as np

from canopy.contracts.sample import SEVERITY_ORDINAL, Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec, ScalarResult, VectorInputs
from canopy.formula_engine.ports import DataSource
from canopy.formula_engine.config import FormulaEngineConfig
from canopy.formula_engine.errors import (
    INPUT_MISSING_PREFIX,
    FormulaTypeError,
    FormulaInputOutage,
    FormulaInputMissing,
)
from canopy.formula_engine.services.parser import ParsedFormula
from canopy.formula_engine.services.evaluator import worst_severity, evaluate_scalar

_INVALID = SEVERITY_ORDINAL[AlarmSeverity.INVALID]


def _number(sample: Sample | None) -> float:
    """A vector member's value: its number, or NaN when it has none."""
    if sample is None or not isinstance(sample.value, float | int | bool | str):
        return math.nan
    try:
        return float(sample.value)
    except (TypeError, ValueError):
        return math.nan


def _plain_value(pv: str, sample: Sample) -> float | int | bool | str:
    """A directly read token's value, by the rule of plain scalar formulas."""
    if isinstance(sample.value, float | int | bool | str):
        return sample.value
    if sample.value is None:
        return 0.0
    try:
        return float(sample.value)
    except (TypeError, ValueError) as exc:
        raise FormulaTypeError(
            f"PV '{pv}' value is not usable in a scalar formula: {type(sample.value).__name__}"
        ) from exc


def _severity(sample: Sample | None) -> int:
    return _INVALID if sample is None else SEVERITY_ORDINAL[sample.severity]


def _one_value(value: object) -> float | int | bool | str:
    """The expression's result as one plain value; an array left unreduced is an error."""
    if isinstance(value, np.ndarray):
        if value.ndim > 0:
            raise FormulaTypeError(
                "A scalar formula must reduce a vector token to one value, e.g. max(...), sum(...), count_nonzero(...)"
            )
        return value.item()
    if isinstance(value, np.generic):
        return value.item()
    return value


async def evaluate_status_formula(
    spec: FormulaSpec,
    parsed: ParsedFormula,
    data_source: DataSource,
    config: FormulaEngineConfig,
) -> ScalarResult:
    """Evaluate a scalar formula that has vector tokens or status functions."""
    vectors = parsed.vector_tokens
    scalar_tokens = [t for t in parsed.tokens if t not in vectors]
    strict = [t for t in scalar_tokens if t in parsed.value_tokens]

    # A directly read value fails in outage before any read, as in a plain formula.
    strict_pvs = dict.fromkeys(spec.bindings[t] for t in strict)
    in_outage = [pv for pv in strict_pvs if data_source.in_open_outage(pv)]
    if in_outage:
        raise FormulaInputOutage.for_pvs(in_outage)

    # One read per binding key, however many tokens name it. An input in outage
    # is not read: its last sample is from before the drop.
    samples: dict[str, Sample | None] = {}
    for key in dict.fromkeys([*scalar_tokens, *parsed.member_keys()]):
        pv = spec.bindings[key]
        samples[key] = None if data_source.in_open_outage(pv) else await data_source.read_latest(pv)

    missing = list(dict.fromkeys(spec.bindings[t] for t in strict if samples[t] is None))
    if missing:
        raise FormulaInputMissing.for_pvs(missing)

    variables: dict[str, object] = {}
    for idx, token in enumerate(parsed.tokens):
        if token not in parsed.value_tokens:
            continue
        if token in vectors:
            variables[f"__v{idx}"] = np.fromiter(
                (_number(samples[k]) for k in vectors[token]), dtype=np.float64, count=len(vectors[token])
            )
        else:
            variables[f"__v{idx}"] = _plain_value(spec.bindings[token], samples[token])

    for name, func, token in parsed.status_symbols:
        read = _severity if func == "severity" else (lambda sample: 0 if sample is None else 1)
        if token in vectors:
            variables[name] = np.fromiter(
                (read(samples[k]) for k in vectors[token]), dtype=np.int64, count=len(vectors[token])
            )
        else:
            variables[name] = read(samples[token])

    value = _one_value(await evaluate_scalar(parsed, variables, config.eval_timeout_ms / 1000.0))
    if isinstance(value, float) and not math.isfinite(value):
        absent = list(dict.fromkeys(spec.bindings[key] for key, s in samples.items() if s is None))
        if absent and math.isnan(value):
            down = any(data_source.in_open_outage(pv) for pv in absent)
            error = FormulaInputOutage if down else FormulaInputMissing
            listed = absent[: config.vector_missing_listed]
            if len(listed) == len(absent):
                raise error.for_pvs(absent)
            raise error(f"{INPUT_MISSING_PREFIX}{listed} and {len(absent) - len(listed)} more")
        raise FormulaTypeError(f"The result is not a finite number ({value})")

    stamps = [s.ts_ns for s in samples.values() if s is not None]
    severities = [AlarmSeverity.INVALID if s is None else s.severity for s in samples.values()]
    return ScalarResult(
        # With no input sample at all the result still needs a time: now.
        ts_ns=max(stamps) if stamps else time.time_ns(),
        value=value,
        severity=worst_severity(severities),
        # Plain tokens are listed as in every scalar result; members are summed up.
        inputs_used={spec.bindings[t]: samples[t].ts_ns for t in scalar_tokens if samples[t] is not None},
        vector_inputs=_vector_inputs(spec, parsed, samples, config.vector_missing_listed),
    )


def _vector_inputs(
    spec: FormulaSpec, parsed: ParsedFormula, samples: dict[str, Sample | None], listed: int
) -> VectorInputs | None:
    """The members counted, and the first ``listed`` of those without a sample named."""
    members = parsed.member_keys()
    if not members:
        return None
    missing = [spec.bindings[key] for key in members if samples[key] is None]
    return VectorInputs(count=len(members), missing=missing[:listed], more=max(len(missing) - listed, 0))
