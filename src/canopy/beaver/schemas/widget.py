"""DTOs for Beaver's widget-registry API.

The on-disk widget definition shape mirrors :class:`WidgetDTO` directly; the
loader parses each ``widgets/*.json`` file into one. Wire format is camelCase
via ``CanopyModel``'s ``to_camel`` alias generator (e.g. Python ``qt_mapping``
serializes as ``qtMapping``; Python ``class_`` serializes as ``class``).
"""

from typing import Any, Literal

from pydantic import Field

from canopy.contracts.base import CanopyModel

WidgetCategory = Literal[
    "indicators",
    "controls",
    "containers",
    "plots",
    "decorations",
    "drawing",
    "legacy",
]


class ReactComponentRef(CanopyModel):
    name: str
    module: str


class QtMapping(CanopyModel):
    # `class` is a Python keyword; explicit alias is the standard pydantic
    # pattern here. The project rule against per-field aliases targets
    # casing-only renames, which to_camel already handles for every other field.
    class_: str = Field(alias="class")
    extends: str | None = None
    header: str | None = None


class QtPropMapping(CanopyModel):
    to: str
    transform: str | None = None


class InspectorGroup(CanopyModel):
    name: str
    fields: list[str]


class InspectorSchema(CanopyModel):
    groups: list[InspectorGroup]


class WidgetDefaults(CanopyModel):
    width: int | None = None
    height: int | None = None


class WidgetDTO(CanopyModel):
    """Full widget definition. Returned by ``/widgets/{id}`` and ``/widgets/by-qt-class/{class}``."""

    id: str
    version: str
    display_name: str
    description: str
    category: WidgetCategory
    palette_icon: str

    react_component: ReactComponentRef
    qt_mapping: QtMapping | None = None
    prop_schema: dict[str, Any] = Field(default_factory=dict)
    qt_prop_map: dict[str, QtPropMapping] | None = None
    inspector_schema: InspectorSchema
    defaults: WidgetDefaults = Field(default_factory=WidgetDefaults)

    supports_pv: bool
    supports_rules: bool
    supports_children: bool


class WidgetSummaryDTO(CanopyModel):
    """Slim row for ``/widgets`` list responses (no schemas)."""

    id: str
    display_name: str
    category: WidgetCategory
    palette_icon: str
    version: str
    supports_pv: bool


class WidgetListDTO(CanopyModel):
    widgets: list[WidgetSummaryDTO]
    total: int


class CategoryDTO(CanopyModel):
    id: str
    display_name: str
    count: int


class CategoryListDTO(CanopyModel):
    categories: list[CategoryDTO]


class ValidateRequestDTO(CanopyModel):
    props: dict[str, Any]


class ValidationErrorDTO(CanopyModel):
    path: str
    message: str


class ValidateResultDTO(CanopyModel):
    valid: bool
    errors: list[ValidationErrorDTO] = Field(default_factory=list)
