"""Scalar formula evaluation path.

Reads the latest sample for each binding via the data source, evaluates the
expression as a scalar, and returns a ``ScalarResult``.
"""

from canopy.fox.ports import DataSource
from canopy.fox.config import FoxConfig
from canopy.fox.errors import FormulaInputMissing
from canopy.contracts.sample import AlarmSeverity
from canopy.contracts.formula import FormulaSpec, ScalarResult
from canopy.fox.services.parser import ParsedFormula
from canopy.fox.services.evaluator import worst_severity, evaluate_scalar


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

    for idx, token in enumerate(parsed.tokens):
        pv = spec.bindings[token]
        sample = await data_source.read_latest(pv)
        if sample is None:
            missing.append(pv)
            continue
        if isinstance(sample.value, float | int | bool | str):
            variables[f"__v{idx}"] = sample.value
        else:
            variables[f"__v{idx}"] = float(sample.value) if sample.value is not None else 0.0
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
