"""Function and constant namespace exposed to Fox expressions.

``SAFE_FUNCS`` is the single allowlist: any ``Call`` in the AST whose function
name is not a key here is rejected. The values are the actual callables used
at evaluation time.
"""

import math

import numpy as np

SAFE_FUNCS: dict[str, object] = {
    # Trigonometric
    "sin": np.sin,
    "cos": np.cos,
    "tan": np.tan,
    "asin": np.arcsin,
    "acos": np.arccos,
    "atan": np.arctan,
    "atan2": np.arctan2,
    "sinh": np.sinh,
    "cosh": np.cosh,
    "tanh": np.tanh,
    # Exponential / logarithmic
    "exp": np.exp,
    "log": np.log,
    "log10": np.log10,
    "ln": np.log,
    "sqrt": np.sqrt,
    "pow": np.power,
    "abs": np.abs,
    # Rounding
    "floor": np.floor,
    "ceil": np.ceil,
    # Angle conversion
    "radians": np.radians,
    "degrees": np.degrees,
    # Reductions
    "mean": np.mean,
    "sum": np.sum,
    "min": np.min,
    "max": np.max,
    "std": np.std,
    "var": np.var,
    "ptp": np.ptp,
    "count_nonzero": np.count_nonzero,
    # Elementwise
    "where": np.where,
    "clip": np.clip,
}

SAFE_CONSTANTS: dict[str, object] = {
    "pi": math.pi,
    "e": math.e,
    "inf": math.inf,
    "nan": math.nan,
    "True": True,
    "False": False,
    "None": None,
}

# Functions that numexpr supports natively (used to detect fast-path compatibility)
NUMEXPR_FUNCS: set[str] = {
    "sin",
    "cos",
    "tan",
    "arcsin",
    "arccos",
    "arctan",
    "arctan2",
    "sinh",
    "cosh",
    "tanh",
    "exp",
    "log",
    "log10",
    "sqrt",
    "abs",
    "where",
}

# Mapping from our namespace names to numexpr-compatible names
NUMEXPR_NAME_MAP: dict[str, str] = {
    "asin": "arcsin",
    "acos": "arccos",
    "atan": "arctan",
    "atan2": "arctan2",
    "ln": "log",
}


def build_eval_namespace() -> dict[str, object]:
    """Build the namespace dict for asteval / direct eval."""
    ns: dict[str, object] = {}
    ns.update(SAFE_FUNCS)
    ns.update(SAFE_CONSTANTS)
    ns["np"] = np
    ns["math"] = math
    return ns
