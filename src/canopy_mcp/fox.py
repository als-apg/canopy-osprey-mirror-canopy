"""In-process Fox formula parsing for the lint pass.

canopy_mcp talks to the canopy backend over HTTP, with one exception: formula
checks parse in-process, so a draft is checked without a round trip and without
a running Fox. This module is the only place canopy_mcp touches ``canopy.fox``,
and it uses exactly one entry point, ``canopy.fox.gateway.make_offline_validator``
(the import-linter contract allows this edge and no other).

The validator is built once — by :func:`bind` at startup, or lazily on first use —
and only ever asked to ``validate``: it has no data source.
"""

from __future__ import annotations

from typing import Any

from canopy.contracts.formula import FormulaSpec
from canopy.fox.gateway import make_offline_validator

FORMULA_KINDS = ("scalar", "timeseries", "waveform")
DEFAULT_KIND = "scalar"

_validator: Any = None


def bind() -> None:
    """Build the process-wide offline validator (call once at startup)."""
    global _validator
    _validator = make_offline_validator()


def _get() -> Any:
    if _validator is None:
        bind()
    return _validator


async def formula_errors(expression: str, bindings: dict[str, str] | None = None, kind: str | None = None) -> list[str]:
    """Parse errors for one formula; ``[]`` when Fox accepts it."""
    try:
        spec = FormulaSpec(
            kind=kind if kind in FORMULA_KINDS else DEFAULT_KIND,
            expression=expression,
            bindings=dict(bindings or {}),
        )
    except Exception as exc:  # pydantic rejects the spec itself
        return [str(exc)]
    result = await _get().validate(spec)
    return [] if result.valid else list(result.errors or ["invalid formula"])
