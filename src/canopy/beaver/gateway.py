"""Public surface of the Beaver module for in-process callers.

Other Canopy modules import only from this file; the rest of
``canopy.beaver.*`` is private implementation. The widget-service singleton
is bound at app startup via ``beaver_lifespan``.
"""

from canopy.beaver.schemas.widget import (
    WidgetDTO,
    CategoryDTO,
    WidgetSummaryDTO,
    ValidateResultDTO,
)
from canopy.beaver.services.widget_service import WidgetService

_service: WidgetService | None = None


def bind(service: WidgetService | None) -> None:
    """Wire (or unwire) the WidgetService singleton."""
    global _service
    _service = service


def get_service() -> WidgetService:
    if _service is None:
        raise RuntimeError("Beaver gateway accessed before app startup bound a service")
    return _service


async def get(widget_id: str) -> WidgetDTO:
    """Return the full widget definition for a kebab-case id."""
    return await get_service().get(widget_id)


async def by_qt_class(qt_class: str) -> WidgetDTO:
    """Return the widget definition whose ``qt_mapping.class`` matches."""
    return await get_service().by_qt_class(qt_class)


async def list_summaries(category: str | None = None) -> list[WidgetSummaryDTO]:
    """Return slim row summaries, optionally filtered by category."""
    return await get_service().list_summaries(category)


async def categories() -> list[CategoryDTO]:
    """Return categories with widget counts."""
    return await get_service().categories()


async def validate_props(widget_id: str, props: dict) -> ValidateResultDTO:
    """Validate a props object against the widget's ``prop_schema``."""
    return await get_service().validate_props(widget_id, props)
