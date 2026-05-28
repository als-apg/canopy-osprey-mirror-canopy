"""Public surface of the Fox module for in-process callers.

Other Canopy modules import only from this file; the rest of ``canopy.fox.*``
is private implementation. Each function is a thin facade over ``FormulaService``.

``bind()`` wires the service singleton at app startup; ``get_service()`` is exposed
for ``dependencies.py``. Tests can re-bind a fake service freely.
"""

from canopy.contracts.formula import FormulaSpec, FormulaResult, FormulaValidationResult
from canopy.fox.services.formula_service import FormulaService

_service: FormulaService | None = None


def bind(service: FormulaService | None) -> None:
    """Wire (or unwire) the FormulaService singleton."""
    global _service
    _service = service


def get_service() -> FormulaService:
    if _service is None:
        raise RuntimeError("Fox gateway accessed before app startup bound a service")
    return _service


# --------------------------------------------------------------------------- #
# Public facade: one-shot evaluation
# --------------------------------------------------------------------------- #


async def evaluate(spec: FormulaSpec, *, t0_ns: int, t1_ns: int) -> FormulaResult:
    """Pure function: evaluate ``spec`` over the given window.

    Pulls inputs from the data source (Vole, eventually). Raises
    ``FormulaInputMissing`` if any binding's PV is not buffered.
    """
    return await get_service().evaluate(spec, t0_ns=t0_ns, t1_ns=t1_ns)


async def validate(spec: FormulaSpec) -> FormulaValidationResult:
    """Parse + AST-check without touching the data source. Cheap; no I/O."""
    return await get_service().validate(spec)


# --------------------------------------------------------------------------- #
# Live formula curves — deferred until Vole ships
# --------------------------------------------------------------------------- #


async def register_curve(spec: FormulaSpec) -> str:
    """Deferred until Vole exists. See Module-Architecture-Fox.md section 7."""
    raise NotImplementedError("register_curve requires Vole (not yet implemented)")


async def unregister_curve(formula_curve_id: str) -> None:
    """Deferred until Vole exists."""
    raise NotImplementedError("unregister_curve requires Vole (not yet implemented)")
