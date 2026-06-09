"""WidgetIndex — in-memory catalog of widgets, populated once at startup.

Three lookup tables built by :class:`FileCatalogLoader` and bound on the
``WidgetService`` for the lifetime of the process. Immutable post-load in v1.
"""

from dataclasses import dataclass

from canopy.beaver.schemas.widget import WidgetDTO


@dataclass
class WidgetIndex:
    by_id: dict[str, WidgetDTO]
    by_qt_class: dict[str, WidgetDTO]
    by_category: dict[str, list[WidgetDTO]]
    registry_version: str

    @property
    def widget_count(self) -> int:
        return len(self.by_id)

    @property
    def category_count(self) -> int:
        return len(self.by_category)
