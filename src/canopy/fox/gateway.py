"""Public surface of the Fox module for in-process callers.

Other Canopy modules import only from this file; the rest of ``canopy.fox.*``
is private implementation. Each function is a thin facade over ``FormulaService``.

``bind()`` wires the service singleton at app startup; ``get_service()`` is exposed
for ``dependencies.py``. Tests can re-bind a fake service freely.
"""

from canopy.contracts.formula import FormulaSpec, FormulaResult, FormulaValidationResult
from canopy.fox.services.formula_service import FormulaService
from canopy.fox.services.subscription_manager import SubscriptionManager

_service: FormulaService | None = None
_sub_manager: SubscriptionManager | None = None


def bind(service: FormulaService | None) -> None:
    """Wire (or unwire) the FormulaService singleton."""
    global _service
    _service = service


def get_service() -> FormulaService:
    if _service is None:
        raise RuntimeError("Fox gateway accessed before app startup bound a service")
    return _service


def bind_subscription_manager(mgr: SubscriptionManager | None) -> None:
    """Wire (or unwire) the SubscriptionManager singleton."""
    global _sub_manager
    _sub_manager = mgr


def get_subscription_manager() -> SubscriptionManager:
    if _sub_manager is None:
        raise RuntimeError("Fox subscription manager accessed before app startup")
    return _sub_manager


# --------------------------------------------------------------------------- #
# Public facade: one-shot evaluation
# --------------------------------------------------------------------------- #


async def evaluate(spec: FormulaSpec, *, t0_ns: int, t1_ns: int) -> FormulaResult:
    """Pure function: evaluate ``spec`` over the given window.

    Pulls inputs from the data source (Vole). Raises
    ``FormulaInputMissing`` if any binding's PV is not buffered.
    """
    return await get_service().evaluate(spec, t0_ns=t0_ns, t1_ns=t1_ns)


async def validate(spec: FormulaSpec) -> FormulaValidationResult:
    """Parse + AST-check without touching the data source. Cheap; no I/O."""
    return await get_service().validate(spec)
