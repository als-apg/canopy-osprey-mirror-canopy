"""Typed ``Annotated[..., Depends(...)]`` aliases for Fox's API layer.

Routes pull these in by alias rather than re-spelling ``Depends()``.
"""

from typing import Annotated

from fastapi import Depends

from canopy.fox import gateway
from canopy.fox.services.formula_service import FormulaService
from canopy.fox.services.subscription_manager import SubscriptionManager


def get_formula_service() -> FormulaService:
    return gateway.get_service()


def get_subscription_manager() -> SubscriptionManager:
    return gateway.get_subscription_manager()


FormulaServiceDep = Annotated[FormulaService, Depends(get_formula_service)]
SubscriptionManagerDep = Annotated[SubscriptionManager, Depends(get_subscription_manager)]
