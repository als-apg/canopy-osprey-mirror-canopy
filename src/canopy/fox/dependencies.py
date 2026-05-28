"""Typed ``Annotated[..., Depends(...)]`` aliases for Fox's API layer.

Routes pull these in by alias rather than re-spelling ``Depends()``.
"""

from typing import Annotated

from fastapi import Depends

from canopy.fox import gateway
from canopy.fox.services.formula_service import FormulaService


def get_formula_service() -> FormulaService:
    return gateway.get_service()


FormulaServiceDep = Annotated[FormulaService, Depends(get_formula_service)]
