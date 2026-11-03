"""Typed ``Annotated[..., Depends(...)]`` aliases for the Formula Engine's API layer.

Routes pull these in by alias rather than re-spelling ``Depends()``.
"""

from typing import Annotated

from fastapi import Depends

from canopy.formula_engine import gateway
from canopy.formula_engine.services.formula_service import FormulaService
from canopy.formula_engine.services.subscription_manager import SubscriptionManager


def get_formula_service() -> FormulaService:
    return gateway.get_service()


def get_subscription_manager() -> SubscriptionManager:
    return gateway.get_subscription_manager()


FormulaServiceDep = Annotated[FormulaService, Depends(get_formula_service)]
SubscriptionManagerDep = Annotated[SubscriptionManager, Depends(get_subscription_manager)]
