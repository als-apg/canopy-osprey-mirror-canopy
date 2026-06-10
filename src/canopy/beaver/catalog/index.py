"""WidgetIndex — in-memory catalog of widgets, populated once at startup.

Three lookup tables built by :class:`FileCatalogLoader` and bound on the
``WidgetService`` for the lifetime of the process. Immutable post-load in v1.
"""

from dataclasses import dataclass

from jsonschema import Draft7Validator

from canopy.beaver.schemas.widget import WidgetDTO


@dataclass
class WidgetIndex:
    by_id: dict[str, WidgetDTO]
    by_qt_class: dict[str, WidgetDTO]
    by_category: dict[str, list[WidgetDTO]]
    # Per-widget validator compiled once at load time; prop schemas are
    # immutable post-load, so validate_props never recompiles per request.
    validators: dict[str, Draft7Validator]
    registry_version: str

    @property
    def widget_count(self) -> int:
        return len(self.by_id)

    @property
    def category_count(self) -> int:
        return len(self.by_category)
