"""Typed ``Annotated[..., Depends(...)]`` aliases for Beaver's API layer."""

from typing import Annotated

from fastapi import Depends

from canopy.beaver import gateway
from canopy.beaver.services.widget_service import WidgetService


def get_widget_service() -> WidgetService:
    return gateway.get_service()


WidgetServiceDep = Annotated[WidgetService, Depends(get_widget_service)]
