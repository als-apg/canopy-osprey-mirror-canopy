"""Timeseries formula evaluation path.

Pulls aligned time-series windows from the data source, evaluates the expression
element-wise over the aligned arrays, and optionally decimates the output.
"""

import numpy as np

from canopy.fox.ports import DataSource
from canopy.fox.config import FoxConfig
from canopy.fox.errors import FormulaDimensionMismatch
from canopy.core.decimation import uniform_time_decimate
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec, TimeseriesResult
from canopy.fox.services.parser import ParsedFormula
from canopy.fox.services.evaluator import evaluate_array, worst_severity_arrays


async def evaluate_timeseries_formula(
    spec: FormulaSpec,
    parsed: ParsedFormula,
    data_source: DataSource,
    t0_ns: int,
    t1_ns: int,
    config: FoxConfig,
) -> TimeseriesResult:
    """Evaluate a timeseries formula over [t0_ns, t1_ns] using aligned windows."""
    pvs = [spec.bindings[tok] for tok in parsed.tokens]
    max_points = spec.max_points or config.max_output_points

    if spec.mode == "binned":
        window = await data_source.read_binned(pvs, t0_ns, t1_ns, target_buckets=max_points)
    else:
        window = await data_source.read_aligned(
            pvs,
            t0_ns,
            t1_ns,
            align=spec.align,
            index=spec.index,
            grid_dt_ns=spec.grid_dt_ns,
        )

    variables: dict[str, np.ndarray] = {}
    for i, token in enumerate(parsed.tokens):
        pv = spec.bindings[token]
        variables[f"__v{i}"] = window.arrays[pv]

    timeout_s = config.eval_timeout_ms / 1000.0
    result_array = await evaluate_array(parsed, variables, timeout_s)

    n_points = len(window.ts_ns)
    if result_array.ndim == 0 or result_array.shape[0] != n_points:
        raise FormulaDimensionMismatch(
            f"Expression produced shape {result_array.shape} but expected ({n_points},); "
            "reduction functions like mean() are not supported for curve formulas"
        )

    ts_list = window.ts_ns.tolist()
    values: list[float | None] = []
    for v in result_array.flat:
        if np.isnan(v):
            values.append(None)
        else:
            values.append(float(v))

    sev_arrays = [
        window.severities[spec.bindings[tok]] for tok in parsed.tokens if spec.bindings[tok] in window.severities
    ]
    severity = worst_severity_arrays(sev_arrays) if sev_arrays else [AlarmSeverity.NO_ALARM] * len(ts_list)

    inputs = {tok: spec.bindings[tok] for tok in parsed.tokens}

    decimated = False
    if len(ts_list) > max_points:
        samples = [Sample(ts_ns=t, value=v, severity=s, source="archive") for t, v, s in zip(ts_list, values, severity)]
        samples = uniform_time_decimate(samples, max_points)
        ts_list = [s.ts_ns for s in samples]
        values = [s.value for s in samples]
        severity = [s.severity for s in samples]
        decimated = True

    return TimeseriesResult(
        ts_ns=ts_list,
        values=values,
        severity=severity,
        inputs=inputs,
        decimated=decimated,
    )
