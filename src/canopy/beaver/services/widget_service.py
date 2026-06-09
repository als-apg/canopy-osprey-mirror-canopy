"""WidgetService — orchestration over the in-memory widget catalog.

One instance per ``canopy-api`` process, constructed by ``beaver_lifespan``.
Tests can construct their own with a fixture-loaded index and substitute it
via ``canopy.beaver.gateway.bind(service)`` or via FastAPI's
``dependency_overrides`` on the mounted sub-app.
"""

from typing import Any

import jsonschema

from canopy.beaver.config import BeaverConfig
from canopy.beaver.errors import WidgetNotFound, QtClassNotMapped
from canopy.beaver.catalog.index import WidgetIndex
from canopy.beaver.schemas.widget import (
    WidgetDTO,
    CategoryDTO,
    WidgetSummaryDTO,
    ValidateResultDTO,
    ValidationErrorDTO,
)

_CATEGORY_LABELS = {
    "indicators": "Indicators",
    "controls": "Controls",
    "containers": "Containers",
    "plots": "Plots & Charts",
    "decorations": "Decorations",
    "legacy": "Legacy",
}


class WidgetService:
    def __init__(self, index: WidgetIndex, config: BeaverConfig) -> None:
        self._index = index
        self._config = config

    @property
    def index(self) -> WidgetIndex:
        return self._index

    async def get(self, widget_id: str) -> WidgetDTO:
        widget = self._index.by_id.get(widget_id)
        if widget is None:
            raise WidgetNotFound(f"No widget registered with id {widget_id!r}")
        return widget

    async def by_qt_class(self, qt_class: str) -> WidgetDTO:
        widget = self._index.by_qt_class.get(qt_class)
        if widget is None:
            raise QtClassNotMapped(f"No widget mapped from Qt class {qt_class!r}")
        return widget

    async def list_summaries(self, category: str | None = None) -> list[WidgetSummaryDTO]:
        widgets = list(self._index.by_id.values())
        if category is not None:
            widgets = [w for w in widgets if w.category == category]
        widgets.sort(key=lambda w: w.id)
        return [
            WidgetSummaryDTO(
                id=w.id,
                display_name=w.display_name,
                category=w.category,
                palette_icon=w.palette_icon,
                version=w.version,
                supports_pv=w.supports_pv,
            )
            for w in widgets
        ]

    async def categories(self) -> list[CategoryDTO]:
        return [
            CategoryDTO(
                id=cat,
                display_name=_CATEGORY_LABELS.get(cat, cat.title()),
                count=len(widgets),
            )
            for cat, widgets in sorted(self._index.by_category.items())
        ]

    async def validate_props(self, widget_id: str, props: dict[str, Any]) -> ValidateResultDTO:
        widget = await self.get(widget_id)
        validator = jsonschema.Draft7Validator(widget.prop_schema)
        errors = sorted(validator.iter_errors(props), key=lambda e: list(e.absolute_path))
        if not errors:
            return ValidateResultDTO(valid=True)
        return ValidateResultDTO(
            valid=False,
            errors=[
                ValidationErrorDTO(
                    path="/" + "/".join(str(p) for p in e.absolute_path),
                    message=e.message,
                )
                for e in errors
            ],
        )
