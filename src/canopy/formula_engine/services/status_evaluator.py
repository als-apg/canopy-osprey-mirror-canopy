"""Scalar evaluation of a formula with vector tokens or status functions.

A plain scalar formula fails as a whole when one input has no value. A formula
over many channels must not: one channel that is down would hide the state of
all the others. So this path reads every input into three numbers:

==========================  =======  ==========  =============
input                       value    severity()  connected()
==========================  =======  ==========  =============
has a number                number   0 to 3      1
connection down (outage)    NaN      3           0
no sample yet               NaN      3           0
sample that is no number    NaN      as sampled  1
==========================  =======  ==========  =============

"No number" is text (also text that looks like a number), an array, or an empty
sample. That holds for every member of a vector token and for a token the
expression reads only through ``severity()`` or ``connected()``. A token whose
value the expression reads directly (``{x}`` outside a status function, or
through ``label()``) keeps the rule of plain formulas: in outage or without a
sample, the formula fails with ``FormulaInputOutage`` / ``FormulaInputMissing``.

A member without a number must never pass for a number. ``severity()``,
``connected()``, ``count`` and the ``nan*`` reductions are safe: they do not
read the value, or leave a NaN out. Everywhere else a NaN would be taken for a
value (a comparison with NaN is false, ``count_nonzero`` counts it), so a
formula that reads a vector token's values any other way
(``ParsedFormula.unguarded_vectors``) fails while a member of that token has no
number, with the retryable input error naming those members.

The result's own severity is the worst over all inputs, and an input without a
sample counts as INVALID. A formula with one member down therefore never looks
healthy, whatever its expression computes.

The result is always a finite number, a bool or text. NaN and infinity are not
JSON, and a client drops a frame it cannot parse, so it would keep showing the
last good value. A result that is not finite because inputs have no value, or
because an input's own value is infinite, is the same retryable input error
naming them. Any other non-finite result is a ``FormulaTypeError``.

The inputs are read together under one time limit
(``vector_read_timeout_ms``); an input not read in time is without a value.
Members and status reads take what the live side holds and never ask the
archive.
"""

import math
import time
import asyncio

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
    FormulaInputUnusable,
)
from canopy.formula_engine.services.parser import LABEL_FUNC, ParsedFormula
from canopy.formula_engine.services.evaluator import worst_severity, evaluate_scalar

_INVALID = SEVERITY_ORDINAL[AlarmSeverity.INVALID]


def _number(sample: Sample | None) -> float:
    """A vector member's value: its number, or NaN when it has none (text is never a number)."""
    if sample is None or not isinstance(sample.value, float | int | bool):
        return math.nan
    return float(sample.value)


def _plain_value(pv: str, sample: Sample) -> float | int | bool | str:
    """A directly read token's value, by the rule of plain scalar formulas."""
    if isinstance(sample.value, float | int | bool | str):
        return sample.value
    try:
        return float(sample.value)
    except (TypeError, ValueError) as exc:
        raise FormulaTypeError(
            f"PV '{pv}' value is not usable in a scalar formula: {type(sample.value).__name__}"
        ) from exc


def _label(sample: Sample | None, labels: list[str] | None) -> str:
    """``label()``: the enum string the channel's value stands for.

    A value that is text already is its own label. A whole number that indexes
    the channel's enum strings gives that string. Everything else (a channel
    without enum strings, an index outside them, a fraction) gives ``''``.
    """
    value = None if sample is None else sample.value
    if isinstance(value, str):
        return value
    if labels and not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value):
        index = int(value)
        if index == value and 0 <= index < len(labels):
            return labels[index]
    return ""


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
    strict = [t for t in scalar_tokens if t in parsed.value_tokens or t in parsed.label_tokens]

    # A directly read value fails in outage before any read, as in a plain formula.
    strict_pvs = dict.fromkeys(spec.bindings[t] for t in strict)
    in_outage = [pv for pv in strict_pvs if data_source.in_open_outage(pv)]
    if in_outage:
        raise FormulaInputOutage.for_pvs(in_outage)

    samples = await _read_inputs(spec, [*scalar_tokens, *parsed.member_keys()], set(strict), data_source, config)

    missing = list(dict.fromkeys(spec.bindings[t] for t in strict if samples[t] is None))
    if missing:
        raise FormulaInputMissing.for_pvs(missing)
    # A sample that is there but empty is no value either (and never a 0).
    empty = list(dict.fromkeys(spec.bindings[t] for t in strict if samples[t].value is None))
    if empty:
        raise FormulaInputUnusable.for_pvs(empty)

    # A member without a number must not pass for one outside a nan* reduction.
    unguarded = {key for token in parsed.unguarded_vectors for key in vectors[token]}
    exposed = [key for key in parsed.member_keys() if key in unguarded and math.isnan(_number(samples[key]))]
    if exposed:
        raise _input_error(spec, exposed, samples, data_source, config)

    # Binding keys whose value (not only whose status) the expression reads.
    read_by_value = {key for t in parsed.value_tokens for key in vectors.get(t, (t,))}

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
        if func == LABEL_FUNC:
            pv = spec.bindings[token]
            variables[name] = _label(samples[token], await data_source.enum_labels(pv))
            continue
        read = _severity if func == "severity" else (lambda sample: 0 if sample is None else 1)
        if token in vectors:
            variables[name] = np.fromiter(
                (read(samples[k]) for k in vectors[token]), dtype=np.int64, count=len(vectors[token])
            )
        else:
            variables[name] = read(samples[token])

    value = _one_value(await evaluate_scalar(parsed, variables, config.eval_timeout_ms / 1000.0))
    if isinstance(value, float) and not math.isfinite(value):
        # Inputs that can be the cause: among those read by value, the ones
        # without a sample (they read as NaN) and the ones whose own value is
        # NaN or infinite.
        culprits = [
            key
            for key, sample in samples.items()
            if key in read_by_value
            and (
                (sample is None and math.isnan(value))
                or (sample is not None and isinstance(sample.value, float) and not math.isfinite(sample.value))
            )
        ]
        if culprits:
            raise _input_error(spec, culprits, samples, data_source, config)
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
        pending=sum(
            1 for key, s in samples.items() if s is None and not data_source.in_open_outage(spec.bindings[key])
        ),
    )


async def _read_inputs(
    spec: FormulaSpec,
    keys: list[str],
    strict: set[str],
    data_source: DataSource,
    config: FormulaEngineConfig,
) -> dict[str, Sample | None]:
    """One read per binding key, all together, under one time limit.

    An input in outage is not read: its last sample is from before the drop. A
    directly read token is read as a plain formula reads it; every other input
    takes what the live side holds. An input not read within
    ``vector_read_timeout_ms`` is without a value.
    """

    async def read(key: str) -> Sample | None:
        pv = spec.bindings[key]
        if data_source.in_open_outage(pv):
            return None
        if key in strict:
            return await data_source.read_latest(pv)
        return await data_source.read_latest_live(pv)

    tasks = {key: asyncio.ensure_future(read(key)) for key in dict.fromkeys(keys)}
    try:
        _, late = await asyncio.wait(tasks.values(), timeout=config.vector_read_timeout_ms / 1000.0)
    finally:
        for task in tasks.values():
            task.cancel()
    return {key: None if task in late else task.result() for key, task in tasks.items()}


def _input_error(
    spec: FormulaSpec,
    keys: list[str],
    samples: dict[str, Sample | None],
    data_source: DataSource,
    config: FormulaEngineConfig,
) -> FormulaInputMissing:
    """The retryable error naming the inputs ``keys`` (capped), by the reason that weighs most.

    A lost connection first, then a sample still on its way, then a value that
    is there but unusable. The wording is one for all three.
    """
    absent = [spec.bindings[key] for key in keys if samples[key] is None]
    if any(data_source.in_open_outage(pv) for pv in absent):
        error: type[FormulaInputMissing] = FormulaInputOutage
    else:
        error = FormulaInputMissing if absent else FormulaInputUnusable
    named = list(dict.fromkeys(spec.bindings[key] for key in keys))
    listed = named[: config.vector_missing_listed]
    if len(listed) == len(named):
        return error.for_pvs(named)
    return error(f"{INPUT_MISSING_PREFIX}{listed} and {len(named) - len(listed)} more")


def _vector_inputs(
    spec: FormulaSpec, parsed: ParsedFormula, samples: dict[str, Sample | None], listed: int
) -> VectorInputs | None:
    """The members counted, and the first ``listed`` of those without a sample named."""
    members = parsed.member_keys()
    if not members:
        return None
    missing = [spec.bindings[key] for key in members if samples[key] is None]
    return VectorInputs(count=len(members), missing=missing[:listed], more=max(len(missing) - listed, 0))
