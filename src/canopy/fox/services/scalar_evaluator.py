"""Scalar formula evaluation path.

Reads the latest sample for each binding via the data source, evaluates the
expression as a scalar, and returns a ``ScalarResult``.
"""

from canopy.fox.ports import DataSource
from canopy.fox.config import FoxConfig
from canopy.fox.errors import FormulaTypeError, FormulaInputOutage, FormulaInputMissing
from canopy.contracts.sample import AlarmSeverity
from canopy.contracts.formula import FormulaSpec, ScalarResult
from canopy.fox.services.parser import ParsedFormula
from canopy.fox.services.evaluator import worst_severity, evaluate_scalar


def raise_on_open_outage(spec: FormulaSpec, parsed: ParsedFormula, data_source: DataSource) -> None:
    """Raise ``FormulaInputOutage`` naming every bound input whose connection is down now.

    Runs before any ``read_latest``: the ring still holds the last sample from
    before the drop, and a read of a missing ring may refill it from the
    archive -- neither is the PV's current value.
    """
    in_outage = list(
        dict.fromkeys(pv for pv in (spec.bindings[t] for t in parsed.tokens) if data_source.in_open_outage(pv))
    )
    if in_outage:
        raise FormulaInputOutage(f"PVs disconnected: {in_outage}")


async def evaluate_scalar_formula(
    spec: FormulaSpec,
    parsed: ParsedFormula,
    data_source: DataSource,
    config: FoxConfig,
) -> ScalarResult:
    """Evaluate a scalar formula against latest values from the data source."""
    variables: dict[str, float | int | bool | str] = {}
    severities: list[AlarmSeverity] = []
    inputs_used: dict[str, int] = {}
    missing: list[str] = []

    raise_on_open_outage(spec, parsed, data_source)

    for idx, token in enumerate(parsed.tokens):
        pv = spec.bindings[token]
        sample = await data_source.read_latest(pv)
        if sample is None:
            missing.append(pv)
            continue
        if isinstance(sample.value, float | int | bool | str):
            variables[f"__v{idx}"] = sample.value
        elif sample.value is None:
            variables[f"__v{idx}"] = 0.0
        else:
            # e.g. an array-valued PV bound to a scalar formula: float(list) would
            # raise TypeError -> 500. Report it as a client type error instead.
            try:
                variables[f"__v{idx}"] = float(sample.value)
            except (TypeError, ValueError) as exc:
                raise FormulaTypeError(
                    f"PV '{pv}' value is not usable in a scalar formula: {type(sample.value).__name__}"
                ) from exc
        severities.append(sample.severity)
        inputs_used[pv] = sample.ts_ns

    if missing:
        raise FormulaInputMissing(f"PVs not available: {missing}")

    timeout_s = config.eval_timeout_ms / 1000.0
    value = await evaluate_scalar(parsed, variables, timeout_s)

    return ScalarResult(
        ts_ns=max(inputs_used.values()) if inputs_used else 0,
        value=value,
        severity=worst_severity(severities) if severities else AlarmSeverity.INVALID,
        inputs_used=inputs_used,
    )
