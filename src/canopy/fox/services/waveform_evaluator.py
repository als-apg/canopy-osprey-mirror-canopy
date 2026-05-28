"""Waveform formula evaluation path.

Reads the latest waveform (array-valued) sample for each binding, evaluates
the expression over the vectors, and returns a ``WaveformResult``.
"""

import numpy as np

from canopy.fox.ports import DataSource
from canopy.fox.config import FoxConfig
from canopy.fox.errors import FormulaInputMissing, FormulaDimensionMismatch
from canopy.contracts.sample import AlarmSeverity
from canopy.contracts.formula import FormulaSpec, WaveformResult
from canopy.fox.services.parser import ParsedFormula
from canopy.fox.services.evaluator import evaluate_array, worst_severity


async def evaluate_waveform_formula(
    spec: FormulaSpec,
    parsed: ParsedFormula,
    data_source: DataSource,
    config: FoxConfig,
) -> WaveformResult:
    """Evaluate a waveform formula against latest array-valued samples."""
    variables: dict[str, np.ndarray] = {}
    severities: list[AlarmSeverity] = []
    timestamps: list[int] = []
    missing: list[str] = []
    lengths: set[int] = set()

    for i, token in enumerate(parsed.tokens):
        pv = spec.bindings[token]
        sample = await data_source.read_latest(pv)
        if sample is None:
            missing.append(pv)
            continue

        if isinstance(sample.value, list | bytes):
            arr = np.asarray(list(sample.value), dtype=np.float64)
        elif isinstance(sample.value, np.ndarray):
            arr = sample.value.astype(np.float64)
        else:
            arr = np.array([float(sample.value) if sample.value is not None else 0.0])

        variables[f"__v{i}"] = arr
        lengths.add(len(arr))
        severities.append(sample.severity)
        timestamps.append(sample.ts_ns)

    if missing:
        raise FormulaInputMissing(f"PVs not available: {missing}")

    if len(lengths) > 1:
        raise FormulaDimensionMismatch(
            f"Waveform inputs have mismatched lengths: {sorted(lengths)}"
        )

    timeout_s = config.eval_timeout_ms / 1000.0
    result_array = await evaluate_array(parsed, variables, timeout_s)

    return WaveformResult(
        ts_ns=max(timestamps) if timestamps else 0,
        values=result_array.flatten().tolist(),
        severity=worst_severity(severities) if severities else AlarmSeverity.INVALID,
    )
