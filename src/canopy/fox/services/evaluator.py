"""Core evaluation dispatch: numexpr for arrays, asteval for scalars.

All compute is CPU-bound and runs via ``asyncio.to_thread`` to avoid blocking
the event loop. Timeout is enforced via ``asyncio.wait_for``.
"""

import asyncio
from collections.abc import Iterable

import numpy as np
import numexpr as ne
from asteval import Interpreter

from canopy.fox.errors import FoxError, FormulaTypeError, FormulaEvalTimeout
from canopy.contracts.sample import AlarmSeverity
from canopy.fox.services.parser import ParsedFormula
from canopy.fox.language.namespace import NUMEXPR_NAME_MAP, build_eval_namespace

_SEVERITY_ORDER = [AlarmSeverity.NO_ALARM, AlarmSeverity.MINOR, AlarmSeverity.MAJOR, AlarmSeverity.INVALID]


def worst_severity(severities: Iterable[AlarmSeverity]) -> AlarmSeverity:
    """Return the most severe alarm across the inputs."""
    worst = AlarmSeverity.NO_ALARM
    for s in severities:
        if _SEVERITY_ORDER.index(s) > _SEVERITY_ORDER.index(worst):
            worst = s
            if worst == AlarmSeverity.INVALID:
                break
    return worst


def worst_severity_arrays(severity_arrays: list[np.ndarray]) -> list[AlarmSeverity]:
    """Per-step worst severity across multiple ordinal-encoded severity arrays.

    Each array element is an int index into ``_SEVERITY_ORDER``.
    """
    if not severity_arrays:
        return []
    stacked = np.stack(severity_arrays, axis=0)
    max_ordinals = np.max(stacked, axis=0)
    return [_SEVERITY_ORDER[int(o)] for o in max_ordinals]


def _numexpr_expr(parsed: ParsedFormula) -> str:
    """Translate our placeholder expression to numexpr-compatible form."""
    expr = parsed.placeholder_expr
    for our_name, ne_name in NUMEXPR_NAME_MAP.items():
        expr = expr.replace(our_name + "(", ne_name + "(")
    return expr


def _evaluate_array_numexpr(parsed: ParsedFormula, variables: dict[str, np.ndarray]) -> np.ndarray:
    """Evaluate using numexpr (fast path for numeric array expressions)."""
    expr = _numexpr_expr(parsed)
    local_dict = dict(variables)
    return ne.evaluate(expr, local_dict=local_dict)


def _evaluate_array_asteval(parsed: ParsedFormula, variables: dict[str, np.ndarray]) -> np.ndarray:
    """Evaluate using asteval (fallback for expressions with unsupported functions)."""
    interp = Interpreter(usersyms=build_eval_namespace())
    for name, arr in variables.items():
        interp.symtable[name] = arr
    result = interp(parsed.placeholder_expr)
    if interp.error:
        msgs = [err.get_error()[1] for err in interp.error]
        raise FormulaTypeError(f"Evaluation error: {'; '.join(msgs)}")
    if result is None:
        raise FormulaTypeError("Expression returned None")
    return np.asarray(result, dtype=np.float64)


def _evaluate_scalar_asteval(
    parsed: ParsedFormula, variables: dict[str, float | int | bool | str]
) -> float | int | bool | str:
    """Evaluate a scalar expression using asteval."""
    interp = Interpreter(usersyms=build_eval_namespace())
    for name, val in variables.items():
        interp.symtable[name] = val
    result = interp(parsed.placeholder_expr)
    if interp.error:
        msgs = [err.get_error()[1] for err in interp.error]
        raise FormulaTypeError(f"Evaluation error: {'; '.join(msgs)}")
    if result is None:
        raise FormulaTypeError("Expression returned None")
    return result


async def evaluate_array(
    parsed: ParsedFormula,
    variables: dict[str, np.ndarray],
    timeout_s: float,
) -> np.ndarray:
    """Dispatch array evaluation: numexpr if compatible, else asteval.

    Timeout is best-effort: ``to_thread`` workers cannot be interrupted, so a
    timed-out computation continues in the background until it completes
    naturally. The real resource bound is expression length + input size.
    """

    def compute():
        if parsed.is_numexpr_compatible:
            return _evaluate_array_numexpr(parsed, variables)
        return _evaluate_array_asteval(parsed, variables)

    return await _run_compute(compute, timeout_s)


async def evaluate_scalar(
    parsed: ParsedFormula,
    variables: dict[str, float | int | bool | str],
    timeout_s: float,
) -> float | int | bool | str:
    """Evaluate a scalar expression using asteval with timeout."""

    def compute():
        return _evaluate_scalar_asteval(parsed, variables)

    return await _run_compute(compute, timeout_s)


async def _run_compute(compute, timeout_s: float):
    """Run a compute() in a worker thread, mapping failures to typed errors.

    numexpr/numpy raise plain ``ValueError`` (shape/dtype), ``OverflowError``,
    ``RecursionError`` etc. Without this they escape as generic 500s; map them to
    ``FormulaTypeError`` (422) so bad inputs read as client errors, not crashes.
    """
    try:
        return await asyncio.wait_for(asyncio.to_thread(compute), timeout=timeout_s)
    except TimeoutError:
        raise FormulaEvalTimeout(f"Expression evaluation timed out after {timeout_s:.1f}s") from None
    except FoxError:
        raise
    except (ValueError, ArithmeticError, TypeError, RecursionError) as exc:
        raise FormulaTypeError(f"Evaluation error: {exc}") from exc
